"""Verify the short-history fallback independently of fitted model libraries."""

import json
import math
from statistics import NormalDist

import pytest

from prediction.provisional import MODEL_ID, provisional_forecast


def _prices(returns, initial=6000.0):
    log_price = math.log(initial)
    result = [initial]
    for change in returns:
        log_price += change / 10000.0
        result.append(math.exp(log_price))
    return result


def _implied_step_variance(forecast, horizon=15):
    radius = math.log(forecast[f"ewma_{horizon}_upper"] / forecast[f"ewma_{horizon}_center"]) * 10000
    return (radius / NormalDist().inv_cdf(0.9)) ** 2 / (horizon / 5)


def test_exact_mean_square_seed_ewma_update_and_endpoint_formula():
    prices = _prices([1.0, -2.0, 3.0, -4.0, 5.0, 20.0])
    forecast = provisional_forecast(prices)
    expected_variance = 0.94 * 11.0 + 0.06 * 400.0
    assert _implied_step_variance(forecast) == pytest.approx(expected_variance, rel=1e-8)
    for horizon, steps in ((15, 3), (30, 6)):
        radius = NormalDist().inv_cdf(0.9) * math.sqrt(steps * expected_variance) / 10000
        assert forecast[f"ewma_{horizon}_lower"] == pytest.approx(prices[-1] * math.exp(-radius), rel=1e-12)
        assert forecast[f"ewma_{horizon}_center"] == prices[-1]
        assert forecast[f"ewma_{horizon}_upper"] == pytest.approx(prices[-1] * math.exp(radius), rel=1e-12)
        assert 0 < forecast[f"ewma_{horizon}_lower"] < prices[-1] < forecast[f"ewma_{horizon}_upper"]
    assert forecast["model_id"] == MODEL_ID
    assert forecast["nominal_coverage"] == 0.8
    assert forecast["forecast_mode"] == "PROVISIONAL"
    assert forecast["model_family"] == "EWMA"
    assert forecast["available_models"] == ["ewma"]
    assert forecast["high_vol_probability"] is None
    assert not any(key.startswith(("garch_", "markov_")) for key in forecast)
    assert json.loads(json.dumps(forecast, allow_nan=False)) == forecast


def test_shock_widens_variance_then_no_return_decays_it():
    seed = [1.0, -1.0, 1.0, -1.0, 1.0]
    baseline = provisional_forecast(_prices(seed))
    shocked = provisional_forecast(_prices(seed + [20.0]))
    quiet = provisional_forecast(_prices(seed + [20.0, 0.0]))
    assert _implied_step_variance(shocked) > _implied_step_variance(baseline)
    assert _implied_step_variance(quiet) == pytest.approx(0.94 * _implied_step_variance(shocked), rel=1e-8)
    # Both horizons use the same next-step variance, with cumulative variance
    # proportional to elapsed steps rather than a fabricated directional drift.
    assert _implied_step_variance(shocked, 15) == pytest.approx(_implied_step_variance(shocked, 30), rel=1e-10)


def test_prefix_is_stateless_and_future_calls_do_not_change_issued_forecast():
    prefix = _prices([1.0, -2.0, 3.0, -4.0, 5.0, 6.0])
    snapshot = prefix.copy()
    issued = provisional_forecast(prefix)
    future = _prices([1.0, -2.0, 3.0, -4.0, 5.0, 6.0, 25.0, -30.0])
    other = provisional_forecast(future)
    assert other["model_id"] == issued["model_id"]  # Algorithm identity, not input identity.
    assert provisional_forecast(future[:len(prefix)]) == issued
    assert provisional_forecast(prefix) == issued
    assert prefix == snapshot


@pytest.mark.parametrize("closes", [
    [], [6000.0] * 5, None, "6000,6001", [6000.0] * 5 + [True],
    [6000.0] * 5 + [float("nan")], [6000.0] * 5 + [float("inf")],
    [6000.0] * 5 + [0.0], [6000.0] * 5 + [-1.0],
    [6000.0] * 5 + ["6001"], [6000.0] * 5 + [10**400],
    [[6000.0, 6001.0]] * 6,
])
def test_invalid_or_short_sequences_are_rejected(closes):
    with pytest.raises(ValueError):
        provisional_forecast(closes)


def test_flat_sequence_has_no_invented_variance_floor():
    with pytest.raises(ValueError, match="[Nn]o measurable price variation"):
        provisional_forecast([6000.0] * 20)
