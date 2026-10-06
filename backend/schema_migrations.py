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


CURRENT_SCHEMA_VERSION = 3


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


MIGRATIONS: tuple[tuple[int, Callable[[sqlite3.Connection], None]], ...] = (
    (1, _migration_1_quotes),
    (2, _migration_2_signal_evidence),
    (3, _migration_3_decision_tables),
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
