from datetime import datetime, timedelta
import sqlite3

import numpy as np
import pytest

from research.price_models.data import build_blocks, load_blocks, read_only_database, session_bounds


def _rows(start="2026-09-08 09:30:00", minutes=120, poll_seconds=30):
    opening = datetime.fromisoformat(start)
    return [dict(id=i + 1, symbol="SPX", timestamp=opening + timedelta(seconds=i * poll_seconds),
                 spot_price=6000 + i / 100, total_net_gex=40., total_call_gex=100., total_put_gex=-60.,
                 max_call_gex_strike=6010., max_put_gex_strike=5990.)
            for i in range(minutes * 60 // poll_seconds + 1)]


def test_session_boundaries_calendar_and_no_overnight_rolling_anchor():
    rows = _rows() + _rows("2026-09-09 09:30:00") + _rows("2026-09-07 09:30:00")
    blocks, audit = build_blocks(rows, source="synthetic")
    assert {b["session"] for b in blocks} == {"2026-09-08", "2026-09-09"}
    for block in blocks:
        assert np.all(block["timestamp"].astype("datetime64[D]") == np.datetime64(block["session"]))
        assert block["timestamp"][0] == np.datetime64(block["session"] + "T10:00")
        assert np.all(np.diff(block["timestamp"]) == np.timedelta64(5, "m"))
    assert audit["counts"]["outside_cash_session_rows"] == len(_rows())
    assert session_bounds("2025-01-09") is None
    assert session_bounds("2026-11-27")[1].hour == 13
    assert session_bounds("2026-12-24")[1].hour == 13
    with pytest.raises(ValueError, match="only verified"):
        session_bounds("2027-01-04")


def test_causal_observation_and_prior_feature_with_strict_staleness():
    rows = _rows()
    # At 10:30 price is 90 seconds old (allowed), feature is 120 (allowed).
    cutoff = datetime.fromisoformat("2026-09-08 10:30")
    rows = [r for r in rows if not cutoff - timedelta(seconds=90) < r["timestamp"] <= cutoff]
    # At 11:00 newest price is 91 seconds old: reject and split the block.
    cutoff2 = datetime.fromisoformat("2026-09-08 11:00")
    rows = [r for r in rows if not cutoff2 - timedelta(seconds=91) < r["timestamp"] <= cutoff2]
    blocks, audit = build_blocks(rows, source="synthetic", min_block_points=1, price_anchor_window=1)
    stamps = np.concatenate([b["timestamp"] for b in blocks])
    assert np.datetime64(cutoff) in stamps
    assert np.datetime64(cutoff2) not in stamps
    for block in blocks:
        assert np.all(block["feature_at"] < block["observed_at"])
        assert np.all(block["observed_at"] <= block["timestamp"])
        assert np.all(block["timestamp"] - block["observed_at"] <= np.timedelta64(90, "s"))
        assert np.all(block["timestamp"] - block["feature_at"] <= np.timedelta64(150, "s"))
        assert np.all(np.diff(block["timestamp"]) == np.timedelta64(5, "m"))
    assert audit["counts"]["stale_price_grid_points"] > 0


def test_stale_prior_feature_is_rejected_even_with_fresh_price():
    rows = _rows(minutes=90)
    target = datetime.fromisoformat("2026-09-08 10:00")
    rows = [r for r in rows if not target - timedelta(seconds=151) < r["timestamp"] < target]
    blocks, audit = build_blocks(rows, source="synthetic", min_block_points=1, price_anchor_window=1)
    assert np.datetime64(target) not in np.concatenate([b["timestamp"] for b in blocks])
    assert audit["counts"]["stale_feature_grid_points"] >= 1


def test_future_perturbation_does_not_change_past_values():
    rows = _rows(minutes=240)
    cutoff = datetime.fromisoformat("2026-09-08 11:30")
    changed = [dict(r) for r in rows]
    for row in changed:
        if row["timestamp"] > cutoff:
            row["spot_price"] *= 1.1
            row["total_net_gex"] = 20.
            row["total_put_gex"] = -80.
            row["max_call_gex_strike"] *= 1.2
    first, _ = build_blocks(rows, source="synthetic")
    second, _ = build_blocks(changed, source="synthetic")
    for a, b in zip(first, second, strict=True):
        mask = a["timestamp"] <= np.datetime64(cutoff)
        for name in ("y", "g", "anchor", "price_anchor", "feature_at", "observed_at"):
            np.testing.assert_array_equal(a[name][mask], b[name][mask])


def test_identical_duplicates_removed_conflicting_duplicates_fail():
    rows = _rows()
    blocks, audit = build_blocks(rows + rows, source="synthetic")
    original, _ = build_blocks(rows, source="synthetic")
    assert audit["counts"]["duplicate_rows"] == len(rows)
    np.testing.assert_array_equal(blocks[0]["y"], original[0]["y"])
    bad = dict(rows[0], spot_price=1)
    with pytest.raises(ValueError, match="Conflicting"):
        build_blocks(rows + [bad], source="synthetic")


def _database(path, mismatch=False):
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE gex_snapshots (id INTEGER, collection_run_id INTEGER, symbol TEXT, timestamp TEXT, spot_price REAL, total_net_gex REAL, total_call_gex REAL, total_put_gex REAL, max_call_gex_strike REAL, max_put_gex_strike REAL)")
        con.execute("CREATE TABLE raw_option_greeks (snapshot_id INTEGER, option_type TEXT, gex_value REAL, strike_price REAL, gamma REAL, open_interest INTEGER)")
        con.execute("CREATE INDEX raw_snapshot_idx ON raw_option_greeks(snapshot_id)")
        for row in _rows():
            con.execute("INSERT INTO gex_snapshots VALUES (?,?,?,?,?,?,?,?,?,?)", (row["id"], 1, row["symbol"], str(row["timestamp"]), *[row[k] for k in ("spot_price", "total_net_gex", "total_call_gex", "total_put_gex", "max_call_gex_strike", "max_put_gex_strike")]))
            con.executemany("INSERT INTO raw_option_greeks VALUES (?,?,?,?,?,?)", [(row["id"], "CALL", 100., 6010., 1., 100), (row["id"], "PUT", -60. if not mismatch else -20., 5990., .6, 100)])


def test_readonly_sqlite_semantic_verification_and_cross_source_dedup(tmp_path):
    first, second = tmp_path / "one.db", tmp_path / "two.db"
    _database(first)
    _database(second)
    before = first.read_bytes()
    with read_only_database(first) as connection:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM gex_snapshots")
    blocks, audit = load_blocks(sources=[first, second], symbols=("SPX",))
    assert first.read_bytes() == before
    assert blocks
    assert all(s["semantics"]["compatible"] for s in audit["sources"])
    assert audit["sources"][1]["cross_source_duplicate_rows"] == len(_rows())
    mismatch = tmp_path / "mismatch.db"
    _database(mismatch, mismatch=True)
    bad_blocks, bad_audit = load_blocks(sources=[mismatch], symbols=("SPX",))
    assert not bad_blocks
    assert bad_audit["sources"][0]["excluded"]


def test_legacy_polling_has_distinct_bounded_feature_age():
    blocks, audit = build_blocks(_rows(minutes=180, poll_seconds=180), source="legacy", legacy=True)
    assert blocks
    assert audit["max_price_age_seconds"] == 210
    assert audit["max_feature_age_seconds"] == 420
    for block in blocks:
        assert np.all(block["feature_at"] < block["observed_at"])
        assert np.all(block["timestamp"] - block["observed_at"] <= np.timedelta64(210, "s"))
        assert np.all(block["timestamp"] - block["feature_at"] <= np.timedelta64(420, "s"))
