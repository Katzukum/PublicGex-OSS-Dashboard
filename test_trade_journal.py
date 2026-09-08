from datetime import date, datetime

import pytest

from models import Base, SignalEvent, TradeJournalEntry, get_engine, get_session_factory
from trade_journal import create_entry, delete_entry, list_entries, update_entry, weekly_review


def _payload(**updates):
    payload = {
        "symbol": "SPX",
        "session_date": "2026-08-14",
        "scenario_id": "scenario-1",
        "scenario_type": "UPSIDE_EXPANSION",
        "contracts": 2,
        "entry_price": 1.0,
        "exit_price": 1.5,
        "entry_time": "2026-08-14T10:00:00",
        "exit_time": "2026-08-14T10:30:00",
        "fees": 2,
        "expected_entry": 0.9,
        "adhered_to_plan": True,
    }
    payload.update(updates)
    return payload


def test_journal_crud_validation_linking_and_weekly_review(tmp_path):
    engine = get_engine(tmp_path / "journal.db")
    Base.metadata.create_all(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        session.add(SignalEvent(emitted_at=datetime(2026, 8, 14, 9, 30), symbol="SPX", session_date=date(2026, 8, 14), scenario_id="scenario-1", scenario_type="UPSIDE_EXPANSION", setup_key="x", direction_key="SPX|CALL"))
    with Session.begin() as session:
        created = create_entry(session, _payload())
        assert created["pnl"] == 98
        assert created["slippage"] == pytest.approx(20)
        entry_id = created["id"]
    with Session.begin() as session:
        updated = update_entry(session, entry_id, {"notes": "reviewed"})
        assert updated["notes"] == "reviewed"
        assert len(list_entries(session, "SPX")) == 1
        review = weekly_review(session, "2026-08-10")
        assert review["entries"][0]["id"] == entry_id
        assert any(group["dimension"] == "adherence" and group["value"] == "ADHERED" for group in review["groups"])
        assert delete_entry(session, entry_id)
    with Session() as session:
        assert session.query(TradeJournalEntry).count() == 0
    engine.dispose()


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"contracts": -1}, "contracts must be a positive integer"),
        ({"entry_time": "not-a-time"}, "entry_time must be an ISO timestamp"),
        ({"exit_time": "2026-08-14T09:00:00"}, "exit_time cannot precede entry_time"),
    ],
)
def test_journal_rejects_malformed_input(tmp_path, updates, message):
    engine = get_engine(tmp_path / "invalid.db")
    Base.metadata.create_all(engine)
    Session = get_session_factory(engine)
    with Session() as session, pytest.raises(ValueError, match=message):
        create_entry(session, _payload(scenario_id=None, **updates))
    engine.dispose()


def test_linked_scenario_must_match_symbol_and_session(tmp_path):
    engine = get_engine(tmp_path / "link.db")
    Base.metadata.create_all(engine)
    Session = get_session_factory(engine)
    with Session.begin() as session:
        session.add(SignalEvent(emitted_at=datetime(2026, 8, 13), symbol="NDX", session_date=date(2026, 8, 13), scenario_id="scenario-1", setup_key="x", direction_key="NDX|CALL"))
    with Session() as session, pytest.raises(ValueError, match="linked scenario"):
        create_entry(session, _payload())
    engine.dispose()
