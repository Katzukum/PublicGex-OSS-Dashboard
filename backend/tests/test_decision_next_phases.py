"""Pure fixtures for the observational book, quote scoring, and model gate."""

from datetime import date, datetime, timedelta
import json
from math import exp
from types import SimpleNamespace

from edge_model import FEATURES, predict, signal_features
from estimated_flow import classify_interval, summarize_intervals
from option_pnl import mark_structure, select_structure
from schema_migrations import CURRENT_SCHEMA_VERSION, run_migrations
from term_book import merged_strike_book, oi_change, persist_daily_oi, select_expirations
from positioning_validation import validate_term_level_sources
from signal_performance import _build_outcome
from models import Base, OiDaily, SignalEvent


def row(osi, strike, side, bid, ask):
    return SimpleNamespace(osi_symbol=osi, strike_price=strike, option_type=side, bid=bid, ask=ask)


def test_spread_and_butterfly_cross_both_spreads_and_fees():
    entry = [row("C100", 100, "CALL", 4, 5), row("C105", 105, "CALL", 2, 3),
             row("C95", 95, "CALL", 7, 8)]
    exit_ = [row("C100", 100, "CALL", 6, 7), row("C105", 105, "CALL", 1, 2),
             row("C95", 95, "CALL", 8, 9)]
    spread = select_structure(entry, "CALL", "UPSIDE_EXPANSION", 100, 105)
    assert spread == [("C100", 1), ("C105", -1)]
    pnl, multiple, reason = mark_structure(spread, entry, exit_, 1)
    assert (pnl, reason) == (96.0, None)
    assert round(multiple, 6) == round(96 / 302, 6)
    fly = select_structure(entry, "NEUTRAL", "PIN", 100, 100)
    assert fly == [("C95", 1), ("C100", -2), ("C105", 1)]
    fly_pnl, _, _ = mark_structure(fly, entry, exit_, 1)
    assert fly_pnl == -808.0
    assert mark_structure(spread, entry, exit_[:1], 1)[2] == "missing_leg"
    puts = [row("P95", 95, "PUT", 1, 2), row("P100", 100, "PUT", 3, 4),
            row("P105", 105, "PUT", 6, 7)]
    assert select_structure(puts, "NEUTRAL", "PIN", 101, 100) == [
        ("P95", 1), ("P100", -2), ("P105", 1)]


def test_term_mapping_conserves_dollar_gex_and_oi_requires_two_days():
    merged = merged_strike_book(
        [{"strike_price": 5000, "gex_value": 10}, {"strike_price": 5010, "gex_value": -3}],
        [{"strike_price": 500, "gex_value": 4}, {"strike_price": 501, "gex_value": 8}],
        5000, 500)
    assert merged == [{"strike_price": 5000.0, "gex_value": 14.0},
                      {"strike_price": 5010.0, "gex_value": 5.0}]
    changes = oi_change({"old": {"open_interest": 12}, "new": {"open_interest": 5}},
                        {"old": {"open_interest": 10, "volume": 7}, "expired": {"open_interest": 1}})
    assert changes["old"] == {"change": 2, "prior_volume": 7, "estimate": "opened"}
    assert changes["new"] is None
    assert "expired" not in changes
    listed = [date(2026, 11, day) for day in (10, 11, 12, 13, 16, 20)]
    assert select_expirations(listed, date(2026, 11, 10), 4) == listed


def test_flow_excludes_reset_and_gap_and_counts_unclassified_volume():
    now = datetime(2026, 11, 10, 10)
    old = {"timestamp": now, "volume": 10, "bid": 1, "ask": 2}
    buy = classify_interval(old, {"timestamp": now + timedelta(seconds=4), "volume": 15,
                                  "last_price": 2, "delta": .5}, 100)
    middle = classify_interval(old, {"timestamp": now + timedelta(seconds=4), "volume": 13,
                                     "last_price": 1.5, "delta": .5}, 100)
    assert buy["estimated_flow"] == 25000
    assert middle["status"] == "unclassified"
    assert summarize_intervals([buy, middle])["coverage"] == 5 / 8
    assert classify_interval(old, {"timestamp": now + timedelta(seconds=150), "volume": 16}, 100)["status"] == "excluded"
    assert classify_interval(old, {"timestamp": now + timedelta(seconds=4), "volume": 9}, 100)["status"] == "excluded"


def test_predictor_requires_review_and_matches_hand_logit(tmp_path):
    path = tmp_path / "model.json"
    family = {"publish": True, "features": list(FEATURES), "means": [0] * 5,
              "scales": [1] * 5, "coefficients": [1, 0, 0, 0, 0], "intercept": -.5}
    artifact = {"user_reviewed": False, "families": {"pin": family}}
    path.write_text(json.dumps(artifact))
    values = dict.fromkeys(FEATURES, 0)
    values["regime"] = 1
    assert predict(values, "PIN", path)[0] is None
    artifact["user_reviewed"] = True
    path.write_text(json.dumps(artifact))
    assert round(predict(values, "PIN", path)[0], 6) == round(1 / (1 + exp(-.5)), 6)
    del values["regime"]
    assert predict(values, "PIN", path)[0] is None


def test_model_features_ignore_post_signal_fields():
    workspace = {"as_of": "2026-11-10T10:00:00", "regime": "positive",
                 "levels": {"zero_gamma": 99, "call_wall": 102, "put_wall": 98, "implied_move": 4},
                 "market_context": {"range_consumed": .5},
                 "dashboard": {"snapshot": {"spot_price": 100}}}
    frozen = signal_features(workspace)
    workspace["future_outcome"] = {"target_first": True, "end_spot": 9999}
    workspace["dashboard"]["snapshot"]["future_price"] = 9999
    assert signal_features(workspace) == frozen


def test_additive_evidence_migrations(tmp_path):
    path = tmp_path / "history.db"
    import sqlite3
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE signal_outcomes (id INTEGER PRIMARY KEY, outcome_label TEXT)")
        connection.execute("INSERT INTO signal_outcomes VALUES (1, 'target')")
        connection.execute("PRAGMA user_version = 5")
    assert run_migrations(path) == CURRENT_SCHEMA_VERSION
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT outcome_label, option_pnl FROM signal_outcomes WHERE id = 1").fetchone() == ("target", None)
        assert connection.execute("SELECT name FROM sqlite_master WHERE name = 'oi_daily'").fetchone()


def test_term_level_validation_uses_only_matched_holdout_sessions():
    rows = []
    for day in range(1, 11):
        for source, hit in (("0dte", day < 9), ("combined_expirations", True), ("merged_index_etf", False)):
            rows.append({"session_date": f"2026-11-{day:02d}", "source": source,
                         "distance_pct": .3, "touched": hit})
    report = validate_term_level_sources(rows)
    assert report["holdout_sessions"] == 2
    assert report["sources"]["combined_expirations"]["may_feed_v2"] is True
    assert report["sources"]["merged_index_etf"]["may_feed_v2"] is True  # equal to 0DTE on holdout


def test_minute_bar_with_both_levels_counts_invalidation_first():
    start = datetime(2026, 11, 10, 10)
    event = SignalEvent(id=1, engine="scenario_v2", emitted_at=start,
                        spot_price=100, bias="CALL", target=102, invalidation=98,
                        scenario_type="UPSIDE_EXPANSION")
    bars = [SimpleNamespace(timestamp=start + timedelta(minutes=i), spot_price=100,
                            high=103 if i == 1 else 100, low=97 if i == 1 else 100)
            for i in range(1, 16)]
    outcome = _build_outcome(event, 15, bars, [row.timestamp for row in bars])
    assert outcome.outcome_label == "invalidation_first"
    assert outcome.path_resolution_seconds == 60
    assert outcome.option_exit_at == start + timedelta(minutes=2)


def test_daily_oi_keeps_first_reading_after_update_time():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    snapshot = SimpleNamespace(symbol="SPX", timestamp=datetime(2026, 11, 10, 9, 36))
    rows = [{"osi_symbol": "OPT", "strike_price": 100, "option_type": "CALL",
             "open_interest": 10, "volume": 3}]
    with Session.begin() as session:
        persist_daily_oi(session, snapshot, rows, datetime(2026, 11, 10, 9, 25))
        assert session.query(OiDaily).count() == 0
        persist_daily_oi(session, snapshot, rows, datetime(2026, 11, 10, 9, 36))
        rows[0]["open_interest"] = 20
        persist_daily_oi(session, snapshot, rows, datetime(2026, 11, 10, 12))
        assert session.query(OiDaily).one().open_interest == 10
    engine.dispose()


def test_regime_study_recovers_planted_volatility_sign():
    import runpy
    from pathlib import Path
    study = runpy.run_path(str(Path(__file__).resolve().parents[2] / "scripts" / "regime-study.py"))
    points = []
    for day in (10, 11, 12):
        for minute in range(31):
            points.append((datetime(2026, 11, day, 10) + timedelta(minutes=minute),
                           101 + (minute % 2) * .01, 1, 100))
    for day in (13, 14, 15):
        for minute in range(31):
            points.append((datetime(2026, 11, day, 10) + timedelta(minutes=minute),
                           99 + (minute % 2) * 1.0, -1, 101))
    report, observations = study["study_series"](points)
    assert report["regimes"]["positive"]["30"]["mean_forward_realized_volatility"] < report["regimes"]["negative"]["30"]["mean_forward_realized_volatility"]
    assert study["difference_interval"](observations)[1] < 0
