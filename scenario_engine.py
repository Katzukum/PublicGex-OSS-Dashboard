"""Snapshot-consistent, server-side market scenario construction."""

from __future__ import annotations

import hashlib
from statistics import median


SCENARIO_TYPES = ("PIN_MEAN_REVERSION", "UPSIDE_EXPANSION", "DOWNSIDE_EXPANSION", "NO_TRADE")
DATA_QUALITY_THRESHOLD = 0.45


def _finite(value):
    try:
        number = float(value)
        return number if number == number else None
    except (TypeError, ValueError):
        return None


def _zone(center, width):
    return {"low": round(center - width, 4), "high": round(center + width, 4)}


def _spacing(profile):
    strikes = sorted({_finite(row.get("strike_price", row.get("strike"))) for row in profile})
    strikes = [value for value in strikes if value is not None]
    differences = [right - left for left, right in zip(strikes, strikes[1:]) if right > left]
    return median(differences) if differences else None


def _scenario_id(symbol, snapshot_id, scenario_type):
    raw = f"{symbol}:{snapshot_id}:{scenario_type}".encode("utf-8")
    return f"scn_{hashlib.sha256(raw).hexdigest()[:16]}"


def build_scenario_workspace(snapshot: dict, profile: list[dict], market_context: dict, data_quality: dict, *, market_score: float = 0.0, index_basket: dict | None = None) -> dict:
    symbol = str(snapshot.get("symbol") or "").upper()
    snapshot_id = snapshot.get("id") or snapshot.get("snapshot_id")
    spot = _finite(snapshot.get("spot_price"))
    flip = _finite(snapshot.get("flip_strike"))
    net_gex = _finite(snapshot.get("total_net_gex"))
    quality_score = _finite(data_quality.get("score")) or 0.0
    event_state = market_context.get("event_risk", {}).get("state", "NORMAL")
    spacing = _spacing(profile)
    implied_move = _finite(market_context.get("implied_move"))
    width_candidates = [value for value in ((spacing / 2 if spacing else None), (implied_move * 0.15 if implied_move else None), (spot * 0.0005 if spot else None)) if value is not None]
    zone_width = max(width_candidates) if width_candidates else 0.5

    blockers = []
    warnings = list(data_quality.get("warnings") or []) + list(market_context.get("warnings") or [])
    if spot is None or net_gex is None or not profile:
        blockers.append("required snapshot/profile inputs unavailable")
    if quality_score < DATA_QUALITY_THRESHOLD:
        blockers.append("data quality below 45%")
    if event_state == "BLOCKED":
        blockers.append("blocked event window")

    if blockers:
        scenario_type = "NO_TRADE"
    elif net_gex > 0 and flip is not None and abs(spot - flip) <= zone_width * 2:
        scenario_type = "PIN_MEAN_REVERSION"
    elif market_score > 0.20 and (flip is None or spot >= flip):
        scenario_type = "UPSIDE_EXPANSION"
    elif market_score < -0.20 and (flip is None or spot <= flip):
        scenario_type = "DOWNSIDE_EXPANSION"
    else:
        scenario_type = "NO_TRADE"
        blockers.append("directional inputs are not aligned")

    strikes = sorted({_finite(row.get("strike_price", row.get("strike"))) for row in profile})
    strikes = [value for value in strikes if value is not None]
    below = [value for value in strikes if spot is not None and value < spot]
    above = [value for value in strikes if spot is not None and value > spot]
    nearest_below = max(below) if below else None
    nearest_above = min(above) if above else None

    target = invalidation = trigger = None
    if scenario_type == "PIN_MEAN_REVERSION":
        target = flip
        invalidation = nearest_above if spot > flip else nearest_below
        trigger = flip
    elif scenario_type == "UPSIDE_EXPANSION":
        target = nearest_above
        invalidation = flip or nearest_below
        trigger = spot
    elif scenario_type == "DOWNSIDE_EXPANSION":
        target = nearest_below
        invalidation = flip or nearest_above
        trigger = spot
    if scenario_type != "NO_TRADE" and (target is None or invalidation is None):
        scenario_type = "NO_TRADE"
        blockers.append("target or invalidation zone unavailable")

    scenario_id = _scenario_id(symbol, snapshot_id, scenario_type)
    active = {
        "scenario_id": scenario_id,
        "scenario_type": scenario_type,
        "eligible": scenario_type != "NO_TRADE",
        "bias": "CALL" if scenario_type == "UPSIDE_EXPANSION" else "PUT" if scenario_type == "DOWNSIDE_EXPANSION" else "NEUTRAL",
        "trigger_zone": _zone(trigger, zone_width) if trigger is not None else None,
        "target_zone": _zone(target, zone_width) if target is not None else None,
        "invalidation_zone": _zone(invalidation, zone_width) if invalidation is not None else None,
        "reasons": blockers if blockers else ["snapshot, context, and directional gates passed"],
        "warnings": sorted(set(warnings)),
    }
    return {
        "active_scenario": active,
        "scenarios": [active],
        "eligibility": {"eligible": active["eligible"], "blockers": blockers, "event_state": event_state, "data_quality_threshold": DATA_QUALITY_THRESHOLD},
        "decision": {
            "score": round(float(market_score), 4),
            "bias": active["bias"] if active["eligible"] else "WAIT",
            "data_quality": data_quality,
            "scenario_id": scenario_id,
            "scenario_type": scenario_type,
            "target": target,
            "invalidation": invalidation,
            "flip": flip,
            "index_basket": index_basket or {},
            "market_vote": {"score": round(float(market_score), 4), "detail": "Data-quality-weighted Traders and Index Basket regime"},
            "dealer_vote": {"score": 0.35 if (net_gex or 0) > 0 else -0.35 if (net_gex or 0) < 0 else 0, "detail": "Current snapshot net gamma sign"},
            "liquidity_vote": {"score": 0, "detail": "Execution liquidity is graded per candidate"},
            "context": "; ".join(active["reasons"] + active["warnings"]),
        },
    }
