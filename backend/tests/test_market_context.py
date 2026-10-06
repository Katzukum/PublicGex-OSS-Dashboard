import os
from datetime import datetime, timedelta, timezone

from market_context import EASTERN, CachedIcsProvider, atm_straddle_implied_move, build_market_context, classify_event_window, parse_ics_events, persist_market_context
from models import Base, MarketContextSnapshot, get_engine, get_session_factory


ICS = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:cpi-1
DTSTART;TZID=America/New_York:20260308T083000
SUMMARY:CPI Release
END:VEVENT
BEGIN:VEVENT
UID:cpi-1
DTSTART;TZID=America/New_York:20260308T083000
SUMMARY:CPI Release duplicate
END:VEVENT
END:VCALENDAR
"""


def test_ics_normalizes_dst_and_deduplicates_uid():
    events = parse_ics_events(ICS, "BLS")
    assert len(events) == 1
    assert events[0]["starts_at"].tzinfo is not None
    assert events[0]["starts_at"].utcoffset() == timedelta(hours=-4)


def test_event_windows_do_not_mix_quality_with_eligibility():
    event = parse_ics_events(ICS, "BLS")[0]
    now = event["starts_at"] - timedelta(minutes=5)
    assert classify_event_window([event], now)["state"] == "BLOCKED"
    assert classify_event_window([event], now - timedelta(minutes=30))["state"] == "CAUTION"


def test_cache_expiry_and_network_failure_keep_stale_data(tmp_path):
    cache = tmp_path / "events.ics"
    cache.write_text(ICS, encoding="utf-8")
    old = datetime.now(timezone.utc) - timedelta(days=2)
    os.utime(cache, (old.timestamp(), old.timestamp()))
    provider = CachedIcsProvider("BLS", "https://example.invalid", cache, fetch=lambda _url: (_ for _ in ()).throw(OSError("offline")))
    result = provider.load(datetime.now(timezone.utc))
    assert result["status"] == "stale_cache"
    assert len(result["events"]) == 1
    assert result["warnings"] == ["offline"]


def test_missing_quotes_return_null_implied_move():
    assert atm_straddle_implied_move([], 6000) is None
    context = build_market_context("SPX", 6000, [], [], [], now=datetime.now(EASTERN))
    assert context["implied_move"] is None
    assert context["range_consumed"] is None
    assert context["status"] == "partial"


def test_atm_straddle_and_range_consumed():
    rows = [
        {"strike_price": 6000, "option_type": "CALL", "bid": 9, "ask": 11},
        {"strike_price": 6000, "option_type": "PUT", "mid_price": 12},
    ]
    history = [{"spot_price": 5990}, {"spot_price": 6000}, {"spot_price": 6012}]
    context = build_market_context("SPX", 6000, rows, history, [])
    assert context["implied_move"] == 22
    assert context["range_consumed"] == 1


def test_context_persists_to_temporary_database(tmp_path):
    engine = get_engine(tmp_path / "context.db")
    Base.metadata.create_all(engine)
    context = build_market_context("SPX", 6000, [], [], [], now=datetime.now(EASTERN))
    Session = get_session_factory(engine)
    with Session.begin() as session:
        persist_market_context(session, context)
    with Session() as session:
        row = session.query(MarketContextSnapshot).one()
        assert row.symbol == "SPX"
        assert row.event_state == "NORMAL"
    engine.dispose()
