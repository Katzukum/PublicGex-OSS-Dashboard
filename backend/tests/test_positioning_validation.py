"""Shadow evaluation never initializes the application or its production database."""
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

import pytest

from positioning_validation import METHODS, evaluate_connection, evaluate_database, main


def _database():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE gex_snapshots (id INTEGER PRIMARY KEY, timestamp TEXT, symbol TEXT, spot_price REAL);
        CREATE TABLE raw_option_greeks (
            snapshot_id INTEGER, timestamp TEXT, symbol TEXT, expiration_date TEXT,
            osi_symbol TEXT, strike_price REAL, option_type TEXT, gamma REAL,
            open_interest INTEGER, volume INTEGER, underlying_price REAL
        );
    """)
    return conn


def _day(conn, date="2026-09-30", minutes=45, raw=True):
    start = datetime.fromisoformat(date + " 09:30:00")
    identifiers = []
    for minute in range(minutes + 1):
        timestamp = str(start + timedelta(minutes=minute))
        cursor = conn.execute(
            "INSERT INTO gex_snapshots (timestamp, symbol, spot_price) VALUES (?, 'SPX', 10000)",
            (timestamp,),
        )
        identifier = cursor.lastrowid
        identifiers.append(identifier)
        if raw:
            for index, strike in enumerate((9910, 9940, 9970, 10010, 10040, 10080)):
                for side in ("CALL", "PUT"):
                    conn.execute(
                        "INSERT INTO raw_option_greeks VALUES (?, ?, 'SPX', ?, ?, ?, ?, 0.001, ?, ?, 10000)",
                        (identifier, timestamp, date, f"{date}-{strike}-{side}", strike, side,
                         (index + 1) * 10 if side == "CALL" else 10,
                         minute * ((index + 1) * 2 if side == "CALL" else 1)),
                    )
    conn.commit()
    return identifiers


def _ready_profile(_conn, _snapshot_id):
    return {
        "coverage": {"observed_minutes": 15, "max_gap_seconds": 60, "valid_activity_contracts": 6},
        "strikes": [
            {"strike": strike, "gross_oi_gex": index + 1, "activity_15m": index + 1, "net_oi_proxy": index + 1}
            for index, strike in enumerate((10010, 10040, 10080))
        ],
    }


def test_real_features_never_use_future_raw_rows():
    conn = _database()
    _day(conn)
    before = evaluate_connection(conn, include_samples=True)
    sample = before["samples"][0]
    conn.execute(
        "UPDATE raw_option_greeks SET open_interest = 999999999, volume = 999999999 "
        "WHERE timestamp > ? AND strike_price = 9910", (sample["as_of"].replace("T", " "),),
    )
    after = evaluate_connection(conn, include_samples=True)
    assert sample["snapshot_id"] == after["samples"][0]["snapshot_id"]
    assert sample["methods"] == after["samples"][0]["methods"]


def test_one_session_is_explicitly_insufficient_holdout():
    conn = _database()
    _day(conn)
    result = evaluate_connection(conn)
    assert result["status"] == "insufficient_holdout"
    assert result["development"]["samples"] > 0
    assert result["holdout"]["samples"] == 0
    assert result["holdout_sessions"] == []
    assert all(method["touch_rate"] is None for method in result["holdout"]["methods"].values())


def test_sessions_are_split_chronologically_and_samples_are_matched_nonoverlapping():
    conn = _database()
    for day in ("2026-09-30", "2026-10-01", "2026-10-02"):
        _day(conn, day)
    result = evaluate_connection(conn, include_samples=True)
    assert result["status"] == "ready"
    assert result["development_sessions"] == ["2026-09-30", "2026-10-01"]
    assert result["holdout_sessions"] == ["2026-10-02"]
    samples = result["samples"]
    for previous, current in zip(samples, samples[1:]):
        assert datetime.fromisoformat(current["as_of"]) >= datetime.fromisoformat(previous["horizon_at"])
    for split in ("development", "holdout"):
        summary = result[split]
        assert set(summary["methods"]) == set(METHODS)
        assert all(method["samples"] == summary["samples"] for method in summary["methods"].values())
        assert all(method["levels"] == 3 * summary["samples"] for method in summary["methods"].values())
        assert all(method["mean_distance_pct"] > 0 for method in summary["methods"].values())


def test_missing_forward_horizon_is_never_labeled_using_next_session():
    conn = _database()
    _day(conn, minutes=10)
    _day(conn, "2026-10-01", minutes=10)
    result = evaluate_connection(conn, feature_loader=_ready_profile)
    assert result["status"] == "no_qualifying_samples"
    assert result["skipped_candidates"]["incomplete_forward_coverage"] == 2


def test_no_qualifying_contracts_returns_null_metrics():
    conn = _database()
    _day(conn, raw=False)
    result = evaluate_connection(conn)
    assert result["status"] == "no_qualifying_samples"
    assert result["skipped_candidates"]["insufficient_matched_features"] > 0
    assert result["development"]["methods"]["oi_concentration"]["touch_rate"] is None


def test_all_methods_exclude_entry_when_activity_is_missing_or_short():
    conn = _database()
    _day(conn, minutes=15, raw=False)
    for profile_kind in ("missing", "short", "gapped"):
        def loader(connection, snapshot_id):
            profile = _ready_profile(connection, snapshot_id)
            if profile_kind == "missing":
                profile["strikes"][0]["activity_15m"] = None
            elif profile_kind == "short":
                profile["coverage"]["observed_minutes"] = 2
            else:
                profile["coverage"]["max_gap_seconds"] = 121
            return profile
        result = evaluate_connection(conn, feature_loader=loader)
        assert result["status"] == "no_qualifying_samples"
        assert all(method["samples"] == 0 for method in result["development"]["methods"].values())


def test_future_touch_after_horizon_is_excluded():
    conn = _database()
    identifiers = _day(conn, minutes=16, raw=False)
    conn.execute("UPDATE gex_snapshots SET spot_price = 10090 WHERE id = ?", (identifiers[16],))
    result = evaluate_connection(conn, feature_loader=_ready_profile, include_samples=True)
    assert len(result["samples"]) == 1
    sample = result["samples"][0]
    assert sample["last_outcome_observation"] == sample["horizon_at"]
    assert all(not level["touched"] for levels in sample["methods"].values() for level in levels)


def test_crossing_during_horizon_counts_but_large_observation_gap_does_not():
    conn = _database()
    identifiers = _day(conn, minutes=15, raw=False)
    conn.execute("UPDATE gex_snapshots SET spot_price = 10090 WHERE id = ?", (identifiers[10],))
    result = evaluate_connection(conn, feature_loader=_ready_profile)
    assert result["development"]["methods"]["oi_concentration"]["touch_rate"] == 1
    conn.execute("DELETE FROM gex_snapshots WHERE id IN (?, ?, ?)", tuple(identifiers[5:8]))
    result = evaluate_connection(conn, feature_loader=_ready_profile)
    assert result["status"] == "no_qualifying_samples"


def test_cli_opens_existing_database_readonly_and_prints_json(tmp_path, capsys):
    conn = _database()
    _day(conn)
    path = tmp_path / "private sample.db"
    with sqlite3.connect(path) as target:
        conn.backup(target)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert main(["--db", str(path), "--symbol", "SPX"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["read_only"] is True
    assert result["status"] == "insufficient_holdout"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert not path.with_suffix(".db-wal").exists()


def test_missing_database_path_is_not_created(tmp_path):
    path = tmp_path / "missing.db"
    with pytest.raises(sqlite3.OperationalError):
        evaluate_database(path)
    assert not path.exists()
