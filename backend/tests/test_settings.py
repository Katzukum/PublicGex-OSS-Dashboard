import copy
import json

import pytest

import app_settings
from app_settings import (DEFAULT_SETTINGS, DEFAULT_SYMBOLS, LEGACY_STARTER_SETTINGS,
                          PREFERENCES_NAME, initialize_settings, read_settings,
                          save_settings_patch, settings_upgrade, validate_settings)


@pytest.fixture
def settings_directory(tmp_path, monkeypatch):
    import runtime_paths
    monkeypatch.setattr(runtime_paths, "DATA_DIR", tmp_path)
    return tmp_path


def test_shared_live_defaults_match_original_configured_universe(settings_directory):
    assert DEFAULT_SYMBOLS == ["SPY", "QQQ", "IWM", "SPX", "NDX"]
    assert DEFAULT_SETTINGS["weights"] == {"SPY": .5, "QQQ": .3, "IWM": .2}
    assert DEFAULT_SETTINGS["weights_index_basket"] == {"SPX": .45, "NDX": .35, "IWM": .2}
    assert DEFAULT_SETTINGS["weights_whale"] == DEFAULT_SETTINGS["weights_index_basket"]
    assert DEFAULT_SETTINGS["ninjatrader_port"] == 5010
    assert initialize_settings()["status"] == "initialized"
    assert json.loads((settings_directory / "settings.json").read_text()) == DEFAULT_SETTINGS
    # The collector and dashboard import the identical shared preset.
    import publicData
    assert publicData.load_settings()["symbols"] == DEFAULT_SYMBOLS
    assert publicData.DEFAULT_SETTINGS is app_settings.DEFAULT_SETTINGS
    assert publicData.load_settings()["refresh_interval"] == 10


def test_only_exact_untouched_starter_gets_backed_up_and_upgraded(settings_directory):
    path = settings_directory / "settings.json"
    original = json.dumps(LEGACY_STARTER_SETTINGS, indent=4)
    path.write_text(original)
    result = initialize_settings()
    assert result["status"] == "upgraded_starter"
    assert (settings_directory / "settings.before-defaults-v2.json").read_text() == original
    assert read_settings()["symbols"] == DEFAULT_SYMBOLS
    assert read_settings()["weights"] == DEFAULT_SETTINGS["weights"]
    assert initialize_settings()["status"] == "preserved"


@pytest.mark.parametrize("updates", [
    {"theme": "light"}, {"refresh_interval": 15}, {"symbols": ["SPY", "DIA"]},
    {"weights": {"SPY": .7, "DIA": .3}}, {"maximum_risk_dollars": 250},
    {"calendar_sources": ["custom-local-calendar"]},
])
def test_ambiguous_or_customized_legacy_settings_are_never_overwritten(settings_directory, updates):
    raw = copy.deepcopy(LEGACY_STARTER_SETTINGS) | updates
    path = settings_directory / "settings.json"
    original = json.dumps(raw)
    path.write_text(original)
    assert settings_upgrade(raw) is None
    assert initialize_settings()["status"] == "preserved"
    assert path.read_text() == original
    assert not (settings_directory / "settings.before-defaults-v2.json").exists()


def test_explicit_spy_only_choice_survives_restarts(settings_directory):
    initialize_settings()
    saved = save_settings_patch({"symbols": ["SPY"], "weights": {"SPY": 1}})
    assert saved["symbols"] == ["SPY"]
    assert json.loads((settings_directory / PREFERENCES_NAME).read_text())["explicit_symbols"] is True
    assert initialize_settings()["status"] == "preserved"
    assert read_settings()["symbols"] == ["SPY"]
    assert settings_upgrade(LEGACY_STARTER_SETTINGS, {"explicit_symbols": True}) is None


def test_partial_settings_preserve_context_and_feature_flags(settings_directory):
    initialize_settings()
    first = save_settings_patch({"calendar_sources": ["saved-calendar.ics"], "market_context": {"custom": True}, "feature_flags": {"trace_replay": False}})
    assert first["feature_flags"]["trace_replay"] is False
    second = save_settings_patch({"fees_per_contract": 2.5, "maximum_risk_dollars": 750, "feature_flags": {"edge_lab": False}})
    assert second["calendar_sources"] == ["saved-calendar.ics"]
    assert second["market_context"] == {"custom": True}
    assert second["feature_flags"]["trace_replay"] is False
    assert second["feature_flags"]["edge_lab"] is False
    assert second["feature_flags"]["decision_workspace"] is True
    assert second["symbols"] == DEFAULT_SYMBOLS


def test_either_basket_alias_updates_both_without_masking_custom_weights(settings_directory):
    initialize_settings()
    expected = {"SPX": .8, "NDX": .2}
    result = save_settings_patch({"weights_whale": expected})
    assert result["weights_index_basket"] == result["weights_whale"] == expected
    result = save_settings_patch({"weights_index_basket": {"NDX": 1}})
    assert result["weights_index_basket"] == result["weights_whale"] == {"NDX": 1}
    before = (settings_directory / "settings.json").read_bytes()
    with pytest.raises(ValueError, match="aliases"):
        save_settings_patch({"weights_index_basket": {"NDX": 1}, "weights_whale": {"SPX": 1}})
    assert (settings_directory / "settings.json").read_bytes() == before


@pytest.mark.parametrize("patch", [
    {"maximum_risk_dollars": -1}, {"maximum_risk_dollars": 0}, {"maximum_risk_dollars": float("inf")},
    {"fees_per_contract": -1}, {"fees_per_contract": float("nan")},
    {"weights": {}}, {"weights": {"SPY": -1}}, {"weights": {"SPY": 0}},
    {"weights_index_basket": {"SPX": float("inf")}}, {"weights_whale": {"SPX": "bad"}},
    {"symbols": []}, {"symbols": ["../outside"]},
    {"feature_flags": {"trace_replay": "false"}}, {"max_poll_interval_seconds": 1},
])
def test_invalid_financial_and_instrument_settings_leave_disk_unchanged(settings_directory, patch):
    initialize_settings()
    path = settings_directory / "settings.json"
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_settings_patch(patch)
    assert path.read_bytes() == before


def test_symbols_normalize_and_deduplicate_without_aliasing_defaults(settings_directory):
    result = validate_settings(DEFAULT_SETTINGS | {"symbols": ["spy", " SPY ", "brk.b", "DIA"]})
    assert result["symbols"] == ["SPY", "BRK.B", "DIA"]
    result["weights"]["SPY"] = 999
    result["feature_flags"]["trace_replay"] = False
    assert DEFAULT_SETTINGS["weights"]["SPY"] == .5
    assert DEFAULT_SETTINGS["feature_flags"]["trace_replay"] is True


def test_old_generated_indicator_port_is_backed_up_and_upgraded(settings_directory):
    raw = copy.deepcopy(DEFAULT_SETTINGS) | {"ninjatrader_port": 15010, "theme": "light", "symbols": ["DIA"], "calendar_sources": ["custom.ics"]}
    path = settings_directory / "settings.json"
    original = json.dumps(raw, indent=4)
    path.write_text(original)
    preferences = settings_directory / PREFERENCES_NAME
    preferences.write_text(json.dumps({"version": 2, "explicit_symbols": True}))
    result = initialize_settings()
    assert result["status"] == "upgraded_ninjatrader_port"
    assert (settings_directory / "settings.before-ninjatrader-port-v3.json").read_text() == original
    assert json.loads(path.read_text()) == raw | {"ninjatrader_port": 5010}
    assert json.loads(preferences.read_text())["explicit_symbols"] is True
    assert initialize_settings()["status"] == "preserved"


@pytest.mark.parametrize("port", [5010, 16000])
def test_existing_custom_or_original_indicator_port_remains_unchanged(settings_directory, port):
    path = settings_directory / "settings.json"
    original = json.dumps({"ninjatrader_port": port, "theme": "light"}, indent=4)
    path.write_text(original)
    assert initialize_settings()["status"] == "preserved"
    assert path.read_text() == original


def test_explicit_return_to_15010_survives_later_startup(settings_directory):
    initialize_settings()
    save_settings_patch({"ninjatrader_port": 15010})
    assert initialize_settings()["status"] == "preserved"
    assert read_settings()["ninjatrader_port"] == 15010
    assert json.loads((settings_directory / PREFERENCES_NAME).read_text())["explicit_ninjatrader_port"] is True


def test_absent_indicator_port_uses_original_without_overwriting_other_settings(settings_directory):
    path = settings_directory / "settings.json"
    original = '{"theme": "light", "symbols": ["DIA"]}'
    path.write_text(original)
    assert initialize_settings()["status"] == "preserved"
    assert read_settings()["ninjatrader_port"] == 5010
    assert path.read_text() == original
