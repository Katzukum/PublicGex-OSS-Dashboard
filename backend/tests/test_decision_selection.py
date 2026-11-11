from decision_selection import dashboard_from_scenario, selected_scenario
from datetime import datetime

from sqlalchemy import create_engine
from models import Base, SignalEvent, SignalOutcome, get_session_factory
from signal_performance import edge_stats_for_scenario


def _versions():
    v1 = {
        "active_scenario": {"scenario_id": "old", "scenario_type": "NO_TRADE", "bias": "WAIT"},
        "eligibility": {"eligible": False, "blockers": ["old blocker"]},
        "decision": {"bias": "WAIT", "score": 0.42, "target": None, "invalidation": None,
                     "market_vote": {"score": 0.42}},
    }
    v2 = {
        "active_scenario": {"scenario_id": "new", "scenario_type": "PIN", "bias": "NEUTRAL",
                            "target": 6000, "invalidation": None,
                            "pin_band_low": 5990, "pin_band_high": 6010,
                            "reasons": ["pin concentration"], "warnings": []},
        "decision": {"bias": "NEUTRAL", "target": 6000, "invalidation": None,
                     "pin_band_low": 5990, "pin_band_high": 6010},
        "eligibility": {"eligible": True, "blockers": []},
        "market_context": {"event_risk": {"state": "NORMAL"}},
    }
    return v1, v2


def test_selected_engine_projects_v2_without_losing_market_context_votes():
    v1, v2 = _versions()
    old = selected_scenario(v1, v2, "scenario_v1")
    new = selected_scenario(v1, v2, "scenario_v2")
    assert old["active_scenario"] == v1["active_scenario"]
    assert {key: value for key, value in old["decision"].items() if key != "engine"} == v1["decision"]
    assert new["active_scenario"]["scenario_id"] == "new"
    assert new["decision"]["target"] == 6000
    assert new["decision"]["pin_band_low"] == 5990
    assert new["decision"]["market_vote"] == {"score": 0.42}
    assert new["eligibility"]["event_state"] == "NORMAL"


def test_ninjatrader_mapping_preserves_dashboard_keys_and_null_pin_invalidation():
    v1, v2 = _versions()
    selected = selected_scenario(v1, v2, "scenario_v2")
    workspace = {**selected, "decision_engine": "scenario_v2"}
    original = {"dashboard_symbol": "SPX", "dashboard_bias": "CALL BIAS",
                "dashboard_bias_score": 0.5, "dashboard_target": 6010,
                "dashboard_invalidation": 5980, "dashboard_context": "legacy",
                "dashboard_flip": 5995}
    mapped = dashboard_from_scenario(original, workspace)
    assert set(mapped) == set(original)
    assert mapped["dashboard_bias"] == "WAIT"
    assert mapped["dashboard_target"] == 6000
    assert mapped["dashboard_invalidation"] is None
    assert "5990 to 6010" in mapped["dashboard_context"]
    assert dashboard_from_scenario(original, {**workspace, "decision_engine": "scenario_v1"}) is original


def test_directional_v2_maps_bias_and_both_price_levels():
    v1, v2 = _versions()
    v2["active_scenario"].update({"scenario_type": "UPSIDE_EXPANSION", "bias": "CALL",
                                  "target": 6020, "invalidation": 5985})
    v2["decision"].update({"bias": "CALL", "target": 6020, "invalidation": 5985})
    workspace = {**selected_scenario(v1, v2, "scenario_v2"), "decision_engine": "scenario_v2"}
    mapped = dashboard_from_scenario({"dashboard_bias": "WAIT", "dashboard_target": None,
                                      "dashboard_invalidation": None, "dashboard_context": ""}, workspace)
    assert (mapped["dashboard_bias"], mapped["dashboard_target"], mapped["dashboard_invalidation"]) == (
        "CALL BIAS", 6020, 5985)


def test_selected_edge_uses_only_v2_outcomes_for_the_selected_scenario():
    db_engine = create_engine("sqlite://")
    Base.metadata.create_all(db_engine)
    Session = get_session_factory(db_engine)
    with Session.begin() as session:
        for engine, scenario, won in (("scenario_v1", "PIN", False),
                                      ("scenario_v2", "PIN", True),
                                      ("scenario_v2", "FADE_CALL_WALL", False)):
            event = SignalEvent(engine=engine, emitted_at=datetime(2026, 11, 10, 15), symbol="SPX",
                                scenario_type=scenario, is_opportunity=True)
            session.add(event)
            session.flush()
            session.add(SignalOutcome(signal_event_id=event.id, label_version=2, horizon_minutes=30,
                                      observed_at=datetime(2026, 11, 10, 15, 30), is_win=won,
                                      outcome_label="target_first" if won else "invalidation_first"))
    with Session() as session:
        primary = edge_stats_for_scenario("SPX", "PIN", session=session)["primary"]
        assert primary["sample_size"] == 1
        assert primary["win_rate"] == 1.0
        assert primary["sample_label"] == "scenario v2"
    db_engine.dispose()


def test_broadcaster_switch_preserves_the_schema_v2_payload_keys(monkeypatch):
    import app_settings
    import signal_performance
    from ninjatrader_broadcaster import build_regime_payload

    monkeypatch.setattr(signal_performance, "update_signal_performance_for_payload",
                        lambda payload, session=None, scenario_types=None: payload)
    overview = {"compass_traders": {"label": "NEUTRAL", "x_score": 0, "y_score": 0},
                "components": [{"symbol": "SPX", "spot": 6000, "flip_strike": 5995,
                                "confidence": 0.8, "net_gex": 100}]}
    v1, v2 = _versions()
    workspace = {**selected_scenario(v1, v2, "scenario_v2"), "decision_engine": "scenario_v2"}
    monkeypatch.setattr(app_settings, "read_settings", lambda: {"decision_engine": "scenario_v1"})
    old = build_regime_payload(overview, decision_workspaces={"SPX": workspace})
    monkeypatch.setattr(app_settings, "read_settings", lambda: {"decision_engine": "scenario_v2"})
    new = build_regime_payload(overview, decision_workspaces={"SPX": workspace})
    assert old is not None and new is not None
    assert set(new) == set(old)
    assert new["dashboard_target_spx"] == 6000
    assert new["dashboard_invalidation_spx"] is None
    assert new["dashboard_bias_spx"] == "WAIT"
    assert new["dashboard_context_spx"].startswith("Scenario v2: PIN")
