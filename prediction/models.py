"""JSON artifacts and inference for the existing price-only research models.

The coordinator owns instrument history, timestamps, session resets, training
cutoffs and warmup.  Here, a close sequence is one contiguous five-minute block.
No fitting occurs during inference, and artifacts never contain executable code.

Bands are nominal 80% Gaussian moment intervals, not empirically calibrated
coverage or probabilities of staying inside a path.  A center is the exponential
of the predicted log-price mean, not the arithmetic expected futures price.
"""

from __future__ import annotations

from datetime import date
import hashlib
import json
import math
from statistics import NormalDist

import numpy as np

from research.price_models.garch import GarchModel
from research.price_models.markov import MarkovModel
from .data import validate_instrument


_ROOT_KEYS = {
    "schema_version", "kind", "implementation_version", "instrument",
    "trained_through", "grid_minutes", "units", "nominal_coverage",
    "interval_method", "calibration_status", "garch", "markov",
    "diagnostics", "model_id",
}
_GARCH_KEYS = {"omega_scaled", "alpha", "beta", "initial_variance_bps2"}
_MARKOV_KEYS = {"intercept_bps", "ar1", "variances_bps2", "transition", "initial_probabilities"}
_RADIUS = NormalDist().inv_cdf(0.9)
# Loose corruption/unit guards, not training-quality or calibration thresholds.
_MAX_VARIANCE_BPS2 = 1e6
_MAX_CLOSE = 1e7


def _training_date(value):
    if not isinstance(value, str):
        raise ValueError("trained_through must be a YYYY-MM-DD date")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("trained_through must be a YYYY-MM-DD date") from exc
    if parsed.isoformat() != value:
        raise ValueError("trained_through must be a YYYY-MM-DD date")
    return value


def _json_tree(value, path="bundle", depth=0):
    """Reject non-JSON values and nonfinite numbers, including diagnostics."""
    if depth > 30:
        raise ValueError(f"Excessively nested JSON at {path}")
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError(f"Non-finite number at {path}")
        return
    if type(value) is list:
        for index, item in enumerate(value):
            _json_tree(item, f"{path}[{index}]", depth + 1)
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ValueError(f"JSON object keys must be strings at {path}")
            _json_tree(item, f"{path}.{key}", depth + 1)
        return
    raise ValueError(f"Non-JSON value at {path}")


def _keys(value, expected, name):
    if type(value) is not dict or set(value) != expected:
        raise ValueError(f"Unknown or missing {name} fields")


def _number(value, name, low=None, high=None):
    if type(value) not in (float, int):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a finite number") from exc
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(f"Suspicious or unsupported {name}")
    return number


def _probabilities(value, shape, name):
    result = np.asarray(value)
    if result.shape != shape or result.dtype.kind not in "fiu":
        raise ValueError(f"Invalid {name} shape or numeric type")
    result = result.astype(float)
    if not np.all(np.isfinite(result)) or np.any(result <= 0) or np.any(result >= 1):
        raise ValueError(f"{name} must contain probabilities strictly between zero and one")
    totals = result.sum(axis=-1)
    if not np.allclose(totals, 1.0, rtol=0, atol=1e-10):
        raise ValueError(f"{name} probabilities must sum to one")
    return result


def _model_id(bundle):
    # Diagnostics can contain platform-specific acceleration/convergence detail;
    # identity covers the complete inference specification and training cutoff.
    content = {key: value for key, value in bundle.items() if key not in ("model_id", "diagnostics")}
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return "fut-v1-" + hashlib.sha256(encoded).hexdigest()[:24]


def validate_bundle(bundle: dict, instrument: str | None = None) -> None:
    """Validate without mutation; raise ValueError on unsupported/corrupt data.

    ``instrument`` optionally enforces the exact futures contract expected by
    the coordinator.  The deterministic ID detects accidental alteration; it is
    not a cryptographic signature or a substitute for trusting the artifact file.
    """
    _keys(bundle, _ROOT_KEYS, "bundle")
    _json_tree(bundle)
    if type(bundle["schema_version"]) is not int or bundle["schema_version"] != 1:
        raise ValueError("Unknown bundle schema_version")
    if type(bundle["implementation_version"]) is not int or bundle["implementation_version"] != 1:
        raise ValueError("Unknown model implementation_version")
    if bundle["kind"] != "futures_price_models":
        raise ValueError("Unknown bundle kind")
    actual_instrument = validate_instrument(bundle["instrument"])
    if instrument is not None and actual_instrument != validate_instrument(instrument):
        raise ValueError("Bundle instrument does not match the requested contract")
    _training_date(bundle["trained_through"])
    if type(bundle["grid_minutes"]) is not int or bundle["grid_minutes"] != 5:
        raise ValueError("Only five-minute model artifacts are supported")
    if bundle["units"] != "log_price_basis_points":
        raise ValueError("Unknown model units")
    if _number(bundle["nominal_coverage"], "nominal_coverage") != 0.8:
        raise ValueError("Only nominal 80% intervals are supported")
    if bundle["interval_method"] != "gaussian_moment" or bundle["calibration_status"] != "uncalibrated":
        raise ValueError("Unsupported interval method or calibration claim")

    garch = bundle["garch"]
    _keys(garch, _GARCH_KEYS, "GARCH")
    _number(garch["omega_scaled"], "GARCH omega_scaled", 1e-6 * (1 - 1e-10), 10.0 * (1 + 1e-10))
    alpha = _number(garch["alpha"], "GARCH alpha", 0, 0.995 + 1e-12)
    beta = _number(garch["beta"], "GARCH beta", 0, 0.995 + 1e-12)
    if alpha + beta > 0.995 + 1e-12:
        raise ValueError("Suspicious GARCH persistence; alpha + beta exceeds the fitted bound")
    _number(garch["initial_variance_bps2"], "GARCH initial variance", 1e-8, _MAX_VARIANCE_BPS2)

    markov = bundle["markov"]
    _keys(markov, _MARKOV_KEYS, "Markov")
    _number(markov["intercept_bps"], "Markov intercept", -1000.0, 1000.0)
    _number(markov["ar1"], "Markov AR coefficient", -0.95, 0.95)
    variances = markov["variances_bps2"]
    if type(variances) is not list or len(variances) != 2:
        raise ValueError("Markov requires two ordered state variances")
    low, high = [_number(value, "Markov state variance", 1e-8, _MAX_VARIANCE_BPS2) for value in variances]
    if high <= low:
        raise ValueError("Markov high-state variance must exceed its low-state variance")
    _probabilities(markov["transition"], (2, 2), "Markov transition matrix")
    _probabilities(markov["initial_probabilities"], (2,), "Markov initial state")

    diagnostics = bundle["diagnostics"]
    _keys(diagnostics, {"garch", "markov"}, "diagnostics")
    for family in ("garch", "markov"):
        detail = diagnostics[family]
        if type(detail) is not dict or detail.get("converged") is not True:
            raise ValueError(f"{family} fit did not converge")
    if diagnostics["garch"].get("use_gex") is not False or diagnostics["markov"].get("use_gex") is not False:
        raise ValueError("Only price-only models are supported")
    if not isinstance(bundle["model_id"], str) or bundle["model_id"] != _model_id(bundle):
        raise ValueError("model_id does not match the model artifact")


def fit_bundle(instrument: str, blocks: list[dict], trained_through: str) -> dict:
    """Fit both existing price-only models and return a strict JSON artifact.

    The research HMM can return an unconverged best run.  Such a fit is rejected
    here; no substitute, retraining on current data, or inference-time fallback
    is performed.  The caller decides when another training attempt is allowed.
    """
    instrument = validate_instrument(instrument)
    trained_through = _training_date(trained_through)
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("blocks must be a nonempty list of training sequences")
    training = []
    for block in blocks:
        if not isinstance(block, dict) or "y" not in block:
            raise ValueError("Every training block must contain y")
        if "instrument" in block and block["instrument"] != instrument:
            raise ValueError("Training block belongs to a different instrument")
        try:
            y = np.asarray(block["y"], dtype=float)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("Training log prices must be finite numeric values") from exc
        if y.ndim != 1 or len(y) < 2 or not np.all(np.isfinite(y)):
            raise ValueError("Every training block needs at least two finite log prices")
        if np.any(y < 0) or np.any(y > math.log(_MAX_CLOSE) * 10000):
            raise ValueError("Suspicious training log-price units")
        training.append({"y": y.copy()})

    garch = GarchModel(False).fit(training)
    markov = MarkovModel(False).fit(training)
    if garch.diagnostics.get("converged") is not True or markov.diagnostics.get("converged") is not True:
        raise RuntimeError("GARCH and Markov must both converge before a bundle is published")
    omega, alpha, beta, theta = garch._parameters
    if theta != 0:
        raise RuntimeError("Unexpected GEX coefficient in a price-only GARCH fit")
    bundle = {
        "schema_version": 1,
        "kind": "futures_price_models",
        "implementation_version": 1,
        "instrument": instrument,
        "trained_through": trained_through,
        "grid_minutes": 5,
        "units": "log_price_basis_points",
        "nominal_coverage": 0.8,
        "interval_method": "gaussian_moment",
        "calibration_status": "uncalibrated",
        "garch": {
            "omega_scaled": float(omega), "alpha": float(alpha), "beta": float(beta),
            "initial_variance_bps2": float(garch._scale_variance),
        },
        "markov": {
            "intercept_bps": float(markov.beta[0]), "ar1": float(markov.beta[1]),
            "variances_bps2": markov.variances.tolist(),
            "transition": markov.transition.tolist(),
            "initial_probabilities": markov.initial.tolist(),
        },
        "diagnostics": {"garch": garch.diagnostics, "markov": markov.diagnostics},
    }
    bundle["model_id"] = _model_id(bundle)
    validate_bundle(bundle, instrument)
    # Detach mutable model diagnostics as well as guaranteeing a JSON roundtrip.
    return json.loads(json.dumps(bundle, allow_nan=False))


def _restore_models(bundle):
    validate_bundle(bundle)
    g = bundle["garch"]
    garch = GarchModel(False)
    garch._parameters = (float(g["omega_scaled"]), float(g["alpha"]), float(g["beta"]), 0.0)
    garch._scale_variance = float(g["initial_variance_bps2"])
    garch.diagnostics = bundle["diagnostics"]["garch"]
    m = bundle["markov"]
    markov = MarkovModel(False)
    markov.beta = np.array([m["intercept_bps"], m["ar1"]], dtype=float)
    markov.variances = np.array(m["variances_bps2"], dtype=float)
    markov.transition = np.array(m["transition"], dtype=float)
    markov.initial = np.array(m["initial_probabilities"], dtype=float)
    markov.diagnostics = bundle["diagnostics"]["markov"]
    markov._fitted = True
    return garch, markov


def forecast_bundle(bundle: dict, closes: list[float]) -> dict:
    """Forecast from the final close using only this contiguous block's prefix.

    All returned band levels are absolute futures prices, without tick rounding.
    Service-level warmup and chronology checks are intentionally not duplicated.
    """
    garch, markov = _restore_models(bundle)
    try:
        if any(isinstance(value, (bool, np.bool_)) for value in closes):
            raise ValueError("Boolean values are not valid closes")
        prices = np.asarray(closes, dtype=float)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("closes must contain numeric prices") from exc
    if prices.ndim != 1 or len(prices) < 2 or not np.all(np.isfinite(prices)):
        raise ValueError("At least two finite closes from one contiguous block are required")
    if np.any(prices < 1.0) or np.any(prices > _MAX_CLOSE):
        raise ValueError("Closes must be positive plausible futures prices")
    y = np.log(prices) * 10000
    block = {"y": y}
    output = {"model_id": bundle["model_id"], "nominal_coverage": 0.8}
    for name, model in (("garch", garch), ("markov", markov)):
        prediction = model.predict(block, max_steps=6)
        for horizon, column in ((15, 2), (30, 5)):
            mean = float(prediction["mean"][-1, column])
            variance = float(prediction["variance"][-1, column])
            if not math.isfinite(mean) or not math.isfinite(variance) or variance <= 0:
                raise ValueError("Model produced invalid forecast moments")
            radius = _RADIUS * math.sqrt(variance)
            for label, offset in (("lower", mean - radius), ("center", mean), ("upper", mean + radius)):
                try:
                    level = float(prices[-1]) * math.exp(offset / 10000)
                except OverflowError as exc:
                    raise ValueError("Model produced an implausible forecast level") from exc
                if not math.isfinite(level) or level <= 0 or level > _MAX_CLOSE:
                    raise ValueError("Model produced an implausible forecast level")
                output[f"{name}_{horizon}_{label}"] = level
    returns = np.r_[0.0, np.diff(y)]
    probability = float(markov._next_state_probabilities(returns, np.zeros(len(y)))[-1, 1])
    if not math.isfinite(probability) or not 0 <= probability <= 1:
        raise ValueError("Model produced an invalid next high-volatility probability")
    output["high_vol_probability"] = probability
    return output
