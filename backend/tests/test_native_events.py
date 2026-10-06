import json
import socket
import time

from sqlalchemy import select


def test_market_events_record_opportunities_and_alerts_with_ninjatrader_off(monkeypatch):
    import appy
    from models import DecisionAlert, SignalEvent, get_session_factory
    from native_runtime import NativeRuntime, RPC_LOCK
    from ninjatrader_broadcaster import broadcaster

    runtime = NativeRuntime()
    monkeypatch.setattr(runtime, "start_integrations", lambda: None)
    runtime.initialize()
    Session = get_session_factory(appy.engine)
    overview = {
        "schema_version": 2,
        "compass_traders": {"label": "MELT UP", "x_score": -0.6, "y_score": 0.95, "confidence": 0.95},
        "compass_whale": {"label": "MELT UP", "x_score": -0.5, "y_score": 0.9, "confidence": 0.95},
        "components": [{"symbol": "SPX", "spot": 6100, "flip_strike": 6090, "trend_score": 0.8,
                        "effective_gex": -100000000, "confidence": 0.95, "data_quality": {"score": 0.95}}],
        "gamma_levels": {"SPX": [{"strike": 6080, "gex": 10000000}, {"strike": 6140, "gex": -20000000}]},
    }
    monkeypatch.setattr(appy, "get_market_overview", lambda: overview.copy())
    with Session.begin() as session:
        session.add(DecisionAlert(symbol="SPX", alert_type="quality", dedupe_key="native-event-test", message="Fixture alert", severity="info"))
    try:
        with socket.create_connection(("127.0.0.1", runtime.event_port)) as connection:
            connection.sendall(json.dumps({"token": runtime.event_token, "event": {"type": "MARKET_UPDATE", "data": {}}}).encode())
        events = []
        deadline = time.monotonic() + 5
        while not events and time.monotonic() < deadline:
            with RPC_LOCK:
                events = runtime.get_events()
            if not events:
                time.sleep(0.02)
        assert events, "Authenticated collector events must be delivered"
        assert events[0]["data"]["alerts"][0]["message"] == "Fixture alert"
        with Session() as session:
            recorded = session.scalars(select(SignalEvent).where(SignalEvent.symbol == "SPX")).all()
            assert recorded
            assert recorded[-1].is_opportunity
        assert runtime.ninjatrader_port is None
        assert broadcaster.running is False
    finally:
        runtime.close()
