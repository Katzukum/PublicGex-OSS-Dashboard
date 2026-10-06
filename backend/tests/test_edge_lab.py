from datetime import datetime, timedelta

from models import Base, SignalEvent, SignalOutcome, get_engine, get_session_factory
from edge_lab import query_edge_lab


def test_edge_lab_filters_and_excludes_legacy_by_default(tmp_path):
    engine = get_engine(tmp_path / "edge.db")
    Base.metadata.create_all(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        for index, opportunity in enumerate((True, True, False)):
            emitted = datetime(2026, 8, 10 + index, 10, 0)
            event = SignalEvent(
                emitted_at=emitted,
                symbol="SPX",
                spot_price=6000,
                regime="MELT UP",
                bias="CALL",
                scenario_type="UPSIDE_EXPANSION",
                session_date=emitted.date(),
                event_tags_json='["CPI"]',
                liquidity_grade="A",
                is_opportunity=opportunity,
                setup_key=f"setup-{index}",
                direction_key="SPX|CALL",
            )
            session.add(event)
            session.flush()
            session.add(SignalOutcome(signal_event_id=event.id, horizon_minutes=30, observed_at=emitted + timedelta(minutes=30), directional_move_points=2, max_favorable_points=3, max_adverse_points=1, is_win=True, outcome_label="target", target_first=True))

    with Session() as session:
        result = query_edge_lab(session, {"symbol": "SPX", "horizon": 30, "scenario": "UPSIDE_EXPANSION", "event_tag": "CPI", "liquidity_grade": "A"})
        assert result["status"] == "INSUFFICIENT"
        assert result["stats"]["independent_opportunities"] == 2
        assert result["stats"]["unique_days"] == 2
        assert len(result["opportunities"]) == 2
        legacy = query_edge_lab(session, {"symbol": "SPX", "horizon": 30, "include_legacy": True})
        assert len(legacy["opportunities"]) == 3
        assert "refresh-correlated" in legacy["warning"]
    engine.dispose()
