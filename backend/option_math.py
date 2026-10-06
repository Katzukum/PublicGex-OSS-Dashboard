"""Shared option-math helpers for runtime views and historical backtests."""

from __future__ import annotations

from datetime import datetime, time
from math import exp, isfinite, pi
from statistics import NormalDist


NORMAL = NormalDist()
SECONDS_PER_YEAR = 365 * 24 * 60 * 60


def row_value(row, key, default=None):
    return row.get(key, default) if isinstance(row, dict) else getattr(row, key, default)


def normal_pdf(value):
    return (1 / (2 * pi) ** 0.5) * exp(-0.5 * value * value)


def option_side(row):
    value = str(row_value(row, "option_type", "") or "").upper()
    return "CALL" if "CALL" in value else "PUT" if "PUT" in value else None


def infer_total_vol(row, current_spot):
    side = option_side(row)
    if not side:
        return None, "missing_side"
    try:
        delta = float(row_value(row, "delta"))
        gamma = float(row_value(row, "gamma"))
        underlying = float(row_value(row, "underlying_price", current_spot) or current_spot)
    except (TypeError, ValueError):
        return None, "invalid_greeks"
    if not all(isfinite(value) for value in (delta, gamma, underlying)) or gamma <= 0 or underlying <= 0:
        return None, "invalid_greeks"
    nd1 = delta if side == "CALL" else delta + 1
    if not 0 < nd1 < 1:
        return None, "invalid_delta"
    d1 = NORMAL.inv_cdf(nd1)
    total_vol = normal_pdf(d1) / (underlying * gamma)
    if not isfinite(total_vol) or total_vol <= 0:
        return None, "invalid_volatility"
    return {"d1": d1, "total_vol": total_vol, "underlying": underlying}, None


def charm_per_year(row, spot: float, timestamp: datetime, settlement_time: time) -> float | None:
    implied, reason = infer_total_vol(row, spot)
    if reason:
        return None
    expiration_text = row_value(row, "expiration_date")
    try:
        expiration_date = datetime.fromisoformat(str(expiration_text)).date()
    except (TypeError, ValueError):
        expiration_date = timestamp.date()
    years = max((datetime.combine(expiration_date, settlement_time) - timestamp).total_seconds(), 1) / SECONDS_PER_YEAR
    d1 = implied["d1"]
    d2 = d1 - implied["total_vol"]
    return normal_pdf(d1) * d2 / (2 * years)


def charm_exposure(row, spot: float, timestamp: datetime, settlement_time: time) -> float | None:
    charm = charm_per_year(row, spot, timestamp, settlement_time)
    if charm is None:
        return None
    try:
        open_interest = float(row_value(row, "open_interest") or 0)
    except (TypeError, ValueError):
        return None
    return (charm / 365) * open_interest * 100


def delta_pressure(row) -> float | None:
    try:
        return float(row_value(row, "delta")) * float(row_value(row, "open_interest") or 0) * 100
    except (TypeError, ValueError):
        return None
