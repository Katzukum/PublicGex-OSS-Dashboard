import json

import numpy as np
import pytest

pytest.importorskip('scipy', reason='Install requirements-research.txt for offline model tests')

from research.price_models.garch import GarchModel


def _synthetic_blocks(seed=73, count=22, length=180, theta=1.1):
    rng = np.random.default_rng(seed)
    blocks = []
    omega, alpha, beta = 0.16, 0.12, 0.68
    for index in range(count):
        gamma = rng.uniform(-1.0, 1.0, length)
        levels = np.empty(length)
        levels[0] = 46051.7
        previous_variance = 1.0
        previous_squared_return = 1.0
        for tick in range(1, length):
            variance = omega * np.exp(theta * gamma[tick - 1]) + alpha * previous_squared_return + beta * previous_variance
            value = np.sqrt(variance) * rng.normal()
            levels[tick] = levels[tick - 1] + 4.0 * value
            previous_variance = variance
            previous_squared_return = value ** 2
        blocks.append({"symbol": "SYNTH", "session": str(index), "block": index, "y": levels, "g": gamma})
    return blocks


@pytest.fixture(scope="module")
def fitted_models():
    blocks = _synthetic_blocks()
    return blocks, GarchModel(True).fit(blocks), GarchModel(False).fit(blocks)


def test_recovers_gamma_variance_effect_and_improves_likelihood(fitted_models):
    _, gamma_model, baseline = fitted_models
    assert gamma_model.diagnostics["theta"] == pytest.approx(1.1, abs=0.4)
    assert gamma_model.diagnostics["objective"] < baseline.diagnostics["objective"] - 0.003
    assert gamma_model.diagnostics["converged"]
    assert 0 <= gamma_model.diagnostics["persistence"] <= 0.995
    assert len(gamma_model.diagnostics["optimizer_attempts"]) == 3
    json.dumps(gamma_model.diagnostics, allow_nan=False)
    steady = {"y": np.full(50, 46051.7), "g": np.ones(50)}
    inverse = {"y": steady["y"], "g": -steady["g"]}
    high = gamma_model.predict(steady)["variance"][-1, 0]
    low = gamma_model.predict(inverse)["variance"][-1, 0]
    assert high > 3.0 * low


def test_gamma_effect_is_estimated_without_prescribing_its_sign():
    model = GarchModel(True).fit(_synthetic_blocks(seed=81, theta=-1.1))
    assert model.diagnostics["theta"] == pytest.approx(-1.1, abs=0.4)


def test_future_perturbation_cannot_change_earlier_forecasts(fitted_models):
    blocks, gamma_model, _ = fitted_models
    original = blocks[0]
    changed = dict(original)
    changed["y"] = original["y"].copy()
    changed["g"] = original["g"].copy()
    changed["y"][73:] += np.arange(len(changed["y"]) - 73) * 150.0 + 200.0
    changed["g"][73:] *= -1
    expected = gamma_model.predict(original)
    actual = gamma_model.predict(changed)
    np.testing.assert_array_equal(actual["mean"][:73], expected["mean"][:73])
    np.testing.assert_array_equal(actual["variance"][:73], expected["variance"][:73])
    assert not np.allclose(actual["variance"][73:], expected["variance"][73:])


def test_each_predict_call_resets_at_block_boundary(fitted_models):
    blocks, gamma_model, _ = fitted_models
    tail = {"y": blocks[1]["y"][100:].copy(), "g": blocks[1]["g"][100:].copy()}
    expected = gamma_model.predict(tail)
    gamma_model.predict(blocks[0])
    actual = gamma_model.predict(tail)
    np.testing.assert_array_equal(actual["variance"], expected["variance"])
    one_origin = {"y": tail["y"][:1], "g": tail["g"][:1]}
    np.testing.assert_array_equal(actual["variance"][:1], gamma_model.predict(one_origin)["variance"])
    shifted = {"y": tail["y"] + 1e4, "g": tail["g"]}
    np.testing.assert_allclose(gamma_model.predict(shifted)["variance"], actual["variance"], rtol=1e-10)


def test_cumulative_variance_is_positive_and_baseline_ignores_gamma(fitted_models):
    blocks, gamma_model, baseline = fitted_models
    for model in (gamma_model, baseline):
        prediction = model.predict(blocks[0], max_steps=12)
        assert prediction["mean"].shape == (len(blocks[0]["y"]), 12)
        assert np.all(prediction["mean"] == 0)
        assert np.all(np.isfinite(prediction["variance"]))
        assert np.all(prediction["variance"] > 0)
        assert np.all(np.diff(prediction["variance"], axis=1) > 0)
    no_gamma = {"y": blocks[0]["y"]}
    np.testing.assert_array_equal(baseline.predict(no_gamma)["variance"], baseline.predict(blocks[0])["variance"])


def test_multistep_variance_matches_closed_form_for_frozen_gamma(fitted_models):
    blocks, gamma_model, _ = fitted_models
    prediction = gamma_model.predict(blocks[0], max_steps=9)
    persistence = gamma_model.diagnostics["persistence"]
    intercept = gamma_model.diagnostics["omega_bp2"] * np.exp(gamma_model.diagnostics["theta"] * blocks[0]["g"])
    long_run_variance = intercept / (1.0 - persistence)
    first = prediction["variance"][:, 0]
    for step in range(1, 10):
        expected = step * long_run_variance + (first - long_run_variance) * (1.0 - persistence ** step) / (1.0 - persistence)
        np.testing.assert_allclose(prediction["variance"][:, step - 1], expected, rtol=1e-12)


def test_invalid_training_or_prediction_fails_explicitly():
    with pytest.raises(ValueError, match="20 observed"):
        GarchModel(False).fit([{"y": np.arange(10)}])
    with pytest.raises(ValueError, match="nonzero finite"):
        GarchModel(False).fit([{"y": np.ones(30)}])
    with pytest.raises(ValueError, match="Normalized gamma"):
        GarchModel(True).fit([{"y": np.arange(30), "g": np.full(30, 2.0)}])
    with pytest.raises(RuntimeError, match="Fit GarchModel"):
        GarchModel(False).predict({"y": np.ones(30)})


def test_failed_optimization_is_reported_without_fallback(monkeypatch):
    from types import SimpleNamespace
    from research.price_models import garch

    def failed(_objective, start, **_kwargs):
        return SimpleNamespace(success=False, fun=1.0, x=start, status=2, message="synthetic failure", nit=0, nfev=1)

    monkeypatch.setattr(garch, "minimize", failed)
    model = GarchModel(False)
    with pytest.raises(RuntimeError, match="Every GARCH optimization"):
        model.fit(_synthetic_blocks(count=1, length=40))
    assert not model.diagnostics["fitted"]
    assert len(model.diagnostics["optimizer_attempts"]) == 3
    assert all(not attempt["success"] for attempt in model.diagnostics["optimizer_attempts"])
