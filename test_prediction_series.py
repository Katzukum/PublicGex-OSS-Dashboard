from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3

import pytest

from prediction.series import SeriesRegistry


NOW = datetime(2026, 9, 30, 20, tzinfo=timezone.utc)


def history(day=29, prices=(6000., 6001., 6002.)):
    start = NOW.replace(day=day, hour=14, minute=0)
    return [{"end_utc": (start + timedelta(minutes=5 * i)).isoformat(), "close": price}
            for i, price in enumerate(prices)]


def select(registry, rows=None, **overrides):
    args = {"instrument": "ES 12-26", "history_policy": "DoNotMerge", "feed_label": "Provider A",
            "history": history() if rows is None else rows, "received_at": NOW}
    args.update(overrides)
    return registry.select(**args)


def seed(record, rows):
    # Registry reads only the public bar schema; no application/store imports.
    path = record["data_dir"] / "forecasts.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS bars(instrument TEXT,end_utc TEXT,close REAL,PRIMARY KEY(instrument,end_utc))")
        for row in rows:
            stamp = datetime.fromisoformat(row["end_utc"].replace("Z", "+00:00")).astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
            connection.execute("INSERT OR IGNORE INTO bars VALUES (?,?,?)", (record["instrument"], stamp, row["close"]))


def test_compatible_append_duplicates_empty_attach_and_restart_stable(tmp_path):
    registry = SeriesRegistry(tmp_path)
    first = select(registry)
    seed(first, history())
    appended = history(prices=(6000., 6001., 6002., 6003.))
    next_selection = select(registry, appended)
    assert next_selection["series_id"] == first["series_id"]
    assert next_selection["revision"] == 1 and next_selection["is_current"]
    assert next_selection["selection_history_fingerprint"] != first["history_fingerprint"]
    assert next_selection["history_fingerprint"] == first["history_fingerprint"]
    seed(first, appended)
    assert select(registry, appended + appended)["series_id"] == first["series_id"]
    assert select(registry, [])["series_id"] == first["series_id"]
    reopened = SeriesRegistry(tmp_path)
    assert select(reopened, appended)["data_dir"] == first["data_dir"]
    assert len(reopened.list_series()) == 1


def test_price_correction_revises_without_mutating_old_files(tmp_path):
    registry = SeriesRegistry(tmp_path)
    first = select(registry)
    seed(first, history())
    old_metadata = (first["data_dir"] / "series.json").read_bytes()
    old_database = (first["data_dir"] / "forecasts.sqlite3").read_bytes()
    corrected = history(prices=(5990., 5991., 5992.))
    second = select(registry, corrected)
    assert second["revision"] == 2 and second["is_current"]
    assert second["profile_id"] == first["profile_id"]
    assert second["series_id"] != first["series_id"]
    assert second["data_dir"] != first["data_dir"]
    assert second["selection_reason"] == "overlapping_price_correction"
    assert (first["data_dir"] / "series.json").read_bytes() == old_metadata
    assert (first["data_dir"] / "forecasts.sqlite3").read_bytes() == old_database
    assert [r["is_current"] for r in registry.list_series()] == [False, True]


def test_stale_chart_returns_retired_revision_without_pointer_flip_or_new_revision(tmp_path):
    registry = SeriesRegistry(tmp_path)
    first = select(registry)
    seed(first, history())
    corrected = history(prices=(5990., 5991., 5992.))
    second = select(registry, corrected)
    seed(second, corrected)
    manifest = second["data_dir"].parent / "manifest.json"
    before = manifest.read_bytes()
    stale_with_new_bars = history(prices=(6000., 6001., 6002., 6005.))
    retired = select(registry, stale_with_new_bars)
    assert retired["series_id"] == first["series_id"]
    assert retired["is_current"] is False
    assert retired["selection_reason"] == "retired_history_basis"
    assert manifest.read_bytes() == before
    assert len(registry.list_series()) == 2
    assert select(registry, corrected)["series_id"] == second["series_id"]
    explicit_reversion = select(registry, stale_with_new_bars, feed_label="Provider A basis v2")
    assert explicit_reversion["is_current"] and explicit_reversion["profile_id"] != first["profile_id"]


def test_unseen_correction_forks_instead_of_false_retired_match(tmp_path):
    registry = SeriesRegistry(tmp_path)
    first = select(registry)
    seed(first, history())
    second = select(registry, history(prices=(5990., 5991., 5992.)))
    seed(second, history(prices=(5990., 5991., 5992.)))
    # One point matches the old basis, but another conflicts with it.
    third = select(registry, history(prices=(6000., 6001., 6009.)))
    assert third["revision"] == 3 and third["is_current"]


def test_policy_provider_and_expiry_isolation_and_safe_paths(tmp_path):
    registry = SeriesRegistry(tmp_path)
    first = select(registry)
    seed(first, history())
    others = [select(registry, history_policy="MergeBackAdjusted"),
              select(registry, history_policy="MergeNonBackAdjusted"),
              select(registry, feed_label="Provider B"),
              select(registry, instrument="ES 03-27"),
              select(registry, [], feed_label="../../provider/../label")]
    assert len({r["profile_id"] for r in [first, *others]}) == 6
    assert all(r["revision"] == 1 for r in others)
    assert all(r["data_dir"].is_relative_to(tmp_path.resolve()) for r in others)
    assert not (others[-1]["data_dir"] / "forecasts.sqlite3").exists()
    assert select(registry, feed_label=" Provider A ")["series_id"] == first["series_id"]


def test_disjoint_nonempty_history_forks_and_empty_new_profile_starts_separate(tmp_path):
    registry = SeriesRegistry(tmp_path)
    first = select(registry)
    seed(first, history())
    second = select(registry, history(day=28))
    assert second["revision"] == 2
    assert second["selection_reason"] == "nonoverlapping_history"
    assert select(registry, [], feed_label="new feed")["revision"] == 1


def test_existing_legacy_database_is_untouched_and_not_automatically_attached(tmp_path):
    legacy = tmp_path / "forecasts.sqlite3"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE bars(instrument TEXT,end_utc TEXT,close REAL)")
        connection.execute("INSERT INTO bars VALUES ('ES 12-26','2026-09-29T14:00:00Z',12345)")
    before = legacy.read_bytes()
    registry = SeriesRegistry(tmp_path)
    selected = select(registry)
    assert selected["revision"] == 1
    assert selected["data_dir"] != tmp_path
    assert legacy.read_bytes() == before
    assert not (selected["data_dir"] / "forecasts.sqlite3").exists()


@pytest.mark.parametrize("label", [None, "", "   ", "x" * 65, "a\nb", "\tProvider", "abc\x7f", "abc\x85"])
def test_invalid_labels_fail_before_creating_profiles(tmp_path, label):
    registry = SeriesRegistry(tmp_path)
    with pytest.raises(ValueError, match="feed_label"):
        select(registry, feed_label=label)
    assert registry.list_series() == []


def test_invalid_policy_conflicting_input_and_future_data_fail_closed(tmp_path):
    registry = SeriesRegistry(tmp_path)
    with pytest.raises(ValueError, match="history_policy"):
        select(registry, history_policy="auto")
    bad = history() + [dict(history()[0], close=1)]
    with pytest.raises(ValueError, match="conflicting duplicate"):
        select(registry, bad)
    with pytest.raises(ValueError, match="future"):
        select(registry, history(day=30), received_at=NOW.replace(hour=13))
    assert registry.list_series() == []


def test_complete_revision_recovers_after_manifest_pointer_interruption(tmp_path):
    registry = SeriesRegistry(tmp_path)
    first = select(registry)
    seed(first, history())
    corrected = history(prices=(5990., 5991., 5992.))
    second = select(registry, corrected)
    seed(second, corrected)
    manifest_path = first["data_dir"].parent / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["current_revision"] = 1
    manifest_path.write_text(json.dumps(manifest))
    reopened = SeriesRegistry(tmp_path)
    assert select(reopened, corrected)["series_id"] == second["series_id"]
    assert json.loads(manifest_path.read_text())["current_revision"] == 2


def test_reads_committed_wal_bars_without_checkpointing_or_mutating_database(tmp_path):
    registry = SeriesRegistry(tmp_path)
    first = select(registry)
    database = first["data_dir"] / "forecasts.sqlite3"
    writer = sqlite3.connect(database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE bars(instrument TEXT,end_utc TEXT,close REAL)")
        writer.execute("INSERT INTO bars VALUES (?,?,?)", (first["instrument"], history()[0]["end_utc"], 6000.))
        writer.commit()
        wal = Path(str(database) + "-wal")
        assert wal.stat().st_size > 0
        before_database, before_wal = database.read_bytes(), wal.read_bytes()
        selected = select(registry, [dict(history()[0], close=5990.)])
        assert selected["revision"] == 2
        assert selected["selection_reason"] == "overlapping_price_correction"
        assert database.read_bytes() == before_database
        assert wal.read_bytes() == before_wal
    finally:
        writer.close()
