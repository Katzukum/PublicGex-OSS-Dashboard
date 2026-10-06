def _row_value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def option_side(option_type, gex_value=0):
    option_text = str(option_type or "").upper()
    if "CALL" in option_text:
        return "call"
    if "PUT" in option_text:
        return "put"
    return "put" if (gex_value or 0) < 0 else "call"


def build_gamma_level(strike, call_gex=0, put_gex=0, open_interest=0):
    strike = float(strike or 0)
    call_gex = float(call_gex or 0)
    put_gex = float(put_gex or 0)
    net_gex = call_gex + put_gex
    gross_gex = abs(call_gex) + abs(put_gex)
    dominant_side = "call" if abs(call_gex) >= abs(put_gex) else "put"
    dominant_gex = call_gex if dominant_side == "call" else put_gex
    level_type = "resistance" if net_gex > 0 else "support" if net_gex < 0 else (
        "resistance" if dominant_side == "call" else "support"
    )

    return {
        "strike": strike,
        "gex": net_gex,
        "net_gex": net_gex,
        "call_gex": call_gex,
        "put_gex": put_gex,
        "gross_gex": gross_gex,
        "dominant_side": dominant_side,
        "dominant_gex": dominant_gex,
        "level_strength": max(abs(net_gex), abs(call_gex), abs(put_gex)),
        "open_interest": int(open_interest or 0),
        "type": level_type,
    }


def aggregate_gamma_levels(rows, spot=0, per_side=5):
    buckets = {}

    for row in rows or []:
        strike = _row_value(row, "strike_price", 0) or 0
        gex_value = _row_value(row, "gex_value", 0) or 0
        open_interest = _row_value(row, "open_interest", 0) or 0
        try:
            strike = float(strike)
            gex_value = float(gex_value)
        except (TypeError, ValueError):
            continue

        if strike <= 0:
            continue

        bucket = buckets.setdefault(strike, {"call_gex": 0.0, "put_gex": 0.0, "open_interest": 0})
        if option_side(_row_value(row, "option_type", ""), gex_value) == "call":
            bucket["call_gex"] += gex_value
        else:
            bucket["put_gex"] += gex_value
        bucket["open_interest"] += int(open_interest or 0)

    levels = [
        build_gamma_level(strike, data["call_gex"], data["put_gex"], data["open_interest"])
        for strike, data in buckets.items()
    ]
    levels = [level for level in levels if level["strike"] > 0 and level["level_strength"] > 0]

    if per_side is not None:
        spot = float(spot or 0)
        below = [level for level in levels if spot <= 0 or level["strike"] < spot]
        above = [level for level in levels if spot > 0 and level["strike"] >= spot]
        levels = (
            sorted(below, key=lambda level: level["level_strength"], reverse=True)[:per_side]
            + sorted(above, key=lambda level: level["level_strength"], reverse=True)[:per_side]
        )

    return sorted(levels, key=lambda level: level["strike"])


def level_net_gex(level):
    return float(level.get("net_gex", level.get("gex", 0)) or 0)


def level_side_gex(level, sign):
    if sign > 0:
        if "call_gex" in level:
            return float(level.get("call_gex") or 0)
        return max(level_net_gex(level), 0)

    if "put_gex" in level:
        return float(level.get("put_gex") or 0)
    return min(level_net_gex(level), 0)


def level_strength(level):
    if "level_strength" in level:
        return float(level.get("level_strength") or 0)
    return max(
        abs(level_net_gex(level)),
        abs(float(level.get("call_gex") or 0)),
        abs(float(level.get("put_gex") or 0)),
    )
