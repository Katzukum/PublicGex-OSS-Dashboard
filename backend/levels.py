"""Structural 0DTE levels for the shadow scenario engine."""

from __future__ import annotations

import math
from collections import defaultdict
from statistics import median


def _finite(value):
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def build_levels(snapshot: dict, profile: list[dict], gamma_sweep: dict | None, implied_move) -> dict:
    spot = _finite(snapshot.get("spot_price"))
    move = _finite(implied_move)
    by_strike = defaultdict(lambda: {"call": 0.0, "put": 0.0, "gross": 0.0})
    for row in profile:
        strike = _finite(row.get("strike_price", row.get("strike")))
        gex = _finite(row.get("gex_value"))
        if strike is None or gex is None:
            continue
        side = str(row.get("option_type") or "").upper()
        if "CALL" in side:
            by_strike[strike]["call"] += gex
        elif "PUT" in side:
            by_strike[strike]["put"] += gex
        by_strike[strike]["gross"] += abs(gex)

    strikes = sorted(by_strike)
    spacings = [right - left for left, right in zip(strikes, strikes[1:]) if right > left]
    spacing = median(spacings) if spacings else None
    call_wall = max((strike for strike in strikes if spot is not None and strike >= spot),
                    key=lambda strike: by_strike[strike]["call"], default=None)
    put_wall = max((strike for strike in strikes if spot is not None and strike <= spot),
                   key=lambda strike: abs(by_strike[strike]["put"]), default=None)
    if call_wall is not None and by_strike[call_wall]["call"] <= 0:
        call_wall = None
    if put_wall is not None and by_strike[put_wall]["put"] == 0:
        put_wall = None

    pin_candidates = [strike for strike in strikes if spot is not None and move is not None and move > 0 and abs(strike - spot) <= move / 2]
    pin = max(pin_candidates, key=lambda strike: by_strike[strike]["gross"], default=None)
    near_gross = sum(value["gross"] for strike, value in by_strike.items() if spot is not None and abs(strike - spot) <= spot * 0.01)
    pin_share = (by_strike[pin]["gross"] / near_gross
                 if pin is not None and spot is not None and abs(pin - spot) <= spot * 0.01 and near_gross > 0 else None)

    crossings = ((gamma_sweep or {}).get("zero_crossings") or {}).get("all") or []
    finite_crossings = [value for raw in crossings if (value := _finite(raw)) is not None]
    if spot is not None and (gamma_sweep or {}).get("status") == "ok" and finite_crossings:
        zero_gamma = min(finite_crossings, key=lambda value: abs(value - spot))
        zero_quality = "modeled"
        warnings = []
    else:
        zero_gamma = _finite(snapshot.get("flip_strike"))
        zero_quality = "proxy" if zero_gamma is not None else "unavailable"
        warnings = ["Modeled zero-gamma sweep unavailable; cumulative crossing is a proxy"] if zero_gamma is not None else ["Zero-gamma level unavailable"]

    ranked_gross = sorted(strikes, key=lambda strike: by_strike[strike]["gross"], reverse=True)[:5]
    return {
        "zero_gamma": zero_gamma,
        "zero_gamma_quality": zero_quality,
        "call_wall": call_wall,
        "put_wall": put_wall,
        "pin_strike": pin,
        "pin_gross_share_near_spot": pin_share,
        "implied_move": move,
        "strike_spacing": spacing,
        "top_five_gross_strikes": ranked_gross,
        "warnings": warnings,
    }
