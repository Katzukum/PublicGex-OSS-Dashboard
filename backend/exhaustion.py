"""Pure, causal option-exhaustion candidate detection."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo


MODEL = "estimated_option_exhaustion_v1"
BAR_SOURCE = "collector_spot_observations"
BUCKETS = {
    "call_buy": ("CALL", "BUY", 1),
    "call_sell": ("CALL", "SELL", -1),
    "put_buy": ("PUT", "BUY", -1),
    "put_sell": ("PUT", "SELL", 1),
}
SENSITIVITY = {"low": 0.70, "medium": 0.80, "high": 0.90, "strong": 0.95}


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if hasattr(value, "_mapping"):
        return dict(value._mapping)
    return dict(value)


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _time(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None


def _seconds_between(later: datetime, earlier: datetime) -> float | None:
    if (later.tzinfo is None) != (earlier.tzinfo is None):
        return None
    if later.tzinfo is not None:
        later, earlier = later.astimezone(timezone.utc), earlier.astimezone(timezone.utc)
    return (later - earlier).total_seconds()


def _utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        return value.isoformat()
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _minute_end(value: datetime) -> datetime:
    return value.replace(second=0, microsecond=0) + timedelta(minutes=1)


def _config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(config or {})
    if isinstance(source.get("option_research"), Mapping):
        source = {**source, **dict(source["option_research"])}
    sensitivity = str(source.get("exhaustion_sensitivity", source.get("sensitivity", "high"))).lower()
    if sensitivity not in SENSITIVITY:
        raise ValueError("Exhaustion sensitivity must be low, medium, high, or strong.")
    period = int(source.get("exhaustion_period_bars", source.get("period_bars", 5)))
    reference = int(source.get("exhaustion_reference_minutes", source.get("reference_minutes", 60)))
    minimum_reference = int(source.get("exhaustion_minimum_reference", source.get("minimum_reference", 30)))
    cooldown = int(source.get("exhaustion_cooldown_minutes", source.get("cooldown_minutes", 5)))
    if period < 2 or reference < 1 or minimum_reference < 1 or minimum_reference > reference or cooldown < 0:
        raise ValueError("Exhaustion period, reference, warmup, and cooldown settings are invalid.")
    call_above = float(source.get("exhaustion_call_above", source.get("call_above", 0.70)))
    put_below = float(source.get("exhaustion_put_below", source.get("put_below", 0.30)))
    if not 0 <= put_below <= 1 or not 0 <= call_above <= 1:
        raise ValueError("Exhaustion price thresholds must be in [0, 1].")
    return {
        "sensitivity": sensitivity,
        "percentile_threshold": SENSITIVITY[sensitivity],
        "price_filter_enabled": bool(source.get("exhaustion_price_filter_enabled", source.get("price_filter_enabled", True))),
        "period_bars": period,
        "call_above": call_above,
        "put_below": put_below,
        "reference_minutes": reference,
        "minimum_reference": minimum_reference,
        "cooldown_minutes": cooldown,
        "envelope_source": str(source.get("envelope_source", "volume_gamma")),
        "observation_timezone": str(source.get("observation_timezone", "America/New_York")),
    }


def _fingerprint(config: Mapping[str, Any]) -> str:
    raw = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def _spot_price(row: Mapping[str, Any]) -> float | None:
    for key in ("price", "spot", "spot_price", "underlying_price", "close"):
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def build_observed_spot_bars(
    spot_observations: Iterable[Mapping[str, Any]], *, as_of: Any = None,
) -> list[dict[str, Any]]:
    """Construct completed observed OHLC bars, retaining singleton partial bars."""
    cutoff = _time(as_of) if as_of is not None else None
    samples: dict[datetime, dict[datetime, float]] = defaultdict(dict)
    for value in spot_observations or []:
        row = _mapping(value)
        timestamp = _time(row.get("available_at")) or _time(row.get("timestamp", row.get("observed_at")))
        price = _spot_price(row)
        if timestamp is None or price is None or price <= 0:
            continue
        if cutoff is not None:
            distance = _seconds_between(cutoff, timestamp)
            if distance is None or distance < 0:
                continue
        end = _minute_end(timestamp)
        if cutoff is not None:
            completion = _seconds_between(cutoff, end)
            if completion is None or completion < 0:
                continue
        samples[end][timestamp] = price
    bars = []
    for end in sorted(samples):
        ordered = sorted(samples[end].items())
        prices = [price for _, price in ordered]
        count = len(ordered)
        bars.append({
            "timestamp": end.isoformat(),
            "available_at": _utc_text(end),
            "open": prices[0], "high": max(prices), "low": min(prices), "close": prices[-1],
            "sample_count": count,
            "status": "ready" if count >= 2 else "partial",
            "eligible": count >= 2,
            "bar_source": BAR_SOURCE,
        })
    return bars


def _bucket_flow(point: Mapping[str, Any], name: str) -> float | None:
    buckets = point.get("buckets")
    if not isinstance(buckets, Mapping) or not isinstance(buckets.get(name), Mapping):
        return None
    return abs_value if (abs_value := _number(buckets[name].get("delta_dollars"))) is None else abs(abs_value)


def _level_value(container: Mapping[str, Any], name: str) -> float | None:
    for field in (f"{name}_level", f"{name}_gamma", name):
        value = container.get(field)
        if isinstance(value, Mapping):
            for key in ("level", "price", "strike"):
                result = _number(value.get(key))
                if result is not None:
                    return result
        else:
            result = _number(value)
            if result is not None:
                return result
    return None


def _envelope_context(
    history: list[dict[str, Any]], at: datetime, source: str, price: float,
) -> tuple[dict[str, Any] | None, bool, bool]:
    available = []
    for item in history:
        when = _time(item.get("available_at", item.get("as_of", item.get("timestamp"))))
        if when is None:
            continue
        delta = _seconds_between(at, when)
        if delta is not None and delta >= 0:
            available.append((when, item))
    if not available:
        return None, False, False
    when, item = max(available, key=lambda pair: pair[0])
    selected: Mapping[str, Any] = item
    source_key = "flow" if source == "volume_gamma" else "structural" if source == "oi_gamma" else source
    if isinstance(item.get(source_key), Mapping):
        selected = item[source_key]
    major, minor = _level_value(selected, "major"), _level_value(selected, "minor")
    threshold = price * 0.001
    major_near = major is not None and abs(price - major) <= threshold
    minor_near = minor is not None and abs(price - minor) <= threshold
    choices = [("major", major), ("minor", minor)]
    choices = [(name, level) for name, level in choices if level is not None]
    nearest = min(choices, key=lambda pair: (abs(price - pair[1]), pair[0])) if choices else None
    context = {
        "source": source,
        "available_at": _utc_text(when),
        "major_level": major,
        "minor_level": minor,
        "nearest_kind": nearest[0] if nearest else None,
        "nearest_level": nearest[1] if nearest else None,
        "distance_percent": abs(price - nearest[1]) / price if nearest else None,
    }
    return context, major_near, minor_near


def _price_position(
    bar_by_time: Mapping[datetime, Mapping[str, Any]], at: datetime, period: int,
) -> float | None:
    window = [bar_by_time.get(at - timedelta(minutes=offset)) for offset in reversed(range(period))]
    if not all(bar and bar.get("eligible") for bar in window):
        return None
    low = min(float(bar["low"]) for bar in window)
    high = max(float(bar["high"]) for bar in window)
    if high == low:
        return None
    return (float(window[-1]["close"]) - low) / (high - low)


def _prior_points(
    point_by_time: Mapping[datetime, Mapping[str, Any]], at: datetime, count: int,
) -> list[Mapping[str, Any]] | None:
    values = [point_by_time.get(at - timedelta(minutes=offset)) for offset in reversed(range(1, count + 1))]
    return values if all(value and value.get("usable") and _number(value.get("cumulative_dollars")) is not None for value in values) else None


def unavailable_exhaustion(
    *, symbol: str = "", as_of: Any = None, config: Mapping[str, Any] | None = None,
    warning: str = "Exhaustion observations are unavailable.",
) -> dict[str, Any]:
    resolved = _config(config)
    return {
        "model": MODEL, "status": "unavailable", "symbol": str(symbol).upper(),
        "session": None, "session_date": None, "as_of": str(as_of) if as_of is not None else None,
        "config": resolved, "config_fingerprint": _fingerprint(resolved),
        "events": [], "current": None,
        "coverage": {
            "completed_price_bars": 0, "eligible_price_bars": 0, "partial_price_bars": 0,
            "eligible_pressure_minutes": 0, "evaluated_bucket_checks": 0,
            "warmup_bucket_checks": 0, "suppressed_by_cooldown": 0,
        },
        "bar_source": BAR_SOURCE,
        "warnings": ["Events are inferred exhaustion candidates; scores are evidence scores, not reversal probabilities.", warning],
    }


def build_exhaustion_events(
    pressure: Mapping[str, Any], spot_observations: Iterable[Mapping[str, Any]],
    envelope_history: Iterable[Mapping[str, Any]], *, config: Mapping[str, Any] | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """Build causal exhaustion candidates from completed pressure and observed bars."""
    resolved = _config(config)
    raw_points = pressure.get("points")
    spot_rows = [dict(item) for item in spot_observations or [] if isinstance(item, Mapping)]
    cutoff = _time(as_of if as_of is not None else pressure.get("available_at", pressure.get("as_of")))
    aware_clock_present = False
    if isinstance(raw_points, list):
        aware_clock_present = any(
            (clock := _time(item.get("available_at", item.get("timestamp")))) is not None and clock.tzinfo is not None
            for item in raw_points if isinstance(item, Mapping)
        )
    if not aware_clock_present:
        aware_clock_present = any(
            (clock := _time(item.get("available_at", item.get("timestamp")))) is not None and clock.tzinfo is not None
            for item in spot_rows
        )
    if cutoff is not None and cutoff.tzinfo is None and aware_clock_present:
        cutoff = cutoff.replace(tzinfo=ZoneInfo(resolved["observation_timezone"])).astimezone(timezone.utc)
    symbol = str(pressure.get("symbol") or "").upper()
    if cutoff is None:
        return unavailable_exhaustion(symbol=symbol, as_of=as_of, config=resolved, warning="A valid as-of time is required.")
    if not isinstance(raw_points, list):
        return unavailable_exhaustion(symbol=symbol, as_of=cutoff, config=resolved, warning="A completed pressure series is required.")
    points: list[tuple[datetime, dict[str, Any]]] = []
    for raw in raw_points:
        if not isinstance(raw, Mapping):
            continue
        point = dict(raw)
        when = _time(point.get("available_at", point.get("timestamp")))
        if when is None:
            continue
        distance = _seconds_between(cutoff, when)
        if distance is not None and distance >= 0:
            points.append((when, point))
    points.sort(key=lambda pair: pair[0])
    if not points:
        return unavailable_exhaustion(symbol=symbol, as_of=cutoff, config=resolved, warning="No completed pressure points are available.")

    bars = build_observed_spot_bars(spot_rows, as_of=cutoff)
    bar_by_time = {_time(bar["available_at"]): bar for bar in bars}
    point_by_time = {when: point for when, point in points}
    history = [dict(item) for item in envelope_history or [] if isinstance(item, Mapping)]
    fingerprint = _fingerprint(resolved)
    events: list[dict[str, Any]] = []
    reference_values: dict[str, list[float]] = {name: [] for name in BUCKETS}
    cooldowns: dict[tuple[str, str, str], datetime] = {}
    evaluated = warmup = suppressed = 0
    session = str(pressure.get("session") or cutoff.date().isoformat())

    for at, point in points:
        current_bar = bar_by_time.get(at)
        current_ratio = _number(point.get("classification_ratio"))
        point_usable = bool(point.get("usable")) and current_ratio is not None and current_ratio >= 0.30
        for bucket_name, (side, action, direction) in BUCKETS.items():
            flow = _bucket_flow(point, bucket_name)
            refs = reference_values[bucket_name][-resolved["reference_minutes"]:]
            enough_refs = len(refs) >= resolved["minimum_reference"]
            if not enough_refs:
                warmup += 1
            elif point_usable and current_bar and current_bar.get("eligible") and flow is not None:
                evaluated += 1
            if not point_usable or current_bar is None or not current_bar.get("eligible") or flow is None or flow <= 0 or not enough_refs:
                if point.get("usable") and flow is not None:
                    reference_values[bucket_name].append(flow)
                continue

            percentile = sum(value <= flow for value in refs) / len(refs)
            if percentile < resolved["percentile_threshold"]:
                reference_values[bucket_name].append(flow)
                continue
            position = _price_position(bar_by_time, at, resolved["period_bars"])
            if resolved["price_filter_enabled"]:
                if position is None:
                    reference_values[bucket_name].append(flow)
                    continue
                if side == "CALL" and position < resolved["call_above"]:
                    reference_values[bucket_name].append(flow)
                    continue
                if side == "PUT" and position > resolved["put_below"]:
                    reference_values[bucket_name].append(flow)
                    continue

            previous_two = _prior_points(point_by_time, at, 2)
            pressure_turn = False
            flow_deceleration = False
            if previous_two:
                before, previous = previous_two
                c0, c1, c2 = (_number(before.get("cumulative_dollars")),
                              _number(previous.get("cumulative_dollars")),
                              _number(point.get("cumulative_dollars")))
                pressure_turn = bool(c0 is not None and c1 is not None and c2 is not None
                                     and direction * (c1 - c0) > 0 and direction * (c2 - c1) <= 0)
                f0, f1 = _bucket_flow(before, bucket_name), _bucket_flow(previous, bucket_name)
                flow_deceleration = bool(f0 is not None and f1 is not None and f0 > 0 and f1 > 0 and flow > 0
                                         and flow < f1 < f0)

            previous = point_by_time.get(at - timedelta(minutes=1))
            previous_c = _number(previous.get("cumulative_dollars")) if previous else None
            current_c = _number(point.get("cumulative_dollars"))
            previous_ema = _number(previous.get("ema_15", previous.get("ema_fast"))) if previous else None
            current_ema = _number(point.get("ema_15", point.get("ema_fast")))
            ema_reversal = bool(previous and previous.get("usable") and previous_c is not None and current_c is not None
                                and previous_ema is not None and current_ema is not None
                                and direction * (previous_c - previous_ema) > 0
                                and direction * (current_c - current_ema) <= 0)

            prior_five = _prior_points(point_by_time, at, 5)
            prior_bars = [bar_by_time.get(at - timedelta(minutes=offset)) for offset in reversed(range(1, 6))]
            divergence = False
            if prior_five and all(bar and bar.get("eligible") for bar in prior_bars) and current_c is not None:
                closes = [float(bar["close"]) for bar in prior_bars]
                current_close = float(current_bar["close"])
                if side == "CALL" and current_close > max(closes):
                    target_index = max(index for index, value in enumerate(closes) if value == max(closes))
                    prior_c = _number(prior_five[target_index].get("cumulative_dollars"))
                    divergence = prior_c is not None and current_c < prior_c
                elif side == "PUT" and current_close < min(closes):
                    target_index = max(index for index, value in enumerate(closes) if value == min(closes))
                    prior_c = _number(prior_five[target_index].get("cumulative_dollars"))
                    divergence = prior_c is not None and current_c > prior_c

            context, major_near, minor_near = _envelope_context(
                history, at, resolved["envelope_source"], float(current_bar["close"]),
            )
            weakening = pressure_turn or ema_reversal or flow_deceleration
            if not (weakening or divergence):
                reference_values[bucket_name].append(flow)
                continue
            location_component = position if side == "CALL" and position is not None else (
                1.0 - position if side == "PUT" and position is not None else 0.0
            )
            proximity = major_near or minor_near
            components = {
                "flow_extremity": percentile,
                "weakening": 1.0 if weakening else 0.0,
                "location": location_component,
                "divergence": 1.0 if divergence else 0.0,
                "proximity": 1.0 if proximity else 0.0,
            }
            score = (0.30 * components["flow_extremity"] + 0.25 * components["weakening"]
                     + 0.20 * components["location"] + 0.15 * components["divergence"]
                     + 0.10 * components["proximity"])
            cooldown_key = (session, side, action)
            previous_event = cooldowns.get(cooldown_key)
            if previous_event is not None:
                elapsed = _seconds_between(at, previous_event)
                if elapsed is not None and elapsed < resolved["cooldown_minutes"] * 60:
                    suppressed += 1
                    reference_values[bucket_name].append(flow)
                    continue
            raw_id = f"{MODEL}|{symbol}|{session}|{_utc_text(at)}|{side}|{action}|{fingerprint}"
            event_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
            evidence_flags = {
                "pressure_turn": pressure_turn,
                "pressure_price_divergence": divergence,
                "ema15_reversal": ema_reversal,
                "flow_deceleration": flow_deceleration,
                "major_gamma_proximity": major_near,
                "minor_gamma_proximity": minor_near,
            }
            availability = {
                "flow_extremity": True,
                "weakening": previous_two is not None or (previous_ema is not None and current_ema is not None),
                "location": position is not None,
                "divergence": prior_five is not None and all(bar and bar.get("eligible") for bar in prior_bars),
                "proximity": context is not None,
                "pressure_turn": previous_two is not None,
                "pressure_price_divergence": prior_five is not None and all(bar and bar.get("eligible") for bar in prior_bars),
                "ema15_reversal": previous_ema is not None and current_ema is not None,
                "flow_deceleration": previous_two is not None,
                "price_position": position is not None,
                "envelope_proximity": context is not None,
            }
            event = {
                "id": event_id, "model": MODEL, "symbol": symbol, "session": session,
                "timestamp": str(point.get("timestamp") or at.isoformat()), "available_at": _utc_text(at),
                "side": side, "action": action, "bucket": bucket_name,
                "label": f"Estimated {side.title()} {action.title()}", "estimated": True,
                "price": float(current_bar["close"]), "flow_dollars": flow,
                "flow_percentile": percentile, "price_position": position,
                "score": score, "components": components,
                "evidence": [name for name, enabled in evidence_flags.items() if enabled],
                "evidence_flags": evidence_flags,
                "availability": availability, "classification_ratio": current_ratio,
                "envelope": context, "nearest_envelope": context, "hedge_direction": direction,
                "bar_source": BAR_SOURCE, "config_fingerprint": fingerprint,
            }
            events.append(event)
            cooldowns[cooldown_key] = at
            reference_values[bucket_name].append(flow)

    eligible_bars = sum(bool(bar["eligible"]) for bar in bars)
    eligible_pressure = sum(bool(point.get("usable")) for _, point in points)
    if evaluated:
        status = "ready"
    elif bars and eligible_pressure:
        status = "partial"
    else:
        status = "unavailable"
    active = []
    for event in events:
        event_time = _time(event["available_at"])
        age = _seconds_between(cutoff, event_time) if event_time else None
        if age is not None and 0 <= age <= 5 * 60:
            active.append(event)
    current = None
    if active:
        latest_time = max(_time(event["available_at"]) for event in active)
        simultaneous = [event for event in active if _time(event["available_at"]) == latest_time]
        current = sorted(simultaneous, key=lambda event: (-event["score"], event["id"]))[0]
    warnings = [
        "Events are inferred exhaustion candidates; scores are evidence scores, not reversal probabilities.",
        "CALL and PUT identify range location; BUY and SELL are estimated option actions, not trading recommendations.",
    ]
    if eligible_bars < len(bars):
        warnings.append("Singleton observed-price bars are partial and cannot satisfy the price filter.")
    if not evaluated:
        warnings.append("The trailing flow reference or eligible observed-price history is still warming up.")
    if resolved["sensitivity"] == "strong":
        warnings.append("Strong uses a stricter flow-percentile threshold and therefore produces fewer candidate markers.")
    return {
        "model": MODEL, "status": status, "symbol": symbol, "session": session, "session_date": session,
        "as_of": cutoff.isoformat(), "available_at": _utc_text(cutoff),
        "config": resolved, "config_fingerprint": fingerprint,
        "events": events, "current": current,
        "coverage": {
            "completed_price_bars": len(bars), "eligible_price_bars": eligible_bars,
            "partial_price_bars": len(bars) - eligible_bars,
            "eligible_pressure_minutes": eligible_pressure,
            "evaluated_bucket_checks": evaluated, "warmup_bucket_checks": warmup,
            "suppressed_by_cooldown": suppressed,
        },
        "bar_source": BAR_SOURCE, "warnings": warnings,
    }

