"""Pure estimated option hedge-pressure calculations.

The calculator deliberately accepts plain observation mappings.  It does not
read a database and it does not claim to observe dealer/customer identity.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from observation_intervals import (
    contract_identity,
    cumulative_volume,
    option_side,
    row_mapping,
)


MODEL = "estimated_hedge_pressure_v1"
MAX_GAP_SECONDS = 120.0
MAX_FUTURE_QUOTE_SKEW_SECONDS = 5.0
MIN_CLASSIFIED_RATIO = 0.30
READY_CLASSIFIED_RATIO = 0.70
DISCLAIMER = "Estimated from option volume + quotes; dealer/customer identity is not observed."
TRADE_CAVEAT = "A snapshot's last trade cannot classify every trade between polls."
OI_CAVEAT = "Pressure does not infer opening/closing activity or changes in open interest."
BUCKET_NAMES = ("call_buy", "call_sell", "put_buy", "put_sell")


def _mapping(value: Any) -> dict[str, Any]:
    return row_mapping(value)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _integer_volume(value: Any) -> int | None:
    result = cumulative_volume(value, require_integral=True)
    return int(result) if result is not None else None


def _time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    return result


def _seconds_between(later: datetime, earlier: datetime) -> float | None:
    if (later.tzinfo is None) != (earlier.tzinfo is None):
        return None
    if later.tzinfo is not None:
        later = later.astimezone(timezone.utc)
        earlier = earlier.astimezone(timezone.utc)
    return (later - earlier).total_seconds()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        return value.isoformat()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _minute_end(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0) + timedelta(minutes=1)


def _display_time(value: datetime, timezone_name: str) -> str:
    if value.tzinfo is None:
        return value.isoformat()
    return value.astimezone(ZoneInfo(timezone_name)).replace(tzinfo=None).isoformat()


def _identity(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    return contract_identity(row)


def _side(row: Mapping[str, Any]) -> str | None:
    shared = option_side(row, lowercase=True)
    if shared:
        return shared
    value = str(row.get("option_type") or row.get("side") or "").upper()
    return "call" if value == "C" else "put" if value == "P" else None


def _row_spot(row: Mapping[str, Any]) -> float | None:
    for name in ("underlying_price", "spot_price", "spot"):
        value = _number(row.get(name))
        if value is not None:
            return value
    return None


def _config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(config or {})
    if isinstance(source.get("option_research"), Mapping):
        source = {**source, **dict(source["option_research"])}
    periods = source.get("ema_periods")
    first_default = periods[0] if isinstance(periods, (list, tuple)) and len(periods) >= 2 else 9
    second_default = periods[1] if isinstance(periods, (list, tuple)) and len(periods) >= 2 else 21
    first = int(source.get("pressure_ema_1", source.get("ema_1", first_default)))
    second = int(source.get("pressure_ema_2", source.get("ema_2", second_default)))
    if first <= 0 or second <= 0:
        raise ValueError("Pressure EMA periods must be positive integers.")
    return {
        "ema_periods": [first, second],
        "observation_timezone": str(source.get("observation_timezone", "America/New_York")),
        "minimum_classified_ratio": MIN_CLASSIFIED_RATIO,
    }


def _fingerprint(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16]


def unavailable_estimated_pressure(
    *, symbol: str = "", as_of: Any = None, config: Mapping[str, Any] | None = None,
    warning: str = "Pressure observations are unavailable.",
) -> dict[str, Any]:
    resolved = _config(config)
    when = _time(as_of)
    return {
        "model": MODEL,
        "status": "unavailable",
        "symbol": str(symbol).upper(),
        "session": when.date().isoformat() if when else None,
        "session_date": when.date().isoformat() if when else None,
        "as_of": str(as_of) if as_of is not None else None,
        "available_at": _utc_text(when) if when else None,
        "config": resolved,
        "config_fingerprint": _fingerprint(resolved),
        "points": [],
        "provisional": None,
        "current": None,
        "coverage": {
            "observed_minutes": 0,
            "usable_minutes": 0,
            "classified_volume": 0,
            "classified_contract_volume": 0,
            "neutral_volume": 0,
            "neutral_contract_volume": 0,
            "unclassified_volume": 0,
            "unclassified_contract_volume": 0,
            "classification_ratio": None,
            "valid_intervals": 0,
            "discarded_intervals": 0,
            "discarded_by_reason": {},
            "missing_clock_intervals": 0,
            "missing_clock_count": 0,
            "max_gap_seconds": None,
        },
        "warnings": [DISCLAIMER, TRADE_CAVEAT, OI_CAVEAT, warning],
    }


def _empty_bucket() -> dict[str, Any]:
    return {
        "classified_volume": 0,
        "delta_shares": 0.0,
        "delta_dollars": 0.0,
        "gamma_activity": 0.0,
    }


def _empty_minute() -> dict[str, Any]:
    return {
        "valid_intervals": 0,
        "zero_activity_intervals": 0,
        "classified_volume": 0,
        "neutral_volume": 0,
        "unclassified_volume": 0,
        "impulse_shares": 0.0,
        "impulse_dollars": 0.0,
        "buckets": {name: _empty_bucket() for name in BUCKET_NAMES},
        "exclusions": Counter(),
    }


def _quote_classification(
    row: Mapping[str, Any], left_time: datetime, right_time: datetime, volume_delta: int,
) -> tuple[str, dict[str, Any] | None]:
    side = _side(row)
    delta = _number(row.get("delta"))
    spot = _row_spot(row)
    bid = _number(row.get("bid"))
    ask = _number(row.get("ask"))
    last = _number(row.get("last_price", row.get("last")))
    if side is None:
        return "invalid_side", None
    if delta is None or delta < -1 or delta > 1 or (side == "call" and delta < 0) or (side == "put" and delta > 0):
        return "invalid_delta", None
    if spot is None or spot <= 0:
        return "invalid_spot", None
    if bid is None or ask is None or last is None or bid < 0 or ask <= bid or last < 0:
        return "invalid_quote", None

    clocks = []
    for name in ("bid_timestamp", "ask_timestamp", "last_timestamp"):
        clock = _time(row.get(f"{name}_utc", row.get(name)))
        if clock is None:
            return "missing_clock", None
        age = _seconds_between(right_time, clock)
        if age is None:
            return "missing_clock", None
        if age < -MAX_FUTURE_QUOTE_SKEW_SECONDS:
            return "future_quote", None
        if age > MAX_GAP_SECONDS:
            return "stale_quote", None
        clocks.append(clock)
    last_time = clocks[2]
    since_left = _seconds_between(last_time, left_time)
    after_right = _seconds_between(last_time, right_time)
    if since_left is None or after_right is None:
        return "missing_clock", None
    if since_left <= 0:
        return "last_not_in_interval", None
    if after_right > MAX_FUTURE_QUOTE_SKEW_SECONDS:
        return "future_last", None

    midpoint = (bid + ask) / 2.0
    half_spread = (ask - bid) / 2.0
    score = max(-1.0, min(1.0, (last - midpoint) / half_spread))
    shares = volume_delta * score * delta * 100.0
    dollars = shares * spot
    gamma = _number(row.get("gamma"))
    gamma_activity = None if gamma is None or gamma < 0 else volume_delta * gamma * spot * spot
    action = "buy" if score > 0 else "sell" if score < 0 else "neutral"
    return "classified", {
        "side": side,
        "action": action,
        "score": score,
        "shares": shares,
        "dollars": dollars,
        "gamma_activity": gamma_activity,
    }


def _aggregate_minute(
    data: Mapping[str, Any] | None, minute_end: datetime, cumulative_shares: float, cumulative_dollars: float,
    timezone_name: str,
) -> tuple[dict[str, Any], float, float]:
    if data is None or data["valid_intervals"] == 0:
        point = {
            "timestamp": _display_time(minute_end, timezone_name), "available_at": _utc_text(minute_end),
            "status": "unavailable", "usable": False,
            "impulse_shares": None, "impulse_dollars": None,
            "cumulative_shares": None, "cumulative_dollars": None,
            "classification_ratio": None, "classified_volume": 0, "neutral_volume": 0,
            "unclassified_volume": 0, "buckets": None, "coverage": {"valid_intervals": 0, "exclusions": {}},
        }
        return point, cumulative_shares, cumulative_dollars

    known = data["classified_volume"] + data["neutral_volume"] + data["unclassified_volume"]
    ratio = ((data["classified_volume"] + data["neutral_volume"]) / known) if known else None
    measured = bool(data["zero_activity_intervals"] or data["classified_volume"] or data["neutral_volume"])
    usable = bool(data["zero_activity_intervals"] and not known) or (ratio is not None and ratio >= MIN_CLASSIFIED_RATIO)
    numeric = measured
    if numeric:
        cumulative_shares += data["impulse_shares"]
        cumulative_dollars += data["impulse_dollars"]
    buckets = None
    if numeric:
        buckets = {}
        for name, bucket in data["buckets"].items():
            buckets[name] = dict(bucket)
    status = "ready" if numeric and (known == 0 or ratio == 1.0) else "partial" if numeric else "unavailable"
    point = {
        "timestamp": _display_time(minute_end, timezone_name), "available_at": _utc_text(minute_end),
        "status": status, "usable": usable,
        "impulse_shares": data["impulse_shares"] if numeric else None,
        "impulse_dollars": data["impulse_dollars"] if numeric else None,
        "cumulative_shares": cumulative_shares if numeric else None,
        "cumulative_dollars": cumulative_dollars if numeric else None,
        "classification_ratio": ratio,
        "classified_volume": data["classified_volume"],
        "neutral_volume": data["neutral_volume"],
        "unclassified_volume": data["unclassified_volume"],
        "buckets": buckets,
        "coverage": {"valid_intervals": data["valid_intervals"], "exclusions": dict(data["exclusions"])},
    }
    return point, cumulative_shares, cumulative_dollars


def _apply_ema(points: list[dict[str, Any]], periods: list[int]) -> None:
    states = {period: {"value": None, "run": 0} for period in periods}
    for point in points:
        for index, period in enumerate(periods):
            key = f"ema_{period}"
            state = states[period]
            value = point["cumulative_dollars"]
            if value is None or not point["usable"]:
                state["value"], state["run"] = None, 0
                point[key] = None
                continue
            alpha = 2.0 / (period + 1.0)
            state["value"] = value if state["value"] is None else alpha * value + (1.0 - alpha) * state["value"]
            state["run"] += 1
            point[key] = state["value"] if state["run"] >= period else None
            if index == 0:
                point["ema_fast"] = point[key]
            elif index == 1:
                point["ema_slow"] = point[key]
        # Null and unavailable points still carry the complete wire contract.
        point["ema_fast"] = point.get(f"ema_{periods[0]}")
        point["ema_slow"] = point.get(f"ema_{periods[1]}")
        point["emas"] = {str(period): point[f"ema_{period}"] for period in periods}


def _apply_slopes(points: list[dict[str, Any]]) -> None:
    for index, point in enumerate(points):
        point["impulse_5m"] = None
        point["slope_5m"] = None
        point["trend"] = "unavailable"
        if index < 5:
            continue
        window = points[index - 5:index + 1]
        if not all(item["usable"] and item["cumulative_dollars"] is not None for item in window):
            continue
        impulse = point["cumulative_dollars"] - window[0]["cumulative_dollars"]
        point["impulse_5m"] = impulse
        point["slope_5m"] = impulse / 5.0
        point["trend"] = "rising" if impulse > 0 else "falling" if impulse < 0 else "flat"


def build_estimated_pressure(
    observations: Iterable[Mapping[str, Any]], *, symbol: str, as_of: Any,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a causal completed-minute pressure series from flat snapshot rows."""
    display_end = _time(as_of)
    symbol = str(symbol).upper()
    if display_end is None:
        return unavailable_estimated_pressure(symbol=symbol, as_of=as_of, config=config, warning="A valid as-of time is required.")
    resolved = _config(config)
    prepared_rows = []
    for value in observations or []:
        row = _mapping(value)
        display_time = _time(row.get("timestamp", row.get("observed_at")))
        calculation_time = _time(row.get("available_at")) or display_time
        prepared_rows.append((row, display_time, calculation_time))
    uses_canonical_clock = any(value is not None and value.tzinfo is not None for _, _, value in prepared_rows)
    end = display_end
    if uses_canonical_clock and end.tzinfo is None:
        end = end.replace(tzinfo=ZoneInfo(resolved["observation_timezone"])).astimezone(timezone.utc)
    session_date = (
        display_end.date() if display_end.tzinfo is None
        else display_end.astimezone(ZoneInfo(resolved["observation_timezone"])).date()
    )
    rows: list[dict[str, Any]] = []
    snapshot_times: dict[int, datetime] = {}
    for row, display_time, timestamp in prepared_rows:
        try:
            sid = int(row.get("snapshot_id"))
        except (TypeError, ValueError, OverflowError):
            continue
        if timestamp is None or str(row.get("symbol") or symbol).upper() != symbol:
            continue
        until = _seconds_between(end, timestamp)
        row_session_date = display_time.date() if display_time is not None else (
            timestamp.astimezone(ZoneInfo(resolved["observation_timezone"])).date()
            if timestamp.tzinfo is not None else timestamp.date()
        )
        if until is None or until < 0 or row_session_date != session_date:
            continue
        row["_time"] = timestamp
        row["_snapshot_id"] = sid
        rows.append(row)
        snapshot_times[sid] = timestamp
    if len(snapshot_times) < 2:
        return unavailable_estimated_pressure(symbol=symbol, as_of=as_of, config=resolved, warning="At least two session snapshots are required.")

    by_snapshot: dict[int, dict[tuple[str, str, str], dict[str, Any]]] = defaultdict(dict)
    duplicate_keys: set[tuple[int, tuple[str, str, str]]] = set()
    for row in rows:
        identity = _identity(row)
        if identity is None:
            continue  # Snapshot header.
        sid = row["_snapshot_id"]
        if identity in by_snapshot[sid]:
            duplicate_keys.add((sid, identity))
        by_snapshot[sid][identity] = row
    for sid, identity in duplicate_keys:
        by_snapshot[sid].pop(identity, None)

    ordered = sorted((timestamp, sid) for sid, timestamp in snapshot_times.items())
    minute_data: dict[datetime, dict[str, Any]] = defaultdict(_empty_minute)
    discarded = Counter()
    valid_intervals = 0
    max_gap = None
    for (left_time, left_sid), (right_time, right_sid) in zip(ordered, ordered[1:]):
        gap = _seconds_between(right_time, left_time)
        if gap is not None:
            max_gap = gap if max_gap is None else max(max_gap, gap)
        if gap is None:
            discarded["clock_mismatch"] += 1
            continue
        if gap <= 0:
            discarded["non_increasing_snapshot_time"] += 1
            continue
        if gap > MAX_GAP_SECONDS:
            discarded["gap_over_120_seconds"] += 1
            continue
        left_rows, right_rows = by_snapshot[left_sid], by_snapshot[right_sid]
        identities = set(left_rows) | set(right_rows)
        for identity in identities:
            if (left_sid, identity) in duplicate_keys or (right_sid, identity) in duplicate_keys:
                discarded["duplicate_contract"] += 1
                continue
            left_row, right_row = left_rows.get(identity), right_rows.get(identity)
            if left_row is None or right_row is None:
                discarded["missing_endpoint"] += 1
                continue
            old_volume, new_volume = _integer_volume(left_row.get("volume")), _integer_volume(right_row.get("volume"))
            if old_volume is None or new_volume is None:
                discarded["invalid_volume"] += 1
                continue
            if new_volume < old_volume:
                discarded["volume_reset"] += 1
                continue
            volume_delta = new_volume - old_volume
            bucket_end = _minute_end(right_time)
            data = minute_data[bucket_end]
            data["valid_intervals"] += 1
            valid_intervals += 1
            if volume_delta == 0:
                data["zero_activity_intervals"] += 1
                continue
            reason, classified = _quote_classification(right_row, left_time, right_time, volume_delta)
            if classified is None:
                data["unclassified_volume"] += volume_delta
                data["exclusions"][reason] += 1
                discarded[reason] += 1
                continue
            if classified["action"] == "neutral":
                data["neutral_volume"] += volume_delta
                continue
            data["classified_volume"] += volume_delta
            data["impulse_shares"] += classified["shares"]
            data["impulse_dollars"] += classified["dollars"]
            bucket = data["buckets"][f"{classified['side']}_{classified['action']}"]
            bucket["classified_volume"] += volume_delta
            bucket["delta_shares"] += classified["shares"]
            bucket["delta_dollars"] += classified["dollars"]
            activity = classified["gamma_activity"]
            if activity is None:
                bucket["gamma_activity"] = None
            elif bucket["gamma_activity"] is not None:
                bucket["gamma_activity"] += activity

    if not minute_data:
        result = unavailable_estimated_pressure(symbol=symbol, as_of=as_of, config=resolved, warning="No valid adjacent observation intervals are available.")
        result["coverage"]["discarded_intervals"] = sum(discarded.values())
        result["coverage"]["discarded_by_reason"] = dict(discarded)
        result["coverage"]["max_gap_seconds"] = max_gap
        return result

    first_end, last_end = min(minute_data), max(minute_data)
    completed_ends = []
    cursor = first_end
    while cursor <= min(last_end, end):
        completed_ends.append(cursor)
        cursor += timedelta(minutes=1)
    points: list[dict[str, Any]] = []
    cumulative_shares = cumulative_dollars = 0.0
    for bucket_end in completed_ends:
        point, cumulative_shares, cumulative_dollars = _aggregate_minute(
            minute_data.get(bucket_end), bucket_end, cumulative_shares, cumulative_dollars,
            resolved["observation_timezone"],
        )
        points.append(point)
    _apply_ema(points, resolved["ema_periods"])
    _apply_slopes(points)

    provisional = None
    provisional_end = next((value for value in sorted(minute_data) if value > end), None)
    if provisional_end is not None:
        provisional, _, _ = _aggregate_minute(
            minute_data[provisional_end], provisional_end, cumulative_shares, cumulative_dollars,
            resolved["observation_timezone"],
        )
        provisional["status"] = "partial"
        provisional["usable"] = False
        provisional["provisional"] = True

    classified = sum(value["classified_volume"] for value in minute_data.values())
    neutral = sum(value["neutral_volume"] for value in minute_data.values())
    unclassified = sum(value["unclassified_volume"] for value in minute_data.values())
    known = classified + neutral + unclassified
    ratio = (classified + neutral) / known if known else None
    observed = sum(point["coverage"]["valid_intervals"] > 0 for point in points)
    usable = sum(bool(point["usable"]) for point in points)
    has_quality_exclusions = any(
        reason in discarded for reason in (
            "volume_reset", "gap_over_120_seconds", "missing_clock", "future_quote", "future_last",
            "stale_quote", "clock_mismatch", "non_increasing_snapshot_time", "missing_endpoint", "duplicate_contract",
        )
    )
    if observed < 2 or (known > 0 and (ratio is None or ratio < MIN_CLASSIFIED_RATIO)):
        status = "unavailable"
    elif ratio is None:
        status = "partial"
    elif ratio < READY_CLASSIFIED_RATIO or usable < 5 or has_quality_exclusions:
        status = "partial"
    else:
        status = "ready"
    warnings = [DISCLAIMER, TRADE_CAVEAT, OI_CAVEAT]
    if ratio is None:
        warnings.append("No traded volume was observed in valid intervals; numeric zero pressure is measured but classification coverage is not defined.")
    if has_quality_exclusions:
        warnings.append("Some observation or quote intervals were excluded; session pressure is incomplete.")
    if any(point["status"] == "unavailable" for point in points):
        warnings.append("Missing minutes are null gaps; cumulative pressure resumes from observed classified flow after each gap.")
    latest = next((point for point in reversed(points) if point["cumulative_dollars"] is not None), None)
    current = None
    if latest is not None:
        current = {
            "timestamp": latest["timestamp"],
            "cumulative_shares": latest["cumulative_shares"],
            "cumulative_dollars": latest["cumulative_dollars"],
            "slope_5m": latest["slope_5m"],
            "impulse_5m": latest["impulse_5m"],
            "trend": latest["trend"],
            "direction": latest["trend"],
            "relative_to_ema_fast": None if latest.get("ema_fast") is None else latest["cumulative_dollars"] - latest["ema_fast"],
            "relative_to_ema_slow": None if latest.get("ema_slow") is None else latest["cumulative_dollars"] - latest["ema_slow"],
        }
        if resolved["ema_periods"] == [15, 30]:
            current["relative_to_ema15"] = current["relative_to_ema_fast"]
            current["relative_to_ema30"] = current["relative_to_ema_slow"]
    return {
        "model": MODEL,
        "status": status,
        "symbol": symbol,
        "session": session_date.isoformat(),
        "session_date": session_date.isoformat(),
        "as_of": display_end.isoformat(),
        "available_at": _utc_text(end),
        "config": resolved,
        "config_fingerprint": _fingerprint(resolved),
        "points": points,
        "provisional": provisional,
        "current": current,
        "coverage": {
            "observed_minutes": observed,
            "usable_minutes": usable,
            "classified_volume": classified,
            "classified_contract_volume": classified,
            "neutral_volume": neutral,
            "neutral_contract_volume": neutral,
            "unclassified_volume": unclassified,
            "unclassified_contract_volume": unclassified,
            "classification_ratio": ratio,
            "valid_intervals": valid_intervals,
            "discarded_intervals": sum(discarded.values()),
            "discarded_by_reason": dict(discarded),
            "missing_clock_intervals": discarded.get("missing_clock", 0),
            "missing_clock_count": discarded.get("missing_clock", 0),
            "max_gap_seconds": max_gap,
        },
        "warnings": warnings,
    }

