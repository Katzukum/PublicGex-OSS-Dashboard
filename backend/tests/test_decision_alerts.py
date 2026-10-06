from datetime import datetime, timedelta

from decision_alerts import emit_transition_alert
from models import Base, DecisionAlert, get_engine, get_session_factory


def test_alerts_dedupe_with_opposite_and_critical_overrides(tmp_path):
    engine = get_engine(tmp_path / "alerts.db")
    Base.metadata.create_all(engine)
    Session = get_session_factory(engine)
    now = datetime(2026, 8, 14, 10, 0)
    with Session.begin() as session:
        first = emit_transition_alert(session, symbol="SPX", alert_type="scenario", state_from="NO_TRADE", state_to="UPSIDE", message="up", scenario_id="s1", now=now)
        assert first["alert_id"].startswith("alert_")
        assert emit_transition_alert(session, symbol="SPX", alert_type="scenario", state_from="NO_TRADE", state_to="UPSIDE", message="same", scenario_id="s1", now=now + timedelta(minutes=1)) is None
        assert emit_transition_alert(session, symbol="SPX", alert_type="scenario", state_from="UPSIDE", state_to="DOWNSIDE", message="ordinary cooldown", scenario_id="s2", now=now + timedelta(minutes=2)) is None
        opposite = emit_transition_alert(session, symbol="SPX", alert_type="scenario", state_from="UPSIDE", state_to="NO_TRADE", message="opposite", scenario_id="s3", now=now + timedelta(minutes=3))
        assert opposite is not None
        critical = emit_transition_alert(session, symbol="SPX", alert_type="event", state_from="NORMAL", state_to="BLOCKED", message="event", severity="critical", now=now + timedelta(minutes=4))
        assert critical is not None
    with Session() as session:
        assert session.query(DecisionAlert).count() == 3
    engine.dispose()
