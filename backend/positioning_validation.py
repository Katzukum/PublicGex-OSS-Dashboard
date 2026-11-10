"""Read-only, chronological shadow evaluation of strike concentration maps.

Run with an explicit private-app database path::

    python backend/positioning_validation.py --db .data/gex_data.db --symbol SPX

This module deliberately does not import the application, ORM models, or collector.
It neither changes trading signals nor writes labels into the source database.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable


METHODS = ("oi_concentration", "recent_activity", "abs_net_oi_proxy", "nearest_strike")
DISTANCE_BUCKETS = ((0.0, 0.25), (0.25, 0.5), (0.5, 1.0))
RESEARCH_FAMILIES = (
    "envelope_touch", "pressure_direction", "pressure_divergence", "exhaustion_reversal",
)
RESEARCH_HORIZONS = (5, 15, 30)
RESEARCH_MODEL = "option_research_shadow_v1"


@dataclass(frozen=True)
class EvaluationConfig:
    horizon_minutes: int = 15
    levels_per_method: int = 3
    max_distance_pct: float = 1.0
    touch_tolerance_pct: float = 0.01
    max_observation_gap_seconds: int = 120
    minimum_activity_minutes: float = 14.0

    def __post_init__(self):
        if self.horizon_minutes < 1 or self.levels_per_method < 1:
            raise ValueError("Horizon and level count must be positive")
        if not 0 <= self.touch_tolerance_pct < self.max_distance_pct <= 1:
            raise ValueError("Require 0 <= touch tolerance < maximum distance <= 1 percent")
        if self.max_observation_gap_seconds < 1 or not 0 < self.minimum_activity_minutes <= 15:
            raise ValueError("Observation gap must be positive and activity history at most 15 minutes")


@dataclass(frozen=True)
class ResearchEvaluationConfig:
    horizons_minutes: tuple[int, ...] = RESEARCH_HORIZONS
    primary_spacing_minutes: int = 30
    max_observation_gap_seconds: int = 120
    touch_tolerance_pct: float = 0.01
    reversal_threshold_pct: float = 0.1
    observation_timezone: str = "America/New_York"

    def __post_init__(self):
        if tuple(self.horizons_minutes) != RESEARCH_HORIZONS:
            raise ValueError("Research outcomes are fixed at 5, 15, and 30 minutes")
        if self.primary_spacing_minutes < max(self.horizons_minutes):
            raise ValueError("Primary samples must be spaced by at least 30 minutes")
        if self.max_observation_gap_seconds < 1:
            raise ValueError("Observation gap must be positive")
        if self.touch_tolerance_pct < 0 or self.reversal_threshold_pct <= 0:
            raise ValueError("Outcome thresholds must be nonnegative")


@dataclass(frozen=True)
class Snapshot:
    id: int
    timestamp: datetime
    spot: float


def _finite(value) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _snapshots(conn: sqlite3.Connection, symbol: str) -> list[Snapshot]:
    rows = conn.execute(
        "SELECT id, timestamp, spot_price FROM gex_snapshots WHERE symbol = ? ORDER BY timestamp, id",
        (symbol.upper(),),
    )
    snapshots = []
    for identifier, timestamp, spot in rows:
        value = _finite(spot)
        try:
            parsed = datetime.fromisoformat(str(timestamp))
        except (ValueError, TypeError):
            continue
        # Stored collector timestamps are naive local wall-clock times. Reject an
        # incompatible timestamp rather than silently changing its session date.
        if parsed.tzinfo is not None or value is None or value <= 0:
            continue
        snapshots.append(Snapshot(int(identifier), parsed, value))
    return snapshots


def _forward_path(day: list[Snapshot], index: int, config: EvaluationConfig) -> list[Snapshot] | None:
    """Only label observations through the horizon; a later row proves coverage."""
    entry = day[index]
    end = entry.timestamp + timedelta(minutes=config.horizon_minutes)
    previous = entry
    path = []
    for candidate in day[index + 1:]:
        if (candidate.timestamp - previous.timestamp).total_seconds() > config.max_observation_gap_seconds:
            return None
        if candidate.timestamp <= end:
            path.append(candidate)
        if candidate.timestamp >= end:
            return path if path else None
        previous = candidate
    return None


def _select_levels(profile: dict, spot: float, config: EvaluationConfig) -> dict[str, list[dict]] | None:
    coverage = profile.get("coverage") or {}
    if (_finite(coverage.get("observed_minutes")) or 0) < config.minimum_activity_minutes:
        return None
    if (_finite(coverage.get("valid_activity_contracts")) or 0) <= 0:
        return None
    if (_finite(coverage.get("max_gap_seconds")) or 0) > config.max_observation_gap_seconds:
        return None
    eligible = []
    for row in profile.get("strikes") or []:
        strike = _finite(row.get("strike"))
        if strike is None or strike <= 0:
            continue
        distance = 100 * abs(strike - spot) / spot
        if not config.touch_tolerance_pct < distance <= config.max_distance_pct:
            continue
        oi = _finite(row.get("gross_oi_gex"))
        net = _finite(row.get("net_oi_proxy"))
        activity = _finite(row.get("activity_15m"))
        eligible.append({
            "strike": strike,
            "distance_pct": distance,
            "oi_concentration": max(oi or 0, 0),
            "recent_activity": max(activity or 0, 0),
            "abs_net_oi_proxy": abs(net or 0),
        })
    selected = {}
    for method in METHODS:
        candidates = eligible if method == "nearest_strike" else [row for row in eligible if row[method] > 0]
        if len(candidates) < config.levels_per_method:
            return None
        if method == "nearest_strike":
            ranked = sorted(candidates, key=lambda row: (row["distance_pct"], row["strike"]))
        else:
            ranked = sorted(candidates, key=lambda row: (-row[method], row["distance_pct"], row["strike"]))
        selected[method] = ranked[:config.levels_per_method]
    return selected


def _touches(strike: float, entry_spot: float, path: list[Snapshot], tolerance: float) -> bool:
    # A crossing between sampled prices is a proxy for an intervening touch.
    previous = entry_spot
    for point in path:
        if abs(point.spot - strike) <= tolerance or (previous - strike) * (point.spot - strike) <= 0:
            return True
        previous = point.spot
    return False


def _summary(records: list[dict]) -> dict:
    methods = {}
    for method in METHODS:
        levels = [level for record in records for level in record["methods"][method]]
        touches = sum(level["touched"] for level in levels)
        buckets = {}
        for lower, upper in DISTANCE_BUCKETS:
            members = [level for level in levels if lower < level["distance_pct"] <= upper]
            hits = sum(level["touched"] for level in members)
            buckets[f"({lower:g},{upper:g}]%"] = {
                "levels": len(members), "touches": hits,
                "touch_rate": hits / len(members) if members else None,
            }
        methods[method] = {
            "samples": len(records), "levels": len(levels), "touches": touches,
            "touch_rate": touches / len(levels) if levels else None,
            "mean_distance_pct": sum(level["distance_pct"] for level in levels) / len(levels) if levels else None,
            "distance_buckets": buckets,
        }
    return {"samples": len(records), "methods": methods}


def _research_config_dict(config: ResearchEvaluationConfig) -> dict[str, Any]:
    return {
        "horizons_minutes": list(config.horizons_minutes),
        "primary_spacing_minutes": config.primary_spacing_minutes,
        "maximum_observation_gap_seconds": config.max_observation_gap_seconds,
        "touch_tolerance_pct": config.touch_tolerance_pct,
        "reversal_threshold_pct": config.reversal_threshold_pct,
        "observation_timezone": config.observation_timezone,
    }


def _config_fingerprint(config: ResearchEvaluationConfig, families: Iterable[str]) -> str:
    payload = {"model": RESEARCH_MODEL, "families": list(families), "config": _research_config_dict(config)}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def _research_forward_paths(
    day: list[Snapshot], index: int, config: ResearchEvaluationConfig,
) -> tuple[dict[int, list[Snapshot]] | None, str | None]:
    """Return complete paths at each fixed horizon from a frozen entry."""
    entry = day[index]
    paths: dict[int, list[Snapshot]] = {}
    previous = entry
    observed: list[Snapshot] = []
    pending = list(config.horizons_minutes)
    for candidate in day[index + 1:]:
        gap = (candidate.timestamp - previous.timestamp).total_seconds()
        if gap <= 0 or gap > config.max_observation_gap_seconds:
            return None, "incomplete_forward_coverage"
        observed.append(candidate)
        previous = candidate
        while pending and candidate.timestamp >= entry.timestamp + timedelta(minutes=pending[0]):
            horizon = pending.pop(0)
            paths[horizon] = list(observed)
        if not pending:
            return paths, None
    return None, "incomplete_forward_coverage"


def _outcomes(entry: Snapshot, paths: dict[int, list[Snapshot]], direction: int = 1) -> dict[str, dict]:
    result = {}
    for horizon, path in sorted(paths.items()):
        end = path[-1].spot
        moves = [point.spot - entry.spot for point in path]
        signed = [direction * move for move in moves]
        result[str(horizon)] = {
            "end_spot": end,
            "return_points": end - entry.spot,
            "return_pct": 100 * (end - entry.spot) / entry.spot,
            "directional_return_points": direction * (end - entry.spot),
            "directional_return_pct": direction * 100 * (end - entry.spot) / entry.spot,
            "mfe_points": max([0.0, *signed]),
            "mae_points": max([0.0, *(-value for value in signed)]),
            "mfe_pct": 100 * max([0.0, *signed]) / entry.spot,
            "mae_pct": 100 * max([0.0, *(-value for value in signed)]) / entry.spot,
            "last_observation": path[-1].timestamp.isoformat(),
        }
    return result


def _touch_outcome(
    level: float, entry: Snapshot, day: list[Snapshot], entry_index: int,
    paths: dict[int, list[Snapshot]], config: ResearchEvaluationConfig,
) -> dict[str, Any]:
    tolerance = entry.spot * config.touch_tolerance_pct / 100
    full_path = paths[max(config.horizons_minutes)]
    previous = entry.spot
    touched_at = None
    touch_index = None
    for relative_index, point in enumerate(full_path):
        if abs(point.spot - level) <= tolerance or (previous - level) * (point.spot - level) <= 0:
            touched_at = point.timestamp
            touch_index = day.index(point)
            break
        previous = point.spot
    direction = 1 if level >= entry.spot else -1
    through = back = 0.0
    post_touch_returns = {str(horizon): None for horizon in config.horizons_minutes}
    if touched_at is not None and touch_index is not None:
        after = day[touch_index:]
        previous_point = after[0]
        for point in after:
            if (point.timestamp - previous_point.timestamp).total_seconds() > config.max_observation_gap_seconds:
                break
            displacement = direction * (point.spot - level)
            through = max(through, displacement)
            back = max(back, -displacement)
            previous_point = point
            for horizon in config.horizons_minutes:
                key = str(horizon)
                if post_touch_returns[key] is None and point.timestamp >= touched_at + timedelta(minutes=horizon):
                    post_touch_returns[key] = 100 * (point.spot - after[0].spot) / after[0].spot
    return {
        "level": level,
        "initial_distance_pct": 100 * abs(level - entry.spot) / entry.spot,
        "touched": touched_at is not None,
        "time_to_touch_seconds": (touched_at - entry.timestamp).total_seconds() if touched_at else None,
        "signed_excursion_through_points": through if touched_at else None,
        "excursion_back_toward_entry_points": back if touched_at else None,
        "returns_after_touch_pct": post_touch_returns,
    }


def _nearest_distinct_strikes(rows: Iterable[dict], spot: float, count: int = 2) -> list[float]:
    strikes = sorted({
        value for row in rows
        if (value := _finite(row.get("strike_price"))) is not None and value > 0 and value != spot
    }, key=lambda value: (abs(value - spot), value))
    return strikes[:count]


def _envelope_sample(
    conn: sqlite3.Connection, entry: Snapshot, day: list[Snapshot], index: int,
    observations: dict, paths: dict[int, list[Snapshot]], config: ResearchEvaluationConfig,
) -> tuple[dict | None, str | None]:
    from option_envelopes import build_option_envelopes
    from positioning import load_positioning

    snapshot = next(
        (item for item in observations.get("snapshots", []) if item.get("snapshot_id") == entry.id), None
    )
    rows = list((snapshot or {}).get("contracts") or [])
    if not rows:
        return None, "missing_snapshot_contracts"
    flow = build_option_envelopes(
        rows, spot=entry.spot, source="volume_gamma", symbol="SPX", as_of=entry.timestamp
    )
    structural = build_option_envelopes(
        rows, spot=entry.spot, source="oi_gamma", symbol="SPX", as_of=entry.timestamp
    )
    positioning = load_positioning(conn, entry.id)
    persistent = [_finite(value) for value in (positioning.get("top_levels") or [])[:2]]
    persistent = [value for value in persistent if value is not None]
    nearest = _nearest_distinct_strikes(rows, entry.spot, 2)
    comparisons = {}
    for name, envelope in (("flow", flow), ("structural", structural)):
        envelope_levels = [
            ("major", _finite(envelope.get("major_gamma"))),
            ("minor", _finite(envelope.get("minor_gamma"))),
        ]
        if not all(value is not None for _, value in envelope_levels) or len(persistent) < 2 or len(nearest) < 2:
            continue
        frozen = [
            *( (f"{name}_{label}", value) for label, value in envelope_levels ),
            *((f"persistent_oi_{position + 1}", value) for position, value in enumerate(persistent)),
            *((f"nearest_strike_{position + 1}", value) for position, value in enumerate(nearest)),
        ]
        comparisons[name] = {
            "model": envelope.get("model"),
            "levels": {
                label: _touch_outcome(float(level), entry, day, index, paths, config)
                for label, level in frozen
            },
        }
    if not comparisons:
        return None, "insufficient_matched_envelope_levels"
    return {"comparisons": comparisons}, None


def _pressure_features(pressure: dict, observations: dict, entry: Snapshot) -> tuple[dict | None, str | None]:
    points = [point for point in pressure.get("points", []) if point.get("usable")]
    current = next((point for point in reversed(points) if _finite(point.get("cumulative_dollars")) is not None), None)
    if current is None:
        return None, "insufficient_pressure_coverage"
    slope = _finite(current.get("slope_5m"))
    if slope is None or slope == 0:
        return None, "flat_or_unavailable_pressure_slope"
    cumulative = _finite(current.get("cumulative_dollars"))
    previous = points[-2] if len(points) >= 2 else None
    previous_slope = _finite(previous.get("slope_5m")) if previous else None
    ema_fast = _finite(current.get("ema_fast"))
    ema_slow = _finite(current.get("ema_slow"))
    spot_samples = observations.get("spot_observations") or []
    prior_spots = [
        (_time, _finite(row.get("price")))
        for row in spot_samples
        if (_time := _parse_time(row.get("timestamp"))) is not None and _time <= entry.timestamp
    ]
    prior = next((price for when, price in reversed(prior_spots) if when <= entry.timestamp - timedelta(minutes=5)), None)
    price_change_5m = entry.spot - prior if prior is not None else None
    features = {
        "direction": 1 if slope > 0 else -1,
        "slope_5m": slope,
        "slope_15m": None,
        "slope_acceleration": slope - previous_slope if previous_slope is not None else None,
        "ema_fast_difference": cumulative - ema_fast if cumulative is not None and ema_fast is not None else None,
        "ema_slow_difference": cumulative - ema_slow if cumulative is not None and ema_slow is not None else None,
        "price_change_5m": price_change_5m,
        "classification_ratio": _finite(current.get("classification_ratio")),
    }
    if len(points) >= 16:
        prior_cumulative = _finite(points[-16].get("cumulative_dollars"))
        if cumulative is not None and prior_cumulative is not None:
            features["slope_15m"] = (cumulative - prior_cumulative) / 15
    features["divergence"] = bool(
        price_change_5m is not None and price_change_5m != 0 and price_change_5m * slope < 0
    )
    return features, None


def _parse_time(value: Any) -> datetime | None:
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=None) if parsed.tzinfo is not None else parsed


def _pressure_baselines(conn: sqlite3.Connection, entry: Snapshot, direction: int) -> dict[str, Any]:
    from positioning import load_positioning
    profile = load_positioning(conn, entry.id)
    levels = sorted(_finite(value) for value in (profile.get("top_levels") or []) if _finite(value) is not None)
    target_candidates = [value for value in levels if (value - entry.spot) * direction > 0]
    invalidation_candidates = [value for value in levels if (value - entry.spot) * direction < 0]
    target = min(target_candidates, key=lambda value: abs(value - entry.spot)) if target_candidates else None
    invalidation = min(invalidation_candidates, key=lambda value: abs(value - entry.spot)) if invalidation_candidates else None
    return {"target": target, "invalidation": invalidation}


def _level_hits(levels: dict[str, Any], entry: Snapshot, path: list[Snapshot]) -> dict[str, Any]:
    result = {}
    for name in ("target", "invalidation"):
        level = levels.get(name)
        result[f"{name}_hit"] = None if level is None else _touches(level, entry.spot, path, 0.0)
    return result


def _envelope_history(observations: dict, symbol: str) -> list[dict]:
    from option_envelopes import build_option_envelopes
    history = []
    for snapshot in observations.get("snapshots", []):
        rows = snapshot.get("contracts") or []
        spot = snapshot.get("spot")
        if not rows or _finite(spot) is None:
            continue
        history.append({
            "snapshot_id": snapshot.get("snapshot_id"),
            "as_of": snapshot.get("timestamp"),
            "available_at": snapshot.get("available_at"),
            "flow": build_option_envelopes(rows, spot=spot, source="volume_gamma", symbol=symbol, as_of=snapshot.get("timestamp")),
            "structural": build_option_envelopes(rows, spot=spot, source="oi_gamma", symbol=symbol, as_of=snapshot.get("timestamp")),
        })
    return history


def _summarize_research(records: list[dict], family: str, horizons: Iterable[int]) -> dict:
    family_records = [record for record in records if record["family"] == family]
    sessions = sorted({record["session_date"] for record in family_records})
    summary: dict[str, Any] = {"samples": len(family_records), "sessions": sessions}
    if family == "envelope_touch":
        series: dict[str, list[dict]] = defaultdict(list)
        for record in family_records:
            for comparison in record["features"]["comparisons"].values():
                for name, outcome in comparison["levels"].items():
                    series[name].append(outcome)
        summary["series"] = {}
        for name, values in sorted(series.items()):
            touched = [value for value in values if value["touched"]]
            summary["series"][name] = {
                "samples": len(values),
                "touches": len(touched),
                "touch_rate": len(touched) / len(values) if values else None,
                "mean_initial_distance_pct": sum(value["initial_distance_pct"] for value in values) / len(values) if values else None,
                "mean_time_to_touch_seconds": sum(value["time_to_touch_seconds"] for value in touched) / len(touched) if touched else None,
            }
    else:
        horizon_summary = {}
        for horizon in horizons:
            values = [record["outcomes"][str(horizon)] for record in family_records]
            horizon_summary[str(horizon)] = {
                "samples": len(values),
                "mean_directional_return_pct": sum(value["directional_return_pct"] for value in values) / len(values) if values else None,
                "mean_mfe_pct": sum(value["mfe_pct"] for value in values) / len(values) if values else None,
                "mean_mae_pct": sum(value["mae_pct"] for value in values) / len(values) if values else None,
            }
        summary["horizons"] = horizon_summary
        if family == "exhaustion_reversal":
            hits = [bool(record["features"]["reversal_hit"]) for record in family_records]
            summary["reversal_hits"] = sum(hits)
            summary["reversal_hit_rate"] = sum(hits) / len(hits) if hits else None
            summary["continuation_failure_rate"] = 1 - summary["reversal_hit_rate"] if hits else None
            segments = Counter(
                (record["features"].get("side"), record["features"].get("action"), record["features"].get("hedge_direction"))
                for record in family_records
            )
            summary["segments"] = {
                "|".join(str(value) for value in key): count for key, count in sorted(segments.items())
            }
    return summary


def evaluate_research_connection(
    conn: sqlite3.Connection, symbol: str, *, families: Iterable[str],
    config: ResearchEvaluationConfig | None = None, include_samples: bool = False,
) -> dict:
    """Evaluate observational research families from bounded prefixes."""
    from exhaustion import MODEL as EXHAUSTION_MODEL, build_exhaustion_events
    from flow_pressure import MODEL as PRESSURE_MODEL, build_estimated_pressure
    from option_envelopes import MODEL as ENVELOPE_MODEL
    from research_observations import MODEL as OBSERVATION_MODEL, load_session_observations

    config = config or ResearchEvaluationConfig()
    requested = tuple(dict.fromkeys(str(value).strip() for value in families if str(value).strip()))
    invalid = [family for family in requested if family not in RESEARCH_FAMILIES]
    if invalid or not requested:
        raise ValueError(f"Unknown or empty research families: {', '.join(invalid) or 'none'}")
    symbol = symbol.upper()
    days: dict[str, list[Snapshot]] = defaultdict(list)
    for snapshot in _snapshots(conn, symbol):
        days[snapshot.timestamp.date().isoformat()].append(snapshot)
    session_dates = sorted(days)
    split_index = min(len(session_dates) - 1, max(1, int(len(session_dates) * 0.8))) if len(session_dates) > 1 else len(session_dates)
    development_dates = session_dates[:split_index]
    holdout_dates = session_dates[split_index:]
    split_lookup = {date: "development" if date in development_dates else "holdout" for date in session_dates}
    fingerprint = _config_fingerprint(config, requested)
    skipped: dict[str, Counter] = {family: Counter() for family in requested}
    clock_exclusions = Counter()
    interval_exclusions = Counter()
    candidates = Counter()
    records: list[dict] = []

    for session_date, day in sorted(days.items()):
        next_entry_at = {family: None for family in requested}
        for index, entry in enumerate(day):
            active = [
                family for family in requested
                if next_entry_at[family] is None or entry.timestamp >= next_entry_at[family]
            ]
            if not active:
                continue
            for family in active:
                next_entry_at[family] = entry.timestamp + timedelta(minutes=config.primary_spacing_minutes)
                candidates[family] += 1
            paths, path_reason = _research_forward_paths(day, index, config)
            if paths is None:
                for family in active:
                    skipped[family][path_reason or "incomplete_forward_coverage"] += 1
                continue
            observations = load_session_observations(
                conn, symbol=symbol, as_of=entry.timestamp, snapshot_id=entry.id,
                observation_timezone=config.observation_timezone,
            )
            for reason, count in (observations.get("coverage", {}).get("clock_exclusions") or {}).items():
                clock_exclusions[reason] += count
            for reason, count in (observations.get("coverage", {}).get("discarded_by_reason") or {}).items():
                interval_exclusions[reason] += count
            common = {
                "snapshot_id": entry.id,
                "as_of": entry.timestamp.isoformat(),
                "session_date": session_date,
                "split": split_lookup[session_date],
                "spot": entry.spot,
                "config_fingerprint": fingerprint,
            }
            pressure = None
            for family in active:
                if family == "envelope_touch":
                    features, reason = _envelope_sample(conn, entry, day, index, observations, paths, config)
                    if features is None:
                        skipped[family][reason or "unavailable"] += 1
                        continue
                    records.append({**common, "family": family, "features": features, "outcomes": {}})
                    continue
                if pressure is None:
                    pressure_rows = []
                    for raw in observations.get("rows") or []:
                        row = dict(raw)
                        if not row.get("available_at"):
                            continue
                        for name in ("bid_timestamp", "ask_timestamp", "last_timestamp"):
                            row[name] = row.get(f"{name}_utc")
                        pressure_rows.append(row)
                    pressure = build_estimated_pressure(
                        pressure_rows,
                        symbol=symbol,
                        as_of=observations.get("available_at") or entry.timestamp,
                        config={"observation_timezone": config.observation_timezone},
                    )
                if family in ("pressure_direction", "pressure_divergence"):
                    features, reason = _pressure_features(pressure, observations, entry)
                    if features is None or (family == "pressure_divergence" and not features["divergence"]):
                        skipped[family][reason or "no_pressure_divergence"] += 1
                        continue
                    direction = int(features["direction"])
                    outcomes = _outcomes(entry, paths, direction)
                    baselines = _pressure_baselines(conn, entry, direction)
                    features["baseline_levels"] = baselines
                    features["baseline_hits_30m"] = _level_hits(baselines, entry, paths[30])
                    records.append({**common, "family": family, "features": features, "outcomes": outcomes})
                    continue
                history = _envelope_history(observations, symbol)
                canonical_spots = [
                    {**row, "timestamp": row.get("available_at") or row.get("timestamp")}
                    for row in observations.get("spot_observations") or []
                ]
                exhaustion = build_exhaustion_events(
                    pressure, canonical_spots, history, config=None,
                    as_of=observations.get("available_at") or entry.timestamp,
                )
                event = exhaustion.get("current")
                if not event:
                    skipped[family]["no_active_exhaustion_candidate"] += 1
                    continue
                direction = -1 if event.get("side") == "CALL" else 1
                outcomes = _outcomes(entry, paths, direction)
                threshold = entry.spot * config.reversal_threshold_pct / 100
                reversal_hit = any(direction * (point.spot - entry.spot) >= threshold for point in paths[30])
                envelope = event.get("nearest_envelope") or {}
                proximity = (
                    envelope.get("nearest_kind") if _finite(envelope.get("distance_percent")) is not None
                    and envelope["distance_percent"] <= 0.001 else "none"
                )
                features = {
                    "event_id": event.get("id"), "side": event.get("side"),
                    "action": event.get("action"), "hedge_direction": event.get("hedge_direction"),
                    "score": event.get("score"), "classification_ratio": event.get("classification_ratio"),
                    "gamma_regime": None, "envelope_proximity": proximity,
                    "price_filter_enabled": bool(exhaustion.get("config", {}).get("price_filter_enabled", True)),
                    "hypothesis_direction": direction, "reversal_hit": reversal_hit,
                }
                records.append({**common, "family": family, "features": features, "outcomes": outcomes})

    development = [record for record in records if record["split"] == "development"]
    holdout = [record for record in records if record["split"] == "holdout"]
    usable_sessions = len({record["session_date"] for record in records})
    exhaustion_count = sum(record["family"] == "exhaustion_reversal" for record in records)
    prerequisites = {
        "minimum_independent_sessions": 20,
        "minimum_exhaustion_candidates": 100,
        "observed_independent_sessions": usable_sessions,
        "observed_exhaustion_candidates": exhaustion_count,
        "eligible_for_signal_review": usable_sessions >= 20 and exhaustion_count >= 100,
        "automatically_enables_signals": False,
    }
    if not records:
        status = "no_qualifying_samples"
    elif not development or not holdout:
        status = "insufficient_holdout"
    elif not prerequisites["eligible_for_signal_review"]:
        status = "insufficient_evidence"
    else:
        status = "ready"
    result = {
        "model": RESEARCH_MODEL,
        "status": status,
        "symbol": symbol,
        "read_only": True,
        "families": list(requested),
        "model_versions": {
            "observations": OBSERVATION_MODEL, "envelopes": ENVELOPE_MODEL,
            "pressure": PRESSURE_MODEL, "exhaustion": EXHAUSTION_MODEL,
        },
        "config": _research_config_dict(config),
        "config_fingerprint": fingerprint,
        "development_sessions": development_dates,
        "holdout_sessions": holdout_dates,
        "coverage": {
            "recorded_sessions": len(session_dates),
            "candidates_by_family": dict(candidates),
            "usable_samples_by_family": dict(Counter(record["family"] for record in records)),
            "excluded_by_family": {family: dict(values) for family, values in skipped.items()},
            "clock_exclusions": dict(clock_exclusions),
            "interval_exclusions": dict(interval_exclusions),
        },
        "development": {
            family: _summarize_research(development, family, config.horizons_minutes) for family in requested
        },
        "holdout": {
            family: _summarize_research(holdout, family, config.horizons_minutes) for family in requested
        },
        "evidence_prerequisites": prerequisites,
        "methodology": {
            "causality": "Each feature is calculated from a database prefix bounded by entry timestamp and snapshot id; forward rows are loaded only for outcomes.",
            "split": "Oldest 80% of sessions are development and the newest remainder is frozen holdout; sessions never cross splits.",
            "sampling": "Primary observations are fixed before feature availability and spaced by 30 minutes per family.",
            "outcomes": "Complete 5/15/30-minute paths require consecutive observations no more than 120 seconds apart.",
            "thresholds": "All thresholds are fixed engineering research hypotheses; holdout results do not tune them.",
        },
        "limitations": [
            "Observed spot samples omit intrainterval highs and lows; crossings are touch proxies.",
            "Estimated pressure and exhaustion do not observe dealer/customer identity.",
            "Insufficient evidence never enables or changes trading signals.",
        ],
    }
    if include_samples:
        result["samples"] = records
    return result


def evaluate_connection(
    conn: sqlite3.Connection,
    symbol: str = "SPX",
    *,
    config: EvaluationConfig | None = None,
    feature_loader: Callable | None = None,
    include_samples: bool = False,
) -> dict:
    """Evaluate fixed as-of rankings on identical, nonoverlapping observations."""
    if feature_loader is None:
        from positioning import load_positioning
        feature_loader = load_positioning
    config = config or EvaluationConfig()
    days = defaultdict(list)
    for snapshot in _snapshots(conn, symbol):
        days[snapshot.timestamp.date().isoformat()].append(snapshot)
    session_dates = sorted(days)
    # No outcome-dependent tuning or fitting: the oldest 80% of recorded sessions
    # is a development report; the remaining newest sessions are held out.
    split_index = min(len(session_dates) - 1, max(1, int(len(session_dates) * 0.8))) if len(session_dates) > 1 else len(session_dates)
    development_dates = session_dates[:split_index]
    holdout_dates = session_dates[split_index:]
    skipped = Counter()
    records = []
    for session_date, day in sorted(days.items()):
        next_entry_at = None
        for index, entry in enumerate(day):
            if next_entry_at is not None and entry.timestamp < next_entry_at:
                continue
            # Fix the candidate schedule before checking either features or
            # outcomes, and avoid repeatedly rescanning an unavailable session.
            next_entry_at = entry.timestamp + timedelta(minutes=config.horizon_minutes)
            path = _forward_path(day, index, config)
            if path is None:
                skipped["incomplete_forward_coverage"] += 1
                continue
            # Only this entry's identifier is exposed to the feature loader.
            # Future spot rows are used solely for the predefined outcome.
            profile = feature_loader(conn, entry.id)
            selected = _select_levels(profile, entry.spot, config)
            if selected is None:
                skipped["insufficient_matched_features"] += 1
                continue
            methods = {}
            for method, levels in selected.items():
                methods[method] = [{
                    "strike": level["strike"],
                    "distance_pct": level["distance_pct"],
                    "touched": _touches(level["strike"], entry.spot, path, entry.spot * config.touch_tolerance_pct / 100),
                } for level in levels]
            horizon = entry.timestamp + timedelta(minutes=config.horizon_minutes)
            records.append({
                "snapshot_id": entry.id, "as_of": entry.timestamp.isoformat(),
                "session_date": session_date, "spot": entry.spot,
                "split": "development" if session_date in development_dates else "holdout",
                "horizon_at": horizon.isoformat(),
                "last_outcome_observation": path[-1].timestamp.isoformat(),
                "outcome_endpoint_lag_seconds": (horizon - path[-1].timestamp).total_seconds(),
                "methods": methods,
            })
    development = [record for record in records if record["split"] == "development"]
    holdout = [record for record in records if record["split"] == "holdout"]
    status = "ready" if development and holdout else "insufficient_holdout" if records else "no_qualifying_samples"
    result = {
        "status": status,
        "symbol": symbol.upper(),
        "read_only": True,
        "methodology": {
            "features": "As-of snapshot only; fixed rankings, no fitted parameters or outcome-based tuning.",
            "outcome": "A sampled spot within tolerance or a crossing between consecutive spots during (entry, entry + horizon].",
            "split": "Oldest 80% of recorded sessions for development; newest remainder held out; at least two sessions required.",
            "session_clock": "Calendar date of stored collector local wall-clock timestamps; timezone-aware rows are excluded.",
            "comparison": "Identical entry snapshots and level counts across all methods; positive feature ranks within a common near-spot strike range.",
            "sampling": "Candidates start at the first valid snapshot each session, then the first snapshot at least one horizon later; unavailable candidates are excluded jointly.",
            "distance_control": "Mean initial distance and fixed distance buckets reported; exact distance matching is not performed.",
            "horizon_minutes": config.horizon_minutes,
            "levels_per_method": config.levels_per_method,
            "maximum_distance_pct": config.max_distance_pct,
            "touch_tolerance_pct": config.touch_tolerance_pct,
            "maximum_observation_gap_seconds": config.max_observation_gap_seconds,
            "minimum_activity_minutes": config.minimum_activity_minutes,
        },
        "development_sessions": development_dates,
        "holdout_sessions": holdout_dates,
        "skipped_candidates": dict(skipped),
        "development": _summary(development),
        "holdout": _summary(holdout),
        "limitations": [
            "These outcomes test level proximity and price reach, not dealer ownership, inventory direction, profitability, or calibrated confidence.",
            "Observed spot samples omit intrainterval moves. Crossings are touch proxies; no intraday high/low feed is available here.",
            "Initial distance affects touch probability. Bucket summaries do not establish improvement without comparable distances and enough independent sessions.",
            "Levels within a sample and observations within a session are correlated; raw level counts are not independent trials.",
            "Stored history may omit active zero-OI contracts collected before the coverage change; the evaluator cannot reconstruct missing contracts.",
            "No trading signal is promoted or changed by this report.",
        ],
    }
    if include_samples:
        result["samples"] = records
    return result


def evaluate_database(db_path: str | Path, symbol: str = "SPX", *, include_samples: bool = False) -> dict:
    """Opening a missing path fails instead of creating or migrating a database."""
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        return evaluate_connection(conn, symbol, include_samples=include_samples)
    finally:
        conn.close()


def evaluate_research_database(
    db_path: str | Path,
    symbol: str = "SPX",
    *,
    families: Iterable[str] = RESEARCH_FAMILIES,
    include_samples: bool = False,
) -> dict:
    """Evaluate research families while keeping the explicitly selected DB read-only."""
    path = Path(db_path).expanduser().resolve()
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        return evaluate_research_connection(
            conn, symbol, families=families, include_samples=include_samples,
        )
    finally:
        conn.close()


def _write_research_csv(path: Path, result: dict) -> None:
    """Export causal samples; nested feature/outcome values remain lossless JSON."""
    fields = (
        "model", "config_fingerprint", "family", "split", "session_date",
        "snapshot_id", "as_of", "spot", "features_json", "outcomes_json",
    )
    with path.open("w", encoding="utf-8", newline="") as target:
        writer = csv.DictWriter(target, fieldnames=fields)
        writer.writeheader()
        for sample in result.get("samples") or []:
            writer.writerow({
                "model": result.get("model"),
                "config_fingerprint": result.get("config_fingerprint"),
                "family": sample.get("family"),
                "split": sample.get("split"),
                "session_date": sample.get("session_date"),
                "snapshot_id": sample.get("snapshot_id"),
                "as_of": sample.get("as_of"),
                "spot": sample.get("spot"),
                "features_json": json.dumps(sample.get("features") or {}, sort_keys=True, allow_nan=False),
                "outcomes_json": json.dumps(sample.get("outcomes") or {}, sort_keys=True, allow_nan=False),
            })


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Explicit path to this app's SQLite database (opened read-only)")
    parser.add_argument("--symbol", default="SPX")
    parser.add_argument("--include-samples", action="store_true", help="Include per-snapshot selected strikes and outcome observations")
    parser.add_argument(
        "--families", nargs="+", choices=RESEARCH_FAMILIES,
        help="Run the observational research evaluator for the selected families instead of the legacy positioning report",
    )
    parser.add_argument("--output-json", type=Path, help="Also write the report as JSON")
    parser.add_argument("--output-csv", type=Path, help="Write causal research samples as CSV")
    args = parser.parse_args(argv)
    try:
        if args.families:
            result = evaluate_research_database(
                args.db,
                args.symbol,
                families=args.families,
                include_samples=bool(args.include_samples or args.output_csv),
            )
        else:
            if args.output_csv:
                raise ValueError("--output-csv requires --families")
            result = evaluate_database(args.db, args.symbol, include_samples=args.include_samples)
        if args.output_json:
            args.output_json.write_text(
                json.dumps(result, indent=2, allow_nan=False) + "\n", encoding="utf-8",
            )
        if args.output_csv:
            _write_research_csv(args.output_csv, result)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
