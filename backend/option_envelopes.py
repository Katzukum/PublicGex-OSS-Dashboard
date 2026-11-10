"""Deterministic observational Option Envelopes."""

from __future__ import annotations

from collections import Counter, defaultdict
from math import isfinite
from typing import Any

from gamma_sweep import build_gamma_sweep


MODEL = "publicgex_envelopes_v1"
SOURCES = {"volume_gamma": "volume", "oi_gamma": "open_interest"}
BOUNDARY_FRACTION = 0.10


def _value(row: Any, key: str, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    if hasattr(row, "_mapping"):
        return row._mapping.get(key, default)
    return getattr(row, key, default)


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _side(row: Any) -> str | None:
    value = str(_value(row, "option_type", "") or "").upper()
    return "CALL" if "CALL" in value else "PUT" if "PUT" in value else None


def _near_outer_boundary(level: float | None, bounds: dict[str, float] | None) -> bool:
    if level is None or not bounds:
        return False
    lower, upper = bounds["min"], bounds["max"]
    width = upper - lower
    if width <= 0:
        return True
    margin = width * BOUNDARY_FRACTION
    return level <= lower + margin or level >= upper - margin


def _base(source: str, symbol: str | None, as_of: Any) -> dict[str, Any]:
    return {
        "model": MODEL,
        "source": source,
        "symbol": str(symbol or "").upper() or None,
        "status": "unavailable",
        "as_of": str(as_of) if as_of is not None else None,
        "zero_gamma": None,
        "major_gamma": None,
        "minor_gamma": None,
        "major_strength": None,
        "minor_strength": None,
        "coverage": {
            "contracts": 0,
            "usable_contracts": 0,
            "excluded_contracts": 0,
            "excluded_by_reason": {},
            "sweep_usable_contracts": 0,
            "min_strike": None,
            "max_strike": None,
            "collected_range": None,
            "modeled_range": None,
            "boundary_flags": {
                "major_gamma": False,
                "minor_gamma": False,
                "zero_gamma": False,
            },
        },
        "warnings": [],
    }


def unavailable_option_envelopes(
    *, source: str = "volume_gamma", symbol: str | None = None, as_of: Any = None,
    warning: str = "Option Envelopes are unavailable."
) -> dict[str, Any]:
    if source not in SOURCES:
        raise ValueError("source must be 'volume_gamma' or 'oi_gamma'")
    result = _base(source, symbol, as_of)
    result["warnings"].append(warning)
    return result


def build_option_envelopes(
    rows,
    *,
    spot,
    source: str = "volume_gamma",
    symbol: str | None = None,
    as_of: Any = None,
) -> dict[str, Any]:
    """Return Major, Minor, and modeled Zero Gamma envelope levels.

    Concentration uses call-positive/put-negative gamma weights.  The modeled
    zero delegates to the existing fixed-volatility Gamma Sweep with the same
    selected weight, then chooses the in-range crossing nearest current spot.
    """
    if source not in SOURCES:
        raise ValueError("source must be 'volume_gamma' or 'oi_gamma'")
    result = _base(source, symbol, as_of)
    raw_rows = list(rows or [])
    result["coverage"]["contracts"] = len(raw_rows)
    current_spot = _number(spot)
    if current_spot is None or current_spot <= 0:
        result["coverage"]["excluded_contracts"] = len(raw_rows)
        result["warnings"].append("A finite positive spot is required.")
        return result
    if not raw_rows:
        result["warnings"].append("No option rows are available.")
        return result

    weight_field = SOURCES[source]
    contributions: dict[float, float] = defaultdict(float)
    usable_rows: list[Any] = []
    excluded: Counter[str] = Counter()
    for row in raw_rows:
        side = _side(row)
        strike = _number(_value(row, "strike_price"))
        gamma = _number(_value(row, "gamma"))
        weight = _number(_value(row, weight_field))
        if side is None:
            excluded["unrecognized_side"] += 1
        elif strike is None or strike <= 0:
            excluded["invalid_strike"] += 1
        elif gamma is None or gamma < 0:
            excluded["invalid_gamma"] += 1
        elif weight is None or weight < 0:
            excluded["invalid_weight"] += 1
        else:
            sign = 1 if side == "CALL" else -1
            contributions[strike] += sign * weight * gamma * current_spot * current_spot
            usable_rows.append(row)

    coverage = result["coverage"]
    coverage["usable_contracts"] = len(usable_rows)
    coverage["excluded_contracts"] = len(raw_rows) - len(usable_rows)
    coverage["excluded_by_reason"] = dict(sorted(excluded.items()))
    if usable_rows:
        strikes = sorted(contributions)
        collected_range = {"min": strikes[0], "max": strikes[-1]}
        coverage["min_strike"] = strikes[0]
        coverage["max_strike"] = strikes[-1]
        coverage["collected_range"] = collected_range
    else:
        collected_range = None

    ranked = sorted(
        (
            {"strike": strike, "strength": abs(contribution)}
            for strike, contribution in contributions.items()
            if contribution != 0
        ),
        key=lambda item: (-item["strength"], abs(item["strike"] - current_spot), item["strike"]),
    )
    if ranked:
        result["major_gamma"] = ranked[0]["strike"]
        result["major_strength"] = ranked[0]["strength"]
    if len(ranked) > 1:
        result["minor_gamma"] = ranked[1]["strike"]
        result["minor_strength"] = ranked[1]["strength"]

    sweep = build_gamma_sweep(
        raw_rows, current_spot, symbol, weight_field=weight_field
    )
    skipped = sum((sweep.get("skipped_contracts") or {}).values())
    coverage["sweep_usable_contracts"] = max(0, len(raw_rows) - skipped)
    sweep_range = sweep.get("range")
    if sweep_range and sweep_range.get("min") is not None and sweep_range.get("max") is not None:
        coverage["modeled_range"] = {
            "min": sweep_range["min"],
            "max": sweep_range["max"],
            **({"step": sweep_range["step"]} if "step" in sweep_range else {}),
        }
    points = sweep.get("points") or []
    curve_is_indeterminate = bool(points) and all(
        abs(_number(point.get("net_gex")) or 0.0) <= 1e-12 for point in points
    )
    crossings = [] if curve_is_indeterminate else [
        value for value in (sweep.get("zero_crossings", {}).get("all", []) or [])
        if _number(value) is not None
    ]
    if crossings:
        result["zero_gamma"] = min(
            (float(value) for value in crossings),
            key=lambda value: (abs(value - current_spot), value),
        )

    flags = coverage["boundary_flags"]
    flags["major_gamma"] = _near_outer_boundary(result["major_gamma"], collected_range)
    flags["minor_gamma"] = _near_outer_boundary(result["minor_gamma"], collected_range)
    flags["zero_gamma"] = _near_outer_boundary(
        result["zero_gamma"], coverage["modeled_range"]
    )
    has_boundary_limitation = any(flags.values())

    if excluded:
        result["warnings"].append("Some contracts were excluded because required envelope inputs are invalid.")
    if result["major_gamma"] is None or result["minor_gamma"] is None:
        result["warnings"].append("Fewer than two nonzero net strike concentrations are available.")
    if curve_is_indeterminate:
        result["warnings"].append("The modeled gamma curve is identically zero, so Zero Gamma is indeterminate.")
    elif result["zero_gamma"] is None:
        result["warnings"].append(
            "No Zero Gamma crossing exists inside the modeled range; an outside crossing is unknown."
        )
    if flags["major_gamma"] or flags["minor_gamma"]:
        result["warnings"].append(
            "Envelope concentration is near the collected strike boundary; a stronger level may exist outside the stored range."
        )
    if flags["zero_gamma"]:
        result["warnings"].append("Zero Gamma is near the modeled sweep boundary.")

    levels = (result["zero_gamma"], result["major_gamma"], result["minor_gamma"])
    all_candidates_usable = (
        len(usable_rows) == len(raw_rows)
        and coverage["sweep_usable_contracts"] == len(raw_rows)
    )
    if all(value is not None for value in levels) and all_candidates_usable and not has_boundary_limitation:
        result["status"] = "ready"
    elif any(value is not None for value in levels):
        result["status"] = "partial"
    return result
