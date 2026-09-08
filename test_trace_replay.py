from datetime import date, datetime

import appy
from models import Base, CollectionRun, GexSnapshot, RawOptionGreek, get_engine, get_session_factory


def test_trace_is_bounded_to_requested_symbol_and_session(tmp_path, monkeypatch):
    engine = get_engine(tmp_path / "trace.db")
    Base.metadata.create_all(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        run = CollectionRun(started_at=datetime(2026, 8, 13, 9, 30), status="complete")
        session.add(run)
        session.flush()
        for day, symbol in ((13, "SPX"), (14, "SPX"), (14, "NDX")):
            timestamp = datetime(2026, 8, day, 10, 0)
            snapshot = GexSnapshot(collection_run_id=run.id, timestamp=timestamp, symbol=symbol, spot_price=100, flip_strike=99, total_net_gex=100, total_call_gex=150, total_put_gex=-50)
            session.add(snapshot)
            session.flush()
            session.add(RawOptionGreek(snapshot_id=snapshot.id, timestamp=timestamp, symbol=symbol, expiration_date=date(2026, 8, day), osi_symbol=f"{symbol}{day}C", strike_price=100, option_type="CALL", delta=0.5, gamma=0.02, open_interest=10, underlying_price=100, gex_value=100))
    monkeypatch.setattr(appy, "engine", engine)
    monkeypatch.setattr(appy, "DB_SCHEMA_CURRENT", True)
    assert appy.get_trace_dates("SPX") == ["2026-08-14", "2026-08-13"]
    trace = appy.get_trace_data("SPX", 390, "2026-08-13")
    assert trace["session_date"] == "2026-08-13"
    assert {row["timestamp"][:10] for row in trace["heatmap"]} == {"2026-08-13"}
    assert all(row["modeled_delta_pressure"] != 0 for row in trace["heatmap"])
    assert "modeled_charm_pressure" in trace["heatmap"][0]
    assert trace["history"][0]["total_net_gex"] == 100
    assert trace["history"][0]["timestamp"].startswith("2026-08-13")
    trace["heatmap"].clear()
    assert appy.get_trace_data("SPX", 390, "2026-08-13")["heatmap"]
    engine.dispose()


def test_trace_uses_latest_poll_per_minute_instead_of_multiplying_gamma(tmp_path, monkeypatch):
    engine = get_engine(tmp_path / 'minute.db')
    Base.metadata.create_all(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        run = CollectionRun(started_at=datetime(2026, 8, 13, 9, 30), status='complete')
        session.add(run)
        session.flush()
        for seconds, gamma in ((0, 100), (30, 200)):
            stamp = datetime(2026, 8, 13, 10, 0, seconds)
            snap = GexSnapshot(collection_run_id=run.id, timestamp=stamp, symbol='SPX', spot_price=100, total_net_gex=gamma)
            session.add(snap)
            session.flush()
            session.add(RawOptionGreek(snapshot_id=snap.id, timestamp=stamp, symbol='SPX', expiration_date=date(2026,8,13), osi_symbol=f'SPX{seconds}', strike_price=100, option_type='CALL', delta=.5, gamma=.02, open_interest=10, underlying_price=100, gex_value=gamma))
    monkeypatch.setattr(appy, 'engine', engine)
    monkeypatch.setattr(appy, 'DB_SCHEMA_CURRENT', True)
    payload = appy.get_trace_data('SPX', 390, '2026-08-13')
    assert len(payload['heatmap']) == 1
    assert payload['heatmap'][0]['net_gex'] == 200
    assert [row['total_net_gex'] for row in payload['history']] == [100, 200]
    engine.dispose()
