import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from models import Base, CollectionRun, GexSnapshot, SignalEvent, SignalOutcome
from signal_performance import edge_stats_for_dashboard, label_due_outcomes, setup_keys


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _run(session):
    run = CollectionRun(started_at=datetime(2026, 6, 22, 9, 30), status="success")
    session.add(run)
    session.flush()
    return run


def _snapshot(session, run, symbol, timestamp, spot):
    row = GexSnapshot(
        collection_run_id=run.id,
        timestamp=timestamp,
        symbol=symbol,
        spot_price=spot,
        total_net_gex=0,
        total_call_gex=0,
        total_put_gex=0,
    )
    session.add(row)
    return row


def _event(session, symbol="NDX", emitted_at=datetime(2026, 6, 22, 9, 30), spot=100, target=103, invalidation=98):
    dashboard = {
        "dashboard_symbol": symbol,
        "dashboard_bias": "CALL BIAS",
        "dashboard_bias_score": 0.48,
        "dashboard_dealer": "Dealer: Long Gamma",
        "dashboard_liquidity": "Liquidity: Upside Open",
    }
    setup_key, direction_key = setup_keys(symbol, dashboard, "GRIND UP")
    event = SignalEvent(
        emitted_at=emitted_at,
        symbol=symbol,
        spot_price=spot,
        regime="GRIND UP",
        bias="CALL",
        bias_score=0.48,
        confidence=0.8,
        target=target,
        invalidation=invalidation,
        market_state="CALL",
        dealer_state="LONG GAMMA",
        liquidity_state="UPSIDE OPEN",
        setup_key=setup_key,
        direction_key=direction_key,
        payload_json="{}",
    )
    session.add(event)
    session.flush()
    return event, dashboard


class SignalPerformanceTests(unittest.TestCase):
    def test_label_due_outcome_records_target_first_win(self):
        session = _session()
        run = _run(session)
        event, _ = _event(session)
        _snapshot(session, run, "NDX", event.emitted_at + timedelta(minutes=5), 101)
        _snapshot(session, run, "NDX", event.emitted_at + timedelta(minutes=10), 103.25)
        _snapshot(session, run, "NDX", event.emitted_at + timedelta(minutes=15), 102)
        session.commit()

        labeled = label_due_outcomes(session=session, horizons=(15,))

        self.assertEqual(labeled, 1)
        outcome = session.query(SignalOutcome).filter_by(signal_event_id=event.id).one()
        self.assertTrue(outcome.is_win)
        self.assertTrue(outcome.target_first)
        self.assertEqual(outcome.outcome_label, "target")
        self.assertEqual(outcome.directional_move_points, 2)
        self.assertEqual(outcome.max_favorable_points, 3.25)

    def test_edge_stats_falls_back_to_symbol_bias_when_exact_sample_is_small(self):
        session = _session()
        run = _run(session)
        base_time = datetime(2026, 6, 22, 9, 30)

        for idx in range(3):
            event, dashboard = _event(
                session,
                emitted_at=base_time + timedelta(minutes=idx),
                spot=100,
                target=102,
                invalidation=98,
            )
            _snapshot(session, run, "NDX", event.emitted_at + timedelta(minutes=30), 101 + idx)

        session.commit()
        label_due_outcomes(session=session, horizons=(30,))

        stats = edge_stats_for_dashboard(dashboard, "GRIND UP", session=session)
        primary = stats["primary"]

        self.assertEqual(primary["sample_label"], "symbol+bias")
        self.assertEqual(primary["sample_size"], 3)
        self.assertEqual(primary["win_rate"], 1.0)
        self.assertEqual(primary["median_move_points"], 2)


if __name__ == "__main__":
    unittest.main()
