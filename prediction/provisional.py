"""A short-history, zero-drift EWMA endpoint forecast in observed price units.

The caller supplies one contiguous five-minute close sequence ending at the
forecast origin; it owns timestamps, session boundaries, gaps and provenance.
With r[j] = 10000 * (log(P[j]) - log(P[j-1])), seed v with the mean of the
first five squared returns. For every subsequent observed return, update
v = 0.94*v + 0.06*r[j]**2. No fitted parameters or stored filter state are used.

The 15/30-minute log-return variances are 3*v and 6*v, respectively. Endpoints
are P[-1] * exp(+-Phi^-1(0.9)*sqrt(h*v)/10000), with center P[-1]. These are
nominal 80% Gaussian moment intervals, not calibrated coverage or probabilities
of staying inside a path. The center represents the mean of log price, not the
arithmetic expected price. Flat data produces no invented variance floor.
"""

from __future__ import annotations

import math
from numbers import Real
from statistics import NormalDist


EWMA_DECAY = 0.94
SEED_RETURNS = 5
MIN_CLOSES = SEED_RETURNS + 1
MODEL_ID = "ewma-provisional-v1-decay094-seed5"
_RADIUS = NormalDist().inv_cdf(0.9)


def provisional_forecast(closes: list[float]) -> dict:
    """Return EWMA-only absolute price bands from at least six observed closes.

    A fixed algorithm ID identifies the decay, seeding and forecast method;
    the coordinator identifies individual forecasts by their origin and series.
    This function never emits fitted-model levels or a regime probability.
    """
    if isinstance(closes, (str, bytes)):
        raise ValueError("closes must contain positive finite numeric prices")
    try:
        supplied = list(closes)
    except TypeError as exc:
        raise ValueError("closes must contain positive finite numeric prices") from exc
    if len(supplied) < MIN_CLOSES:
        raise ValueError("Provisional EWMA needs at least six contiguous five-minute closes")
    prices = []
    for value in supplied:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("closes must contain positive finite numeric prices")
        try:
            price = float(value)
        except (ValueError, OverflowError) as exc:
            raise ValueError("closes must contain positive finite numeric prices") from exc
        if not math.isfinite(price) or price <= 0:
            raise ValueError("closes must contain positive finite numeric prices")
        prices.append(price)

    log_prices = [math.log(price) for price in prices]
    returns = [10000.0 * (current - previous) for previous, current in zip(log_prices, log_prices[1:])]
    variance = math.fsum(value * value for value in returns[:SEED_RETURNS]) / SEED_RETURNS
    for value in returns[SEED_RETURNS:]:
        variance = EWMA_DECAY * variance + (1.0 - EWMA_DECAY) * value * value
    if variance <= 0:
        raise ValueError("No measurable price variation for a provisional EWMA forecast")

    result = {
        "model_id": MODEL_ID,
        "nominal_coverage": 0.8,
        "high_vol_probability": None,
        "forecast_mode": "PROVISIONAL",
        "model_family": "EWMA",
        "available_models": ["ewma"],
    }
    center = prices[-1]
    for horizon, steps in ((15, 3), (30, 6)):
        radius = _RADIUS * math.sqrt(steps * variance) / 10000.0
        try:
            lower, upper = center * math.exp(-radius), center * math.exp(radius)
        except OverflowError as exc:
            raise ValueError("Observed price variation produces non-finite provisional bands") from exc
        if not math.isfinite(lower) or not math.isfinite(upper) or lower <= 0:
            raise ValueError("Observed price variation produces non-finite provisional bands")
        if not lower < center < upper:
            raise ValueError("No measurable price variation for a provisional EWMA forecast")
        result.update({f"ewma_{horizon}_lower": lower, f"ewma_{horizon}_center": center,
                       f"ewma_{horizon}_upper": upper})
    return result
