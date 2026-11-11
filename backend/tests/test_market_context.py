import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from market_context import EASTERN, CachedIcsProvider, atm_straddle_implied_move, build_market_context, classify_event_window, load_public_calendar_events, parse_ics_events, persist_market_context, realized_volatility_15m, stored_timestamp_to_eastern
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


def test_calendar_failure_is_unavailable_without_raising(tmp_path):
    result = load_public_calendar_events(tmp_path, fetch=lambda _url: (_ for _ in ()).throw(OSError("offline")))
    assert result["events"] is None
    assert set(result["sources"].values()) == {"unavailable"}
    assert len(result["warnings"]) == 2
    context = build_market_context("SPX", 6000, [], [], result["events"])
    assert context["event_risk"]["state"] == "UNAVAILABLE"


def test_stored_naive_timestamps_convert_from_source_zone_to_same_et_instant():
    instant = datetime(2026, 11, 10, 23, 30, tzinfo=timezone.utc)
    utc_naive = instant.replace(tzinfo=None)
    pacific_naive = instant.astimezone(ZoneInfo("America/Los_Angeles")).replace(tzinfo=None)
    assert stored_timestamp_to_eastern(utc_naive, timezone.utc) == stored_timestamp_to_eastern(pacific_naive, ZoneInfo("America/Los_Angeles"))


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


def test_realized_volatility_uses_fifteen_elapsed_minutes_at_subminute_cadence():
    start = datetime(2026, 11, 10, 10, 0, tzinfo=EASTERN)
    history = [
        {"timestamp": (start + timedelta(seconds=30 * i)).isoformat(),
         "spot_price": 100 + (i // 2) * 0.1 + (0.03 if i % 2 else 0)}
        for i in range(32)
    ]
    context = build_market_context("SPX", 101.53, [], history, [], now=start + timedelta(minutes=15, seconds=30))
    minute_closes = [history[2 * minute + 1]["spot_price"] for minute in range(16)]
    assert context["realized_volatility_15m"] == realized_volatility_15m(minute_closes)
    assert context["realized_volatility_15m"] != realized_volatility_15m([row["spot_price"] for row in history[-16:]])


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
