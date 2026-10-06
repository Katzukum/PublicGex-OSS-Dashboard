import copy
import json
from statistics import NormalDist

import numpy as np
import pytest

pytest.importorskip("scipy")

from prediction.models import fit_bundle, forecast_bundle, validate_bundle
from research.price_models.garch import GarchModel
from research.price_models.markov import MarkovModel


def _training_blocks(seed=92):
    rng = np.random.default_rng(seed)
    blocks = []
    for session in range(18):
        y = np.empty(78)
        y[0] = np.log(6000.0 + session * 2) * 10000
        state, previous = int(rng.integers(2)), 0.0
        for i in range(1, len(y)):
            if rng.random() > 0.93:
                state = 1 - state
            previous = 0.12 * previous + rng.normal(0, (1.0, 5.0)[state])
            y[i] = y[i - 1] + previous
        blocks.append({"y": y, "instrument": "ES 12-26"})
    return blocks


@pytest.fixture(scope="module")
def fitted():
    blocks = _training_blocks()
    bundle = fit_bundle("ES 12-26", blocks, "2026-09-29")
    return blocks, bundle, GarchModel(False).fit(blocks), MarkovModel(False).fit(blocks)


def test_actual_fit_json_roundtrip_and_exact_research_forecast_parity(fitted):
    blocks, original, garch, markov = fitted
    bundle = json.loads(json.dumps(original, allow_nan=False))
    validate_bundle(bundle, "ES 12-26")
    assert bundle["diagnostics"]["garch"]["converged"]
    assert bundle["diagnostics"]["markov"]["converged"]
    closes = np.exp(blocks[-1]["y"] / 10000).tolist()
    result = forecast_bundle(bundle, closes)
    block = {"y": np.log(closes) * 10000}
    radius = NormalDist().inv_cdf(0.9)
    for family, model in (("garch", garch), ("markov", markov)):
        expected = model.predict(block)
        for horizon, index in ((15, 2), (30, 5)):
            mean, sd = expected["mean"][-1, index], np.sqrt(expected["variance"][-1, index])
            for label, offset in (("lower", mean - radius * sd), ("center", mean), ("upper", mean + radius * sd)):
                assert result[f"{family}_{horizon}_{label}"] == pytest.approx(closes[-1] * np.exp(offset / 10000), rel=1e-12)
    returns = np.r_[0.0, np.diff(block["y"])]
    expected_probability = markov._next_state_probabilities(returns, np.zeros(len(returns)))[-1, 1]
    assert result["high_vol_probability"] == pytest.approx(expected_probability, abs=1e-12)
    assert result["model_id"] == original["model_id"]
    assert result["nominal_coverage"] == 0.8
    assert result["garch_15_center"] == closes[-1]
    assert len(result) == 15


def test_identity_is_deterministic_and_predictions_do_not_mutate_artifact(fitted):
    blocks, bundle, _, _ = fitted
    repeated = fit_bundle("ES 12-26", blocks, "2026-09-29")
    assert repeated["model_id"] == bundle["model_id"]
    snapshot = copy.deepcopy(bundle)
    forecast_bundle(bundle, np.exp(blocks[0]["y"] / 10000).tolist())
    assert bundle == snapshot
    modified_metadata = copy.deepcopy(bundle)
    modified_metadata["trained_through"] = "2026-09-28"
    with pytest.raises(ValueError, match="model_id"):
        validate_bundle(modified_metadata)


def test_prediction_uses_only_available_prefix_and_resets_between_calls(fitted):
    blocks, bundle, _, markov = fitted
    closes = np.exp(blocks[1]["y"] / 10000)
    prefix = closes[:30].copy()
    initial = forecast_bundle(bundle, prefix.tolist())
    changed = closes.copy()
    changed[30:] *= np.exp(np.arange(len(changed) - 30) * 0.0007)
    forecast_bundle(bundle, changed.tolist())
    repeated = forecast_bundle(bundle, prefix.tolist())
    assert repeated == initial
    full_y = np.log(changed) * 10000
    next_probabilities = markov._next_state_probabilities(np.r_[0.0, np.diff(full_y)], np.zeros(len(full_y)))
    assert initial["high_vol_probability"] == pytest.approx(next_probabilities[29, 1], abs=1e-12)
    expected = markov.predict({"y": full_y})
    assert initial["markov_30_center"] == pytest.approx(prefix[-1] * np.exp(expected["mean"][29, 5] / 10000), rel=1e-12)


@pytest.mark.parametrize("field,value", [
    ("schema_version", 2), ("schema_version", True), ("implementation_version", 99),
    ("instrument", "SPX"), ("instrument", "ES"), ("instrument", "ES 11-26"),
    ("instrument", "ES ##-##"), ("trained_through", "20260929"),
    ("grid_minutes", 1), ("units", "price_points"), ("nominal_coverage", 0.95),
    ("calibration_status", "calibrated"),
])
def test_unknown_metadata_is_rejected(fitted, field, value):
    bundle = copy.deepcopy(fitted[1])
    bundle[field] = value
    with pytest.raises(ValueError):
        validate_bundle(bundle)


@pytest.mark.parametrize("family,field,value", [
    ("garch", "alpha", 0.999), ("garch", "omega_scaled", -1.0),
    ("garch", "initial_variance_bps2", float("nan")),
    ("garch", "initial_variance_bps2", 1e20),
    ("garch", "initial_variance_bps2", 10**400),
    ("markov", "ar1", 1.1), ("markov", "intercept_bps", 1e9),
    ("markov", "variances_bps2", [4.0, 1.0]),
    ("markov", "variances_bps2", [1.0, float("inf")]),
    ("markov", "transition", [[0.9, 0.2], [0.1, 0.9]]),
    ("markov", "transition", [[1.1, -0.1], [0.1, 0.9]]),
    ("markov", "initial_probabilities", [0.5, 0.6]),
])
def test_invalid_parameters_are_rejected(fitted, family, field, value):
    bundle = copy.deepcopy(fitted[1])
    bundle[family][field] = value
    with pytest.raises(ValueError):
        validate_bundle(bundle)


def test_wrong_contract_nonconvergence_and_changed_parameters_are_rejected(fitted):
    with pytest.raises(ValueError, match="requested contract"):
        validate_bundle(fitted[1], "NQ 12-26")
    unconverged = copy.deepcopy(fitted[1])
    unconverged["diagnostics"]["markov"]["converged"] = False
    with pytest.raises(ValueError, match="did not converge"):
        forecast_bundle(unconverged, [6000.0, 6001.0])
    changed = copy.deepcopy(fitted[1])
    changed["garch"]["alpha"] *= 0.99
    with pytest.raises(ValueError, match="model_id"):
        validate_bundle(changed)
    extra = copy.deepcopy(fitted[1])
    extra["garch"]["theta"] = 0.0
    with pytest.raises(ValueError, match="GARCH"):
        validate_bundle(extra)


def test_unconverged_research_fit_never_produces_artifact(monkeypatch):
    original_fit = MarkovModel.fit

    def fail_convergence(self, blocks):
        original_fit(self, blocks)
        self.diagnostics["converged"] = False
        return self

    monkeypatch.setattr(MarkovModel, "fit", fail_convergence)
    with pytest.raises(RuntimeError, match="both converge"):
        fit_bundle("ES 12-26", _training_blocks(), "2026-09-29")


@pytest.mark.parametrize("closes", [[6000.0], [6000.0, float("nan")], [6000.0, 0.0], [6000.0, -1.0], [True, 6000.0], [[6000.0, 6001.0]], [6000.0, 10**400]])
def test_invalid_price_inputs_are_rejected(fitted, closes):
    with pytest.raises(ValueError):
        forecast_bundle(fitted[1], closes)
