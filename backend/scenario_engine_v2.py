"""Shadow scenario engine based on structural levels and observed price motion."""

from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta

from levels import build_levels
from market_context import stored_timestamp_to_eastern


DATA_QUALITY_MIN = 0.45  # Fraction of required inputs.
PIN_START_ET_HOUR = 14
PIN_START_ET_MINUTE = 30
PIN_MAX_DISTANCE_IMPLIED = 0.25  # Fraction of one implied move.
PIN_MIN_GROSS_SHARE = 0.15  # Fraction of gross GEX within 1% of spot.
PIN_INVALIDATION_IMPLIED = 0.5  # Fraction of one implied move on each side.
FADE_MIN_RANGE_CONSUMED = 0.6  # Fraction of one implied move.
FADE_INVALIDATION_IMPLIED = 0.25  # Fraction of one implied move beyond the wall.
ZONE_MIN_IMPLIED = 0.1  # Fraction of one implied move.
EXPANSION_RETURN_VOL_MULTIPLIER = 0.25  # 15-minute return divided by 15-minute realized volatility.
MIN_REWARD_RISK = 1.0  # Target distance divided by invalidation distance; directional setups only.
MOMENTUM_LOOKBACK_MINUTES = 15  # Minutes of observed price history.


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def _timestamp(value):
    if isinstance(value, datetime):
        return value
    if value:
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            pass
    return None


def _return_15m(history: list[dict], as_of: datetime, spot: float):
    cutoff = as_of - timedelta(minutes=MOMENTUM_LOOKBACK_MINUTES)
    candidates = []
    for row in history:
        stamp = _timestamp(row.get("timestamp"))
        price = _finite(row.get("spot_price"))
        if stamp is not None and price is not None and price > 0:
            candidates.append((stored_timestamp_to_eastern(stamp), price))
    past = [item for item in candidates if item[0] <= cutoff]
    return math.log(spot / max(past, key=lambda item: item[0])[1]) if past and spot > 0 else None


def _directional_result(kind, bias, spot, target, invalidation, levels, warnings, reasons):
    if target is None or invalidation is None:
        return None, "target or invalidation unavailable"
    risk = abs(spot - invalidation)
    reward = abs(target - spot)
    if risk <= 0 or reward / risk < MIN_REWARD_RISK:
        return None, "reward-to-risk below 1.0"
    if bias == "CALL" and not (target > spot > invalidation):
        return None, "directional levels are not ordered around spot"
    if bias == "PUT" and not (target < spot < invalidation):
        return None, "directional levels are not ordered around spot"
    return {
        "scenario_type": kind, "bias": bias, "target": target, "invalidation": invalidation,
        "reward_to_risk": reward / risk, "reasons": reasons, "warnings": warnings,
    }, None


def build_scenario_workspace_v2(snapshot: dict, profile: list[dict], market_context: dict,
                                data_quality: dict, *, gamma_sweep=None, spot_history=None) -> dict:
    symbol = str(snapshot.get("symbol") or "").upper()
    spot = _finite(snapshot.get("spot_price"))
    net_gex = _finite(snapshot.get("total_net_gex"))
    levels = build_levels(snapshot, profile, gamma_sweep, market_context.get("implied_move"))
    zero = levels["zero_gamma"]
    move = levels["implied_move"]
    quality = _finite(data_quality.get("score")) or 0.0
    warnings = list(data_quality.get("warnings") or []) + list(market_context.get("warnings") or []) + levels["warnings"]
    warnings.append("Dealer direction is unknown; signed GEX is an open-interest proxy")
    blockers = []
    if spot is None or net_gex is None or not profile or zero is None:
        blockers.append("required snapshot/profile or zero-gamma inputs unavailable")
    if move is None or move <= 0:
        blockers.append("implied move unavailable")
    if quality < DATA_QUALITY_MIN:
        blockers.append("data quality below 45%")
    if (market_context.get("event_risk") or {}).get("state") == "BLOCKED":
        blockers.append("blocked event window")

    regime = "transition"
    if spot is not None and zero is not None and net_gex is not None:
        regime = "positive" if spot > zero and net_gex > 0 else "negative" if spot < zero and net_gex < 0 else "transition"
    if regime == "transition":
        blockers.append("transition gamma regime")

    scenario = None
    as_of = _timestamp(snapshot.get("timestamp")) or datetime.now()
    et_time = stored_timestamp_to_eastern(as_of).time()
    if not blockers:
        spacing = levels["strike_spacing"]
        zone_width = max(spacing / 2 if spacing else 0.0, ZONE_MIN_IMPLIED * move)
        pin = levels["pin_strike"]
        if (regime == "positive" and et_time >= datetime(2000, 1, 1, PIN_START_ET_HOUR, PIN_START_ET_MINUTE).time()
                and pin is not None and abs(pin - spot) <= PIN_MAX_DISTANCE_IMPLIED * move
                and (levels["pin_gross_share_near_spot"] or 0) >= PIN_MIN_GROSS_SHARE):
            band = PIN_INVALIDATION_IMPLIED * move
            scenario = {"scenario_type": "PIN", "bias": "NEUTRAL", "target": pin,
                        "invalidation": None, "pin_band_low": pin - band, "pin_band_high": pin + band,
                        "reward_to_risk": None, "reasons": ["positive regime and late-session pin concentration"],
                        "warnings": warnings}
        elif regime == "positive":
            range_consumed = _finite(market_context.get("range_consumed"))
            if range_consumed is not None and range_consumed >= FADE_MIN_RANGE_CONSUMED:
                call = levels["call_wall"]
                put = levels["put_wall"]
                if call is not None and abs(spot - call) <= zone_width:
                    scenario, reason = _directional_result("FADE_CALL_WALL", "PUT", spot, pin or zero,
                                                           call + FADE_INVALIDATION_IMPLIED * move, levels, warnings,
                                                           ["positive regime; call wall tested after range consumption"])
                    if reason:
                        blockers.append(reason)
                elif put is not None and abs(spot - put) <= zone_width:
                    scenario, reason = _directional_result("FADE_PUT_WALL", "CALL", spot, pin or zero,
                                                           put - FADE_INVALIDATION_IMPLIED * move, levels, warnings,
                                                           ["positive regime; put wall tested after range consumption"])
                    if reason:
                        blockers.append(reason)
        else:
            realized = _finite(market_context.get("realized_volatility_15m"))
            observed_return = _return_15m(spot_history or [], stored_timestamp_to_eastern(as_of), spot)
            if realized is None or observed_return is None:
                blockers.append("15-minute price return or realized volatility unavailable")
            elif realized > 0 and abs(observed_return) >= EXPANSION_RETURN_VOL_MULTIPLIER * realized:
                if observed_return < 0:
                    choices = [strike for strike in [levels["put_wall"], *levels["top_five_gross_strikes"]] if strike is not None and strike < spot]
                    scenario, reason = _directional_result("DOWNSIDE_EXPANSION", "PUT", spot, max(choices) if choices else None,
                                                           zero, levels, warnings, ["negative regime and falling 15-minute price"])
                else:
                    choices = [strike for strike in [levels["call_wall"], *levels["top_five_gross_strikes"]] if strike is not None and strike > spot]
                    invalidation = zero if zero < spot else levels["put_wall"]
                    scenario, reason = _directional_result("UPSIDE_EXPANSION", "CALL", spot, min(choices) if choices else None,
                                                           invalidation, levels, warnings, ["negative regime and rising 15-minute price"])
                if reason:
                    blockers.append(reason)
    if scenario is None:
        blockers.append("no eligible structural setup" if not blockers else "")
        blockers = [value for value in blockers if value]
        scenario = {"scenario_type": "NO_TRADE", "bias": "WAIT", "target": None, "invalidation": None,
                    "reward_to_risk": None, "reasons": blockers, "warnings": warnings}

    scenario_id = "scn2_" + hashlib.sha256(f"{symbol}:{snapshot.get('id')}:{scenario['scenario_type']}".encode()).hexdigest()[:16]
    active = {
        "scenario_id": scenario_id, "scenario_type": scenario["scenario_type"],
        "eligible": scenario["scenario_type"] != "NO_TRADE", "bias": scenario["bias"],
        "target": scenario["target"], "invalidation": scenario["invalidation"],
        "pin_band_low": scenario.get("pin_band_low"), "pin_band_high": scenario.get("pin_band_high"),
        "reward_to_risk": scenario["reward_to_risk"], "reasons": scenario["reasons"], "warnings": warnings,
    }
    return {"engine": "scenario_v2", "symbol": symbol, "snapshot_id": snapshot.get("id"),
            "as_of": str(snapshot.get("timestamp")), "regime": regime, "levels": levels,
            "dashboard": {"snapshot": snapshot}, "data_quality": data_quality,
            "market_context": market_context,
            "active_scenario": active, "decision": {"bias": active["bias"], "target": active["target"],
            "invalidation": active["invalidation"], "pin_band_low": active["pin_band_low"],
            "pin_band_high": active["pin_band_high"], "scenario_type": active["scenario_type"]},
            "eligibility": {"eligible": active["eligible"], "blockers": blockers}}
