import sqlite3

import pytest

from schema_migrations import CURRENT_SCHEMA_VERSION, run_migrations


def _legacy_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE raw_option_greeks (
                id INTEGER PRIMARY KEY, snapshot_id INTEGER, symbol TEXT, gex_value REAL
            );
            INSERT INTO raw_option_greeks VALUES (1, 7, 'SPX', 123.5);
            CREATE TABLE signal_events (
                id INTEGER PRIMARY KEY, emitted_at DATETIME, symbol TEXT
            );
            INSERT INTO signal_events VALUES (1, '2026-08-14 10:00:00', 'SPX');
            """
        )


def test_legacy_database_migrates_without_data_loss(tmp_path):
    db_path = tmp_path / "legacy.db"
    _legacy_database(db_path)

    assert run_migrations(db_path) == CURRENT_SCHEMA_VERSION

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT symbol, gex_value FROM raw_option_greeks").fetchone() == (
            "SPX",
            123.5,
        )
        quote_columns = {row[1] for row in connection.execute("PRAGMA table_info(raw_option_greeks)")}
        signal_columns = {row[1] for row in connection.execute("PRAGMA table_info(signal_events)")}
        assert {"bid", "ask", "mid_price", "implied_volatility"} <= quote_columns
        assert {"data_quality", "edge_probability", "scenario_id", "is_opportunity"} <= signal_columns
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='trade_journal_entries'"
        ).fetchone()


def test_migrations_are_idempotent(tmp_path):
    db_path = tmp_path / "repeat.db"
    _legacy_database(db_path)
    run_migrations(db_path)
    before = db_path.stat().st_size
    assert run_migrations(db_path) == CURRENT_SCHEMA_VERSION
    assert db_path.stat().st_size == before


def test_failed_migration_does_not_advance_version(tmp_path):
    db_path = tmp_path / "interrupted.db"
    _legacy_database(db_path)

    def fail_on_second(version, _connection):
        if version == 2:
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError, match="simulated interruption"):
        run_migrations(db_path, before_step=fail_on_second)

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1

    assert run_migrations(db_path) == CURRENT_SCHEMA_VERSION


def test_backup_is_created_only_for_pending_existing_database(tmp_path):
    db_path = tmp_path / "production-copy.db"
    _legacy_database(db_path)
    run_migrations(db_path, create_backup=True)
    backups = list(tmp_path.glob("production-copy_pre_migration_*.db"))
    assert len(backups) == 1
    run_migrations(db_path, create_backup=True)
    assert list(tmp_path.glob("production-copy_pre_migration_*.db")) == backups
