"""Read-only, chronological shadow evaluation of strike concentration maps.

Run with an explicit private-app database path::

    python backend/positioning_validation.py --db .data/gex_data.db --symbol SPX

This module deliberately does not import the application, ORM models, or collector.
It neither changes trading signals nor writes labels into the source database.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


METHODS = ("oi_concentration", "recent_activity", "abs_net_oi_proxy", "nearest_strike")
DISTANCE_BUCKETS = ((0.0, 0.25), (0.25, 0.5), (0.5, 1.0))


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True, help="Explicit path to this app's SQLite database (opened read-only)")
    parser.add_argument("--symbol", default="SPX")
    parser.add_argument("--include-samples", action="store_true", help="Include per-snapshot selected strikes and outcome observations")
    args = parser.parse_args(argv)
    try:
        result = evaluate_database(args.db, args.symbol, include_samples=args.include_samples)
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc)}))
        return 1
    print(json.dumps(result, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
