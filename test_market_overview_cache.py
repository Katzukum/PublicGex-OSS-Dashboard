from datetime import date, datetime, timedelta

from sqlalchemy import event as sqlalchemy_event

import appy
import signal_performance
from models import Base, CollectionRun, GexSnapshot, RawOptionGreek, get_engine, get_session_factory


def _add_snapshot(session, run, symbol, timestamp, spot, net_gex):
    snapshot = GexSnapshot(
        collection_run_id=run.id,
        timestamp=timestamp,
        symbol=symbol,
        spot_price=spot,
        total_net_gex=net_gex,
        total_call_gex=300,
        total_put_gex=-100,
        flip_strike=spot - 1,
        effective_gex=net_gex,
    )
    session.add(snapshot)
    session.flush()
    for offset, gex in ((-1, -100), (1, 300)):
        session.add(
            RawOptionGreek(
                snapshot_id=snapshot.id,
                timestamp=timestamp,
                symbol=symbol,
                expiration_date=date.today(),
                osi_symbol=f"{symbol}{snapshot.id}{offset}",
                strike_price=spot + offset,
                option_type="PUT" if gex < 0 else "CALL",
                delta=-0.4 if gex < 0 else 0.4,
                gamma=0.01,
                open_interest=10,
                underlying_price=spot,
                gex_value=gex,
            )
        )
    return snapshot


def _seed_overview(db_engine):
    Session = get_session_factory(db_engine)
    timestamp = datetime(2026, 8, 14, 10, 0)
    with Session.begin() as session:
        run = CollectionRun(status="success", symbols_requested="SPY,IWM,NDX,SPX")
        session.add(run)
        session.flush()
        for symbol, spot, net_gex in (
            ("SPY", 640, 200),
            ("IWM", 220, -200),
            ("NDX", 24000, -300),
            ("SPX", 6500, 300),
        ):
            _add_snapshot(session, run, symbol, timestamp, spot, net_gex)


def _select_count(db_engine, callback):
    statements = []

    def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    sqlalchemy_event.listen(db_engine, "before_cursor_execute", count_selects)
    try:
        value = callback()
    finally:
        sqlalchemy_event.remove(db_engine, "before_cursor_execute", count_selects)
    return value, len(statements)


def test_overview_uses_bounded_bulk_queries_and_snapshot_cache(tmp_path, monkeypatch):
    db_engine = get_engine(tmp_path / "overview.db")
    Base.metadata.create_all(db_engine)
    _seed_overview(db_engine)
    settings = {
        "weights": {"SPY": 0.7, "IWM": 0.3},
        "weights_whale": {"NDX": 0.5, "SPX": 0.3, "IWM": 0.2},
    }
    edge_version = {"value": 0}

    def edge_stats(*args, **kwargs):
        edge_version["value"] += 1
        return {"version": edge_version["value"]}

    monkeypatch.setattr(appy, "engine", db_engine)
    monkeypatch.setattr(appy, "DB_SCHEMA_CURRENT", True)
    monkeypatch.setattr(appy, "_load_settings", lambda: settings)
    monkeypatch.setattr(signal_performance, "edge_stats_for_dashboard", edge_stats)
    appy._clear_overview_cache()

    first, first_queries = _select_count(db_engine, appy.get_market_overview)
    second, cached_queries = _select_count(db_engine, appy.get_market_overview)

    assert first_queries == 2
    assert cached_queries == 1
    assert [row["symbol"] for row in first["components"]].count("IWM") == 1
    assert first["gamma_levels"] == second["gamma_levels"]
    assert first["edge_stats"] != second["edge_stats"]

    Session = get_session_factory(db_engine)
    with Session.begin() as session:
        run = CollectionRun(status="success", symbols_requested="IWM")
        session.add(run)
        session.flush()
        _add_snapshot(
            session,
            run,
            "IWM",
            datetime(2026, 8, 14, 10, 1),
            225,
            -250,
        )

    after_snapshot, snapshot_queries = _select_count(db_engine, appy.get_market_overview)
    assert snapshot_queries == 2
    assert next(row for row in after_snapshot["components"] if row["symbol"] == "IWM")["spot"] == 225

    settings["weights"] = {"SPY": 0.2, "IWM": 0.8}
    _, weight_queries = _select_count(db_engine, appy.get_market_overview)
    assert weight_queries == 2

    db_engine.dispose()
    appy._clear_overview_cache()


def test_saving_settings_clears_overview_cache(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    appy._overview_cache_key = ("cached",)
    appy._overview_cache_value = {"compass": {}}

    result = appy.save_settings({"symbols": ["SPY"], "weights": {"SPY": 1.0}})

    assert result["ok"]
    assert appy._overview_cache_key is None
    assert appy._overview_cache_value is None
