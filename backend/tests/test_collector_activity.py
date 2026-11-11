from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from models import Base, CollectionRun, GexSnapshot, RawOptionGreek


def test_chain_native_greeks_accept_sdk_attribute_objects(monkeypatch):
    import publicData as collector

    osi = "SPXW261111C07600000"
    option = SimpleNamespace(
        instrument=SimpleNamespace(symbol=osi), open_interest=10, volume=3,
        bid=10, ask=11, last=10.5,
        option_details=SimpleNamespace(strike_price=7600,
                                       greeks=SimpleNamespace(gamma=0.02, delta=0.5, theta=-0.1)),
    )
    monkeypatch.setattr(collector, "get_instrument_type", lambda _: "INDEX")
    monkeypatch.setattr(collector, "OrderInstrument", SimpleNamespace)
    monkeypatch.setattr(collector, "OptionChainRequest", SimpleNamespace)
    monkeypatch.setattr(collector, "get_0dte_expiration", lambda *_: "2026-11-11")
    monkeypatch.setattr(collector, "get_option_greeks_batch", lambda *_: pytest.fail("extra Greeks call"))
    monkeypatch.setattr(collector, "get_option_quotes_batch", lambda *_: pytest.fail("extra quotes call"))
    client = SimpleNamespace(get_quotes=lambda _: [{"last": 7600}],
                             get_option_chain=lambda _: [option])
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        run = CollectionRun(status="running")
        session.add(run)
        session.flush()
        result = collector.process_symbol(client, session, run, "SPX",
                                          {"account_id": "fixture", "settings": {"chain_fields_confirmed": True}},
                                          SimpleNamespace(wait=lambda: None), emit_events=False)
        assert result["status"] == "saved", result
        row = session.scalar(select(RawOptionGreek))
        assert row.gamma == 0.02
        assert row.bid == 10 and row.ask == 11
    engine.dispose()


def test_slow_collection_uses_response_time_for_pressure_and_replay(monkeypatch):
    import publicData as collector
    from flow_pressure import build_estimated_pressure
    from research_observations import load_session_observations

    class Clock(datetime):
        current = datetime(2026, 10, 7, 10, 0, 55)

        @classmethod
        def now(cls):
            return cls.current

    monkeypatch.setattr(collector, "datetime", Clock)
    monkeypatch.setattr(collector, "get_instrument_type", lambda _: "INDEX")
    monkeypatch.setattr(collector, "OrderInstrument", SimpleNamespace)
    monkeypatch.setattr(collector, "OptionChainRequest", SimpleNamespace)
    monkeypatch.setattr(collector, "get_0dte_expiration", lambda *_: "2026-10-07")
    monkeypatch.setattr(collector, "get_option_greeks_batch", lambda *_: {
        "NDX-call": {"gamma": .02, "delta": .5, "theta": -.1},
    })
    volume = 0

    def delayed_quotes(*_):
        # Reproduce NDX quotes arriving >5 seconds after request start,
        # across a minute boundary. Provider clocks are serialized as UTC.
        Clock.current += timedelta(seconds=12)
        quote_time = Clock.current + timedelta(hours=4, seconds=-1)
        return {"NDX-call": {"volume": volume, "bid": 1., "ask": 2., "last": 2.,
                             "bid_timestamp": quote_time, "ask_timestamp": quote_time,
                             "last_timestamp": quote_time}}

    monkeypatch.setattr(collector, "get_option_quotes_batch", delayed_quotes)
    client = SimpleNamespace(
        get_quotes=lambda _: [{"last": 100}],
        get_option_chain=lambda _: [{"instrument": {
            "symbol": "NDX-call", "strike_price": 100, "option_type": "CALL",
        }, "open_interest": 10}],
    )
    events = []
    import event_utils
    monkeypatch.setattr(event_utils, "send_event", lambda name, payload: events.append((name, payload)))
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        run = CollectionRun(status="running")
        session.add(run)
        session.flush()
        for minute in range(7):
            Clock.current = datetime(2026, 10, 7, 10, minute, 55)
            volume = minute * 10
            result = collector.process_symbol(client, session, run, "NDX", {"account_id": "fixture"},
                                              SimpleNamespace(wait=lambda: None))
            assert result["status"] == "saved", result
            snapshot = session.get(GexSnapshot, result["snapshot_id"])
            assert snapshot.timestamp == Clock.current
            rows = list(session.scalars(select(RawOptionGreek).where(RawOptionGreek.snapshot_id == snapshot.id)))
            assert all(row.timestamp == snapshot.timestamp for row in rows)
            assert events[-1][1]["timestamp"] == str(snapshot.timestamp)

        conn = session.connection()
        early = load_session_observations(conn, symbol="NDX", as_of="2026-10-07 10:01:00", snapshot_id=snapshot.id)
        assert early["snapshots"] == []  # First response is not available until 10:01:07.
        observed = load_session_observations(conn, symbol="NDX", as_of=snapshot.timestamp, snapshot_id=snapshot.id)
        pressure = build_estimated_pressure(observed["rows"], symbol="NDX", as_of=observed["available_at"])
        assert pressure["status"] == "ready"
        assert pressure["coverage"]["classification_ratio"] == 1.
        assert pressure["coverage"]["discarded_by_reason"].get("future_quote", 0) == 0
        assert pressure["points"][0]["timestamp"] == "2026-10-07T10:03:00"
        assert pressure["points"][0]["cumulative_dollars"] == 50_000

        # A truly future quote is still rejected: do not widen the skew guard.
        for row in observed["rows"]:
            row["bid_timestamp_utc"] = "2026-10-07T15:00:00Z"
        rejected = build_estimated_pressure(observed["rows"], symbol="NDX", as_of=observed["available_at"])
        assert rejected["status"] == "unavailable"
        assert rejected["coverage"]["discarded_by_reason"]["future_quote"] == 6
    engine.dispose()


def test_collector_preserves_zero_oi_activity_without_inventing_positioning(monkeypatch):
    import publicData as collector

    # Contract 101 trades despite zero OI. Contract 102 has activity but a
    # missing gamma; retain that observation without manufacturing exposure.
    specifications = [(100, "CALL", 10, 0.02, 200), (101, "CALL", 0, 0.04, 125),
                      (102, "PUT", 0, None, 3), (99, "PUT", 0, 0.02, 0),
                      (98, "PUT", 0, 0.0, None)]
    options = []
    greeks, quotes = {}, {}
    for strike, side, oi, gamma, volume in specifications:
        osi = f"fixture-{strike}-{side}"
        options.append({"instrument": {"symbol": osi, "strike_price": strike, "option_type": side},
                        "open_interest": oi})
        greeks[osi] = {"gamma": gamma, "delta": 0.5, "theta": -0.1}
        quotes[osi] = {"volume": volume, "bid": 1.0, "ask": 1.1}

    monkeypatch.setattr(collector, "get_instrument_type", lambda _symbol: "INDEX")
    monkeypatch.setattr(collector, "OrderInstrument", SimpleNamespace)
    monkeypatch.setattr(collector, "OptionChainRequest", SimpleNamespace)
    monkeypatch.setattr(collector, "get_0dte_expiration", lambda *_args: "2026-10-02")
    monkeypatch.setattr(collector, "get_option_greeks_batch", lambda *_args: greeks)
    monkeypatch.setattr(collector, "get_option_quotes_batch", lambda *_args: quotes)
    client = SimpleNamespace(get_quotes=lambda _instruments: [{"last": 100}],
                             get_option_chain=lambda _request: options)
    limiter = SimpleNamespace(wait=lambda: None)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        run = CollectionRun(status="running")
        session.add(run)
        session.flush()
        result = collector.process_symbol(client, session, run, "SPX", {"account_id": "fixture"},
                                          limiter, emit_events=False)
        assert result["status"] == "saved", result
        rows = list(session.scalars(select(RawOptionGreek).order_by(RawOptionGreek.strike_price)))
        assert [row.strike_price for row in rows] == [99, 100, 101, 102]
        active = next(row for row in rows if row.strike_price == 101)
        assert active.open_interest == 0
        assert active.volume == 125
        assert active.gamma == 0.04
        assert active.gex_value == 0
        assert active.expiration_date == date(2026, 10, 2)
        missing_gamma = next(row for row in rows if row.strike_price == 102)
        assert missing_gamma.volume == 3
        assert missing_gamma.gamma is None
        snapshot = session.get(GexSnapshot, result["snapshot_id"])
        assert snapshot.total_net_gex == pytest.approx(0.02 * 10 * 100 * 100 ** 2 * 0.01)
        assert snapshot.total_put_gex == 0
        assert snapshot.max_call_gex_strike == 100
        assert snapshot.max_put_gex_strike == 0
    engine.dispose()


def test_zero_oi_observations_do_not_change_the_collector_broadcast_levels():
    from publicData import build_overview_data

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        run = CollectionRun(status="success")
        session.add(run)
        session.flush()
        snapshot = GexSnapshot(collection_run_id=run.id, symbol="SPX", spot_price=100.5,
                               total_net_gex=3000, flip_strike=100, effective_gex=3000)
        session.add(snapshot)
        session.flush()
        for strike, gex in [(100, 2000), (102, 1000)]:
            session.add(RawOptionGreek(snapshot_id=snapshot.id, symbol="SPX", strike_price=strike,
                                       option_type="CALL", open_interest=10, gex_value=gex))
        session.flush()
        before = build_overview_data(session, {"weights": {"SPX": 1}})
        session.add(RawOptionGreek(snapshot_id=snapshot.id, symbol="SPX", strike_price=101,
                                   option_type="CALL", open_interest=0, gex_value=0, volume=500))
        session.flush()
        after = build_overview_data(session, {"weights": {"SPX": 1}})
        assert after == before
        assert next(row for row in after["components"] if "acceleration" in row)["acceleration"] == -500
    engine.dispose()


def test_zero_oi_activity_does_not_create_a_legacy_backtest_gamma_pit():
    import sqlite3
    from backtest_gamma_butterflies import _raw_option_rows, _score_gamma_pit_levels, _strike_summary

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("""
            CREATE TABLE raw_option_greeks (
                snapshot_id INTEGER, expiration_date TEXT, osi_symbol TEXT,
                strike_price REAL, option_type TEXT, delta REAL, gamma REAL,
                open_interest INTEGER, underlying_price REAL, gex_value REAL, volume INTEGER
            )
        """)
        for strike, gex in ((100, 2000), (102, 1000)):
            connection.execute("INSERT INTO raw_option_greeks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                               (1, "2026-10-02", f"fixture-{strike}", strike, "CALL", .5, .02,
                                10, 100.5, gex, 100))
        before = _strike_summary(_raw_option_rows(connection, 1))
        _score_gamma_pit_levels(before, pit_window=1)
        # Newly retained activity sits between existing walls. Including its
        # zero GEX would manufacture a pit that the legacy universe never had.
        connection.execute("INSERT INTO raw_option_greeks VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (1, "2026-10-02", "fixture-101", 101, "CALL", .5, .02,
                            0, 100.5, 0, 500))
        after = _strike_summary(_raw_option_rows(connection, 1))
        _score_gamma_pit_levels(after, pit_window=1)
        assert after == before
        assert set(after) == {100, 102}
        assert all(level["pit_score"] == 0 for level in after.values())
    finally:
        connection.close()
