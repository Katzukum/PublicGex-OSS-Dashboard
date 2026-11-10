import json
from datetime import datetime

from models import Base, CollectionRun, GexSnapshot, RawOptionGreek, SignalEvent, get_engine, get_session_factory
from publicData import record_collector_decisions
from signal_performance import record_signal_payload


def test_collector_records_scenarios_for_all_symbols_without_bridge(tmp_path, monkeypatch):
    import market_context

    starting_now = {"source": "BLS", "title": "Fixture release", "starts_at": datetime.now(market_context.EASTERN)}
    monkeypatch.setattr(market_context, "load_public_calendar_events", lambda _path: {
        "events": [starting_now], "warnings": [], "sources": {"BLS": "cached", "FOMC": "cached"},
    })
    symbols = ("SPX", "SPY", "QQQ", "NDX", "IWM")
    db_engine = get_engine(tmp_path / "collector-decisions.db")
    Base.metadata.create_all(db_engine)
    Session = get_session_factory(db_engine)
    with Session.begin() as session:
        run = CollectionRun(started_at=datetime(2026, 11, 10, 10), status="success")
        session.add(run)
        session.flush()
        for symbol in symbols:
            snapshot = GexSnapshot(
                collection_run_id=run.id, timestamp=datetime(2026, 11, 10, 10), symbol=symbol,
                spot_price=100, total_net_gex=1000, total_call_gex=2000, total_put_gex=-1000,
                flip_strike=100, effective_gex=1000,
            )
            session.add(snapshot)
            session.flush()
            for strike in (95, 100, 105):
                for side, sign in (("CALL", 1), ("PUT", -1)):
                    session.add(RawOptionGreek(
                        snapshot_id=snapshot.id, timestamp=snapshot.timestamp, symbol=symbol,
                        strike_price=strike, option_type=side, open_interest=10, gamma=0.01,
                        gex_value=sign * 100, bid=1.0, ask=1.2, mid_price=1.1,
                        expiration_date=snapshot.timestamp.date(),
                    ))

    assert record_collector_decisions(db_engine, list(symbols)) == 10
    assert record_collector_decisions(db_engine, list(symbols)) == 0
    with Session() as session:
        scenarios = session.query(SignalEvent).filter_by(engine="scenario_v1").all()
        shadows = session.query(SignalEvent).filter_by(engine="scenario_v2").all()
        assert {row.symbol for row in scenarios} == set(symbols)
        assert {row.symbol for row in shadows} == set(symbols)
        assert all(row.scenario_type == "NO_TRADE" for row in scenarios)
        assert all("Fixture release" in row.event_tags_json for row in scenarios)
        votes = session.query(SignalEvent).filter_by(engine="broadcaster_vote").all()
        assert len(votes) == 2
        assert record_signal_payload(json.loads(votes[0].payload_json), session=session) == 0
    db_engine.dispose()
