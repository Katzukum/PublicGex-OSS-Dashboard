"""Ordered, additive SQLite migrations for the decision-workspace schema.

Migrations intentionally use sqlite3 rather than SQLAlchemy metadata so an
existing multi-gigabyte database can be upgraded in place.  Every operation is
idempotent, and ``PRAGMA user_version`` advances only after a migration commits.
"""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Callable


CURRENT_SCHEMA_VERSION = 10


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone()
    return row is not None


def _add_columns(connection: sqlite3.Connection, table: str, definitions: dict[str, str]) -> None:
    if not _table_exists(connection, table):
        return
    existing = _columns(connection, table)
    for name, definition in definitions.items():
        if name not in existing:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _migration_1_quotes(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "raw_option_greeks",
        {
            "bid": "REAL",
            "ask": "REAL",
            "mid_price": "REAL",
            "last_price": "REAL",
            "bid_size": "INTEGER",
            "ask_size": "INTEGER",
            "volume": "INTEGER",
            "implied_volatility": "REAL",
            "bid_timestamp": "DATETIME",
            "ask_timestamp": "DATETIME",
            "last_timestamp": "DATETIME",
        },
    )


def _migration_2_signal_evidence(connection: sqlite3.Connection) -> None:
    _add_columns(
        connection,
        "signal_events",
        {
            "data_quality": "REAL",
            "edge_probability": "REAL",
            "state_key": "TEXT",
            "scenario_type": "TEXT",
            "scenario_id": "TEXT",
            "session_date": "DATE",
            "event_tags_json": "TEXT DEFAULT '[]'",
            "liquidity_grade": "TEXT",
            "is_opportunity": "BOOLEAN DEFAULT 0",
        },
    )
    if _table_exists(connection, "signal_events"):
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_state_time "
            "ON signal_events(symbol, state_key, emitted_at)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_opportunity_session "
            "ON signal_events(is_opportunity, session_date, symbol)"
        )


def _migration_3_decision_tables(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS market_context_snapshots (
            id INTEGER PRIMARY KEY,
            captured_at DATETIME NOT NULL,
            symbol TEXT NOT NULL,
            session_date DATE,
            event_state TEXT NOT NULL DEFAULT 'NORMAL',
            implied_move REAL,
            range_consumed REAL,
            realized_volatility_15m REAL,
            cross_asset_state TEXT,
            volatility_term_json TEXT NOT NULL DEFAULT '{}',
            warnings_json TEXT NOT NULL DEFAULT '[]',
            source_status_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_market_context_symbol_time
            ON market_context_snapshots(symbol, captured_at);

        CREATE TABLE IF NOT EXISTS decision_alerts (
            id INTEGER PRIMARY KEY,
            created_at DATETIME NOT NULL,
            symbol TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            severity TEXT NOT NULL DEFAULT 'info',
            scenario_id TEXT,
            dedupe_key TEXT NOT NULL UNIQUE,
            state_from TEXT,
            state_to TEXT,
            message TEXT NOT NULL,
            payload_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_decision_alert_symbol_time
            ON decision_alerts(symbol, created_at);

        CREATE TABLE IF NOT EXISTS trade_journal_entries (
            id INTEGER PRIMARY KEY,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL,
            symbol TEXT NOT NULL,
            session_date DATE NOT NULL,
            scenario_id TEXT,
            scenario_type TEXT,
            regime TEXT,
            event_tags_json TEXT NOT NULL DEFAULT '[]',
            liquidity_grade TEXT,
            contracts INTEGER NOT NULL,
            entry_price REAL NOT NULL,
            exit_price REAL,
            entry_time DATETIME NOT NULL,
            exit_time DATETIME,
            fees REAL NOT NULL DEFAULT 0,
            pnl REAL,
            slippage REAL,
            underlying_mfe REAL,
            underlying_mae REAL,
            exit_reason TEXT,
            adhered_to_plan BOOLEAN,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_journal_symbol_session
            ON trade_journal_entries(symbol, session_date);
        """
    )


def _migration_4_versioned_signal_evidence(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "signal_events", {"engine": "TEXT"})
    _add_columns(connection, "signal_outcomes", {
        "label_version": "INTEGER NOT NULL DEFAULT 1",
        "r_multiple": "REAL",
        "path_resolution_seconds": "REAL",
        "pin_held": "BOOLEAN",
    })
    if _table_exists(connection, "signal_events"):
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_signal_engine_state "
            "ON signal_events(symbol, engine, state_key, emitted_at)"
        )


def _migration_5_pin_bands(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "signal_events", {
        "pin_band_low": "REAL",
        "pin_band_high": "REAL",
    })


def _migration_6_option_evidence(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "signal_outcomes", {
        "option_pnl": "REAL", "option_pnl_r": "REAL",
        "option_exclusion": "TEXT", "option_exit_at": "DATETIME",
    })


def _migration_7_term_book(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS gex_term_snapshots (
            id INTEGER PRIMARY KEY, snapshot_id INTEGER NOT NULL, symbol TEXT NOT NULL,
            captured_at DATETIME NOT NULL, expiration_date DATE, is_combined BOOLEAN NOT NULL DEFAULT 0,
            call_gex REAL, put_gex REAL, net_gex REAL, atm_straddle REAL, contract_count INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_term_snapshot ON gex_term_snapshots(snapshot_id, expiration_date);
        CREATE TABLE IF NOT EXISTS gex_term_contracts (
            id INTEGER PRIMARY KEY, term_snapshot_id INTEGER NOT NULL, osi_symbol TEXT NOT NULL,
            strike_price REAL NOT NULL, option_type TEXT NOT NULL, gex_value REAL NOT NULL,
            open_interest INTEGER, volume INTEGER, delta REAL, bid REAL, ask REAL, last_price REAL);
        CREATE INDEX IF NOT EXISTS idx_term_contract ON gex_term_contracts(term_snapshot_id, strike_price);
        CREATE TABLE IF NOT EXISTS oi_daily (
            id INTEGER PRIMARY KEY, session_date DATE NOT NULL, symbol TEXT NOT NULL,
            osi_symbol TEXT NOT NULL, strike_price REAL, option_type TEXT, open_interest INTEGER,
            volume INTEGER, captured_at DATETIME,
            CONSTRAINT uq_oi_daily_contract UNIQUE(session_date, osi_symbol));
        CREATE INDEX IF NOT EXISTS idx_oi_daily_symbol_date ON oi_daily(symbol, session_date);
    """)


def _migration_8_option_baselines(connection: sqlite3.Connection) -> None:
    _add_columns(connection, "signal_outcomes", {
        "random_baseline_pnl_r": "REAL", "momentum_baseline_pnl_r": "REAL",
    })


def _migration_9_estimated_flow(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS estimated_flow_polls (
            id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, session_date DATE NOT NULL,
            captured_at DATETIME NOT NULL, estimated_flow REAL, classified_volume INTEGER DEFAULT 0,
            observed_volume INTEGER DEFAULT 0, coverage REAL,
            label TEXT DEFAULT 'estimated flow, low confidence');
        CREATE INDEX IF NOT EXISTS idx_estimated_flow_session ON estimated_flow_polls(symbol, session_date, captured_at);
    """)


def _migration_10_index_bars(connection: sqlite3.Connection) -> None:
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS index_minute_bars (
            id INTEGER PRIMARY KEY, symbol TEXT NOT NULL, timestamp DATETIME NOT NULL,
            spot_price REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL, volume REAL,
            CONSTRAINT uq_index_minute_bar UNIQUE(symbol, timestamp));
        CREATE INDEX IF NOT EXISTS idx_index_bar_symbol_time ON index_minute_bars(symbol, timestamp);
    """)


MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migration_1_quotes),
    (2, _migration_2_signal_evidence),
    (3, _migration_3_decision_tables),
    (4, _migration_4_versioned_signal_evidence),
    (5, _migration_5_pin_bands),
    (6, _migration_6_option_evidence),
    (7, _migration_7_term_book),
    (8, _migration_8_option_baselines),
    (9, _migration_9_estimated_flow),
    (10, _migration_10_index_bars),
)


def backup_before_migration(db_path: Path) -> Path:
    """Create a recoverable copy without moving or opening the production file."""
    db_path = Path(db_path)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}_pre_migration_{timestamp}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)
    return backup_path


def run_migrations(
    db_path: Path,
    *,
    create_backup: bool = False,
    before_step: Callable[[int, sqlite3.Connection], None] | None = None,
) -> int:
    """Apply pending migrations and return the resulting schema version.

    ``before_step`` is a test seam used to prove interrupted migrations do not
    advance the version. It is intentionally called inside the transaction.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    existed_with_data = db_path.exists() and db_path.stat().st_size > 0

    with sqlite3.connect(db_path) as connection:
        current = int(connection.execute("PRAGMA user_version").fetchone()[0])

    if create_backup and existed_with_data and current < CURRENT_SCHEMA_VERSION:
        backup_before_migration(db_path)

    with sqlite3.connect(db_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for version, migration in MIGRATIONS:
            current = int(connection.execute("PRAGMA user_version").fetchone()[0])
            if version <= current:
                continue
            try:
                connection.execute("BEGIN IMMEDIATE")
                if before_step:
                    before_step(version, connection)
                migration(connection)
                connection.execute(f"PRAGMA user_version = {version}")
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return int(connection.execute("PRAGMA user_version").fetchone()[0])
