from math import exp, isfinite, log, pi
from statistics import NormalDist


NORMAL = NormalDist()
CONTRACT_MULTIPLIER = 100
ETP_SWEEP_STEP = 0.25
INDEX_SWEEP_STEP = 1.0
INDEX_SYMBOLS = {"SPX", "NDX"}


def _row_value(row, key, default=None):
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


def _normal_pdf(value):
    return (1 / (2 * pi) ** 0.5) * exp(-0.5 * value * value)


def _option_side(row):
    option_type = str(_row_value(row, "option_type", "") or "").upper()
    if "CALL" in option_type:
        return "CALL"
    if "PUT" in option_type:
        return "PUT"
    return None


def _infer_total_vol(row, current_spot):
    side = _option_side(row)
    if not side:
        return None, "missing_side"

    try:
        delta = float(_row_value(row, "delta"))
        gamma = float(_row_value(row, "gamma"))
        underlying = float(_row_value(row, "underlying_price", current_spot) or current_spot)
    except (TypeError, ValueError):
        return None, "invalid_greeks"

    if not all(isfinite(value) for value in (delta, gamma, underlying)):
        return None, "invalid_greeks"

    if gamma <= 0 or underlying <= 0:
        return None, "invalid_greeks"

    nd1 = delta if side == "CALL" else delta + 1
    if not isfinite(nd1) or not 0 < nd1 < 1:
        return None, "invalid_delta"

    d1 = NORMAL.inv_cdf(nd1)
    total_vol = _normal_pdf(d1) / (underlying * gamma)
    if not isfinite(total_vol) or total_vol <= 0:
        return None, "invalid_volatility"

    return {"d1": d1, "total_vol": total_vol, "underlying": underlying}, None


def _prepare_contract(row, current_spot):
    side = _option_side(row)
    implied, reason = _infer_total_vol(row, current_spot)
    if reason:
        return None, reason

    try:
        strike = float(_row_value(row, "strike_price"))
        open_interest = int(_row_value(row, "open_interest"))
    except (TypeError, ValueError):
        return None, "invalid_contract"

    if not isfinite(strike) or strike <= 0 or open_interest <= 0:
        return None, "invalid_contract"

    sign = 1 if side == "CALL" else -1
    return {
        "strike": strike,
        "open_interest": open_interest,
        "sign": sign,
        "d1": implied["d1"],
        "total_vol": implied["total_vol"],
        "underlying": implied["underlying"],
    }, None


def _contract_share_gamma(contract, spot):
    if spot <= 0:
        return 0.0
    total_vol = contract["total_vol"]
    d1 = contract["d1"] + (log(spot / contract["underlying"]) / total_vol)
    gamma = _normal_pdf(d1) / (spot * total_vol)
    return gamma * contract["open_interest"] * CONTRACT_MULTIPLIER * contract["sign"]


def _zero_crossings(points):
    crossings = []
    for index in range(1, len(points)):
        left = points[index - 1]
        right = points[index]
        left_value = left["net_gex"]
        right_value = right["net_gex"]
        if left_value == 0:
            crossings.append(left["spot"])
        elif (left_value < 0 <= right_value) or (left_value > 0 >= right_value):
            span = right_value - left_value
            if span == 0:
                crossings.append(right["spot"])
            else:
                ratio = abs(left_value) / abs(span)
                crossings.append(left["spot"] + ((right["spot"] - left["spot"]) * ratio))
    return crossings


def _build_spot_grid(min_spot, max_spot, current_spot, step):
    values = []
    cursor = min_spot
    precision = 2 if step < 1 else 0
    while cursor <= max_spot + (step * 0.5):
        values.append(round(cursor, precision))
        cursor += step

    values.append(round(current_spot, 4))
    return sorted({value for value in values if min_spot <= value <= max_spot})


def _integrate_share_gamma(points):
    if not points:
        return

    cumulative = 0.0
    points[0]["cumulative_gamma_area"] = 0.0
    for index in range(1, len(points)):
        previous = points[index - 1]
        current = points[index]
        width = current["spot"] - previous["spot"]
        cumulative += ((previous["net_share_gamma"] + current["net_share_gamma"]) / 2) * width
        current["cumulative_gamma_area"] = cumulative


def _interpolate_anchor(points, current_spot):
    for point in points:
        if abs(point["spot"] - current_spot) < 1e-9:
            return point["cumulative_gamma_area"]
    for index in range(1, len(points)):
        left = points[index - 1]
        right = points[index]
        if left["spot"] <= current_spot <= right["spot"]:
            span = right["spot"] - left["spot"]
            if span <= 0:
                return left["cumulative_gamma_area"]
            ratio = (current_spot - left["spot"]) / span
            return left["cumulative_gamma_area"] + (
                (right["cumulative_gamma_area"] - left["cumulative_gamma_area"]) * ratio
            )
    return 0.0


def build_gamma_sweep(profile_rows, spot, symbol):
    try:
        current_spot = float(spot or 0)
    except (TypeError, ValueError):
        current_spot = 0

    if current_spot <= 0:
        return {"status": "unavailable", "reason": "Missing current spot", "points": []}

    raw_rows = list(profile_rows or [])
    if not raw_rows:
        return {"status": "unavailable", "reason": "No option rows", "points": []}

    contracts = []
    skipped = {}
    strikes = []
    for row in raw_rows:
        contract, reason = _prepare_contract(row, current_spot)
        if contract:
            contracts.append(contract)
            strikes.append(contract["strike"])
        else:
            skipped[reason] = skipped.get(reason, 0) + 1

    if not contracts or len(set(strikes)) < 2:
        return {
            "status": "unavailable",
            "reason": "No valid contracts for sweep",
            "model": "fixed inferred volatility from stored delta/gamma",
            "skipped_contracts": skipped,
            "points": [],
        }

    min_spot = min(strikes)
    max_spot = max(strikes)
    if not min_spot <= current_spot <= max_spot:
        return {
            "status": "unavailable",
            "reason": "Current spot is outside the collected strike range",
            "model": "fixed inferred volatility from stored delta/gamma",
            "range": {"min": min_spot, "max": max_spot},
            "skipped_contracts": skipped,
            "points": [],
        }

    step = INDEX_SWEEP_STEP if str(symbol or "").upper() in INDEX_SYMBOLS else ETP_SWEEP_STEP
    grid = _build_spot_grid(min_spot, max_spot, current_spot, step)
    points = []
    for sweep_spot in grid:
        net_share_gamma = sum(_contract_share_gamma(contract, sweep_spot) for contract in contracts)
        points.append(
            {
                "spot": sweep_spot,
                "net_share_gamma": net_share_gamma,
                "net_gex": net_share_gamma * sweep_spot * sweep_spot * 0.01,
            }
        )

    _integrate_share_gamma(points)
    anchor = _interpolate_anchor(points, current_spot)
    for point in points:
        point["hedge_shares"] = -(point["cumulative_gamma_area"] - anchor)
        point.pop("cumulative_gamma_area", None)
        point.pop("net_share_gamma", None)

    crossings = _zero_crossings(points)
    below = [value for value in crossings if value < current_spot]
    above = [value for value in crossings if value >= current_spot]
    current_point = min(points, key=lambda point: abs(point["spot"] - current_spot))

    return {
        "status": "ok",
        "model": "fixed inferred volatility from stored delta/gamma",
        "range": {"min": min_spot, "max": max_spot, "step": step},
        "current": {
            "spot": current_spot,
            "net_gex": current_point["net_gex"],
            "hedge_shares": current_point["hedge_shares"],
        },
        "zero_crossings": {
            "all": crossings,
            "below": below[-1] if below else None,
            "above": above[0] if above else None,
        },
        "skipped_contracts": skipped,
        "points": points,
    }
