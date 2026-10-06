"""Provider contract labels remain exact throughout validation and modeling."""
import copy
import json

import numpy as np
import pytest

from prediction.data import validate_instrument
from prediction.models import fit_bundle, forecast_bundle, validate_bundle


@pytest.mark.parametrize("symbol", ["ES", "MES", "NQ", "MNQ"])
@pytest.mark.parametrize("expiry", ["03-26", "06-26", "09-26", "12-26", "MAR26", "JUN26", "SEP26", "DEC26"])
def test_both_quarterly_label_forms_are_preserved_exactly(symbol, expiry):
    instrument = f"{symbol} {expiry}"
    assert validate_instrument(instrument) == instrument
    assert validate_instrument(instrument, symbol) == instrument


@pytest.mark.parametrize("instrument", [
    None, 123, True, "", "ES", "NQ", "SPX", "NDX", "NDX DEC26", "YM DEC26",
    "ES ##-##", "ES 1!", "@ES", "ES CONT", "ES DEC", "ES DEC2026", "ES DEC-26",
    "ES DE26", "ES Z26", "ES 01-26", "ES 02-26", "ES 04-26", "ES 05-26",
    "ES 07-26", "ES 08-26", "ES 10-26", "ES 11-26", "ES JAN26", "ES MAY26",
    "ES dec26", "es DEC26", "ES  DEC26", " ES DEC26", "ES DEC26 ", "ES DEC26\n",
    "ES\tDEC26", "ES\nDEC26", "ES\u00a0DEC26", "ES DEC26 CME", "ES DEC２６",
])
def test_unsupported_or_ambiguous_labels_are_rejected(instrument):
    with pytest.raises(ValueError, match="quarterly contract"):
        validate_instrument(instrument)
    with pytest.raises(ValueError, match="quarterly contract"):
        fit_bundle(instrument, [], "2026-09-29")


@pytest.mark.parametrize("instrument,symbol", [
    ("NQ DEC26", "MNQ"), ("MNQ DEC26", "NQ"), ("ES DEC26", "MES"),
    ("MES 12-26", "ES"), ("ES 12-26", "SPX"), ("NQ DEC26", "nq"),
])
def test_chart_root_must_match_exact_contract_root(instrument, symbol):
    with pytest.raises(ValueError, match="quarterly contract"):
        validate_instrument(instrument, symbol)


@pytest.fixture(scope="module")
def contract_models():
    rng = np.random.default_rng(92)
    blocks = []
    for session in range(18):
        y = np.empty(78)
        y[0] = np.log(30000.0 + session * 2) * 10000
        state, previous = int(rng.integers(2)), 0.0
        for i in range(1, len(y)):
            if rng.random() > 0.93:
                state = 1 - state
            previous = 0.12 * previous + rng.normal(0, (1.0, 5.0)[state])
            y[i] = y[i - 1] + previous
        blocks.append({"y": y})
    labels = ("NQ DEC26", "NQ 12-26")
    bundles = {label: fit_bundle(label, [dict(block, instrument=label) for block in blocks], "2026-09-29")
               for label in labels}
    return blocks, bundles


def test_real_fit_roundtrip_and_forecasts_preserve_provider_label(contract_models):
    blocks, bundles = contract_models
    for label, original in bundles.items():
        restored = json.loads(json.dumps(original, allow_nan=False))
        validate_bundle(restored, label)
        assert restored["instrument"] == label
        forecast = forecast_bundle(restored, np.exp(blocks[-1]["y"] / 10000).tolist())
        assert forecast["model_id"] == original["model_id"]
        assert forecast["garch_15_lower"] < forecast["garch_15_upper"]
        assert forecast["markov_30_lower"] < forecast["markov_30_upper"]


def test_alias_forms_are_distinct_model_identities(contract_models):
    _, bundles = contract_models
    assert bundles["NQ DEC26"]["model_id"] != bundles["NQ 12-26"]["model_id"]
    for actual, expected in (("NQ DEC26", "NQ 12-26"), ("NQ 12-26", "NQ DEC26")):
        with pytest.raises(ValueError, match="requested contract"):
            validate_bundle(bundles[actual], expected)
    changed = copy.deepcopy(bundles["NQ DEC26"])
    changed["instrument"] = "NQ 12-26"
    with pytest.raises(ValueError, match="model_id"):
        validate_bundle(changed)


def test_training_cannot_mix_the_two_label_forms():
    block = {"instrument": "NQ 12-26", "y": np.log([30000.0, 30001.0]) * 10000}
    with pytest.raises(ValueError, match="different instrument"):
        fit_bundle("NQ DEC26", [block], "2026-09-29")
