from datetime import date, datetime

import appy
import ninjatrader_broadcaster
import signal_performance
from models import Base, CollectionRun, GexSnapshot, RawOptionGreek, get_engine, get_session_factory


def _seed_directional_overview(db_engine):
    Session = get_session_factory(db_engine)
    with Session.begin() as session:
        run = CollectionRun(status="complete", symbols_requested="SPY", symbols_succeeded="SPY")
        session.add(run)
        session.flush()
        snapshot = GexSnapshot(
            collection_run_id=run.id,
            timestamp=datetime.now(),
            symbol="SPY",
            spot_price=110,
            total_net_gex=-200,
            total_call_gex=100,
            total_put_gex=-300,
            flip_strike=100,
            effective_gex=-200,
        )
        session.add(snapshot)
        session.flush()
        for offset in range(20):
            session.add(
                RawOptionGreek(
                    snapshot_id=snapshot.id,
                    timestamp=snapshot.timestamp,
                    symbol="SPY",
                    expiration_date=date.today(),
                    osi_symbol=f"SPY{offset}",
                    strike_price=95 + offset,
                    option_type="PUT",
                    delta=-0.4,
                    gamma=0.01,
                    open_interest=10,
                    underlying_price=110,
                    gex_value=-10,
                )
            )


def test_live_overview_compass_flows_to_ninjatrader_payload(tmp_path, monkeypatch):
    db_engine = get_engine(tmp_path / "contract.db")
    Base.metadata.create_all(db_engine)
    _seed_directional_overview(db_engine)

    monkeypatch.setattr(appy, "engine", db_engine)
    monkeypatch.setattr(appy, "DB_SCHEMA_CURRENT", True)
    monkeypatch.setattr(
        appy,
        "_load_settings",
        lambda: {"weights": {"SPY": 1.0}, "weights_whale": {}},
    )
    monkeypatch.setattr(signal_performance, "label_due_outcomes", lambda *args, **kwargs: 0)
    monkeypatch.setattr(signal_performance, "edge_stats_for_dashboard", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        signal_performance,
        "update_signal_performance_for_payload",
        lambda payload, *args, **kwargs: payload,
    )

    overview = appy.get_market_overview()
    assert "error" not in overview
    assert overview["compass"] == overview["compass_traders"]
    assert overview["compass_traders"]["label"] == "MELT UP"

    sent = []
    monkeypatch.setattr(ninjatrader_broadcaster.broadcaster, "broadcast", sent.append)
    assert ninjatrader_broadcaster.send_regime_update(overview)

    payload = sent[0]
    compass = overview["compass_traders"]
    assert payload["regime"] == "MELT UP"
    assert payload["regime_code"] == 2
    assert payload["confidence"] == compass["confidence_label"]
    assert payload["confidence_score"] == round(compass["confidence"], 4)
    assert payload["x_score"] == round(compass["x_score"], 4)
    assert payload["y_score"] == round(compass["y_score"], 4)

    db_engine.dispose()


def test_broadcaster_falls_back_to_legacy_compass(monkeypatch):
    overview = {
        "compass": {
            "label": "CRASH / FLUSH",
            "confidence": 0.8,
            "confidence_label": "HIGH",
            "x_score": -0.7,
            "y_score": -0.9,
        },
        "components": [],
    }
    sent = []
    monkeypatch.setattr(ninjatrader_broadcaster.broadcaster, "broadcast", sent.append)
    monkeypatch.setattr(
        signal_performance,
        "update_signal_performance_for_payload",
        lambda payload, *args, **kwargs: payload,
    )

    assert ninjatrader_broadcaster.send_regime_update(overview)
    assert sent[0]["regime"] == "CRASH / FLUSH"
    assert sent[0]["regime_code"] == 4
