import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import Boolean, Column, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, create_engine, event
from sqlalchemy.orm import declarative_base, relationship, sessionmaker


DB_CONNECTION_STR = "sqlite:///gex_data.db"
DB_PATH = Path("gex_data.db")
DB_BUSY_TIMEOUT_SECONDS = float(os.getenv("GEX_DB_TIMEOUT_SECONDS", "30"))
DB_BUSY_TIMEOUT_MS = int(DB_BUSY_TIMEOUT_SECONDS * 1000)

Base = declarative_base()


class CollectionRun(Base):
    """One collector pass across configured symbols."""

    __tablename__ = "collection_runs"

    id = Column(Integer, primary_key=True)
    started_at = Column(DateTime, default=datetime.now, index=True)
    finished_at = Column(DateTime, nullable=True)
    status = Column(String, default="running", index=True)
    message = Column(Text, default="")
    symbols_requested = Column(Text, default="")
    symbols_succeeded = Column(Text, default="")
    symbols_failed = Column(Text, default="")
    symbols_skipped = Column(Text, default="")

    snapshots = relationship("GexSnapshot", back_populates="collection_run")


class GexSnapshot(Base):
    """High-level GEX summary for a symbol during a collection run."""

    __tablename__ = "gex_snapshots"

    id = Column(Integer, primary_key=True)
    collection_run_id = Column(Integer, ForeignKey("collection_runs.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    symbol = Column(String, index=True)
    spot_price = Column(Float)
    total_net_gex = Column(Float)
    total_call_gex = Column(Float)
    total_put_gex = Column(Float)
    max_call_gex_strike = Column(Float)
    max_put_gex_strike = Column(Float)
    flip_strike = Column(Float)
    regime = Column(String)
    effective_gex = Column(Float)
    total_gamma = Column(Float, default=0.0)
    total_theta = Column(Float, default=0.0)

    collection_run = relationship("CollectionRun", back_populates="snapshots")
    raw_options = relationship("RawOptionGreek", back_populates="snapshot", cascade="all, delete-orphan")

    __table_args__ = (Index("idx_snapshots_symbol_time", "symbol", "timestamp"),)


class RawOptionGreek(Base):
    """A single option contract's Greek data for one snapshot."""

    __tablename__ = "raw_option_greeks"

    id = Column(Integer, primary_key=True)
    snapshot_id = Column(Integer, ForeignKey("gex_snapshots.id"), nullable=False, index=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    symbol = Column(String, index=True)
    expiration_date = Column(Date)
    osi_symbol = Column(String, index=True)
    strike_price = Column(Float)
    option_type = Column(String)
    delta = Column(Float)
    gamma = Column(Float)
    open_interest = Column(Integer)
    underlying_price = Column(Float)
    gex_value = Column(Float)
    bid = Column(Float, nullable=True)
    ask = Column(Float, nullable=True)
    mid_price = Column(Float, nullable=True)
    last_price = Column(Float, nullable=True)
    bid_size = Column(Integer, nullable=True)
    ask_size = Column(Integer, nullable=True)
    volume = Column(Integer, nullable=True)
    implied_volatility = Column(Float, nullable=True)
    bid_timestamp = Column(DateTime, nullable=True)
    ask_timestamp = Column(DateTime, nullable=True)
    last_timestamp = Column(DateTime, nullable=True)

    snapshot = relationship("GexSnapshot", back_populates="raw_options")

    __table_args__ = (
        Index("idx_raw_symbol_time", "symbol", "timestamp"),
        Index("idx_raw_snapshot_strike", "snapshot_id", "strike_price"),
    )


class SignalEvent(Base):
    """One emitted dashboard/NinjaTrader signal payload for one symbol."""

    __tablename__ = "signal_events"

    id = Column(Integer, primary_key=True)
    emitted_at = Column(DateTime, default=datetime.now, index=True)
    symbol = Column(String, index=True)
    spot_price = Column(Float)
    regime = Column(String, index=True)
    bias = Column(String, index=True)
    bias_score = Column(Float)
    confidence = Column(Float)
    data_quality = Column(Float, nullable=True)
    edge_probability = Column(Float, nullable=True)
    target = Column(Float, nullable=True)
    invalidation = Column(Float, nullable=True)
    flip = Column(Float, nullable=True)
    market_state = Column(String, default="")
    dealer_state = Column(String, default="")
    liquidity_state = Column(String, default="")
    whale_state = Column(String, default="")
    state_key = Column(String, index=True, nullable=True)
    scenario_type = Column(String, index=True, nullable=True)
    scenario_id = Column(String, index=True, nullable=True)
    session_date = Column(Date, index=True, nullable=True)
    event_tags_json = Column(Text, default="[]")
    liquidity_grade = Column(String, index=True, nullable=True)
    is_opportunity = Column(Boolean, default=False, index=True)
    setup_key = Column(String, index=True)
    direction_key = Column(String, index=True)
    payload_json = Column(Text, default="")

    outcomes = relationship("SignalOutcome", back_populates="signal", cascade="all, delete-orphan")

    __table_args__ = (
        Index("idx_signal_symbol_time", "symbol", "emitted_at"),
        Index("idx_signal_setup_time", "setup_key", "emitted_at"),
        Index("idx_signal_direction_time", "direction_key", "emitted_at"),
    )


class SignalOutcome(Base):
    """Future outcome label for one emitted signal at one time horizon."""

    __tablename__ = "signal_outcomes"

    id = Column(Integer, primary_key=True)
    signal_event_id = Column(Integer, ForeignKey("signal_events.id"), nullable=False, index=True)
    horizon_minutes = Column(Integer, index=True)
    labeled_at = Column(DateTime, default=datetime.now, index=True)
    horizon_at = Column(DateTime, index=True)
    observed_at = Column(DateTime, index=True)
    end_spot = Column(Float)
    move_points = Column(Float)
    move_pct = Column(Float)
    directional_move_points = Column(Float, nullable=True)
    max_favorable_points = Column(Float, nullable=True)
    max_adverse_points = Column(Float, nullable=True)
    hit_target = Column(Boolean, default=False)
    hit_invalidation = Column(Boolean, default=False)
    target_first = Column(Boolean, default=False)
    invalidation_first = Column(Boolean, default=False)
    is_win = Column(Boolean, nullable=True)
    outcome_label = Column(String, index=True)

    signal = relationship("SignalEvent", back_populates="outcomes")

    __table_args__ = (
        UniqueConstraint("signal_event_id", "horizon_minutes", name="uq_signal_outcome_horizon"),
        Index("idx_outcome_horizon_label", "horizon_minutes", "outcome_label"),
    )


class MarketContextSnapshot(Base):
    __tablename__ = "market_context_snapshots"

    id = Column(Integer, primary_key=True)
    captured_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    session_date = Column(Date, index=True)
    event_state = Column(String, default="NORMAL", nullable=False)
    implied_move = Column(Float, nullable=True)
    range_consumed = Column(Float, nullable=True)
    realized_volatility_15m = Column(Float, nullable=True)
    cross_asset_state = Column(String, nullable=True)
    volatility_term_json = Column(Text, default="{}")
    warnings_json = Column(Text, default="[]")
    source_status_json = Column(Text, default="{}")


class DecisionAlert(Base):
    __tablename__ = "decision_alerts"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False, index=True)
    symbol = Column(String, nullable=False, index=True)
    alert_type = Column(String, nullable=False)
    severity = Column(String, default="info", nullable=False)
    scenario_id = Column(String, nullable=True)
    dedupe_key = Column(String, nullable=False, unique=True)
    state_from = Column(String, nullable=True)
    state_to = Column(String, nullable=True)
    message = Column(Text, nullable=False)
    payload_json = Column(Text, default="{}")


class TradeJournalEntry(Base):
    __tablename__ = "trade_journal_entries"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)
    updated_at = Column(DateTime, default=datetime.now, nullable=False)
    symbol = Column(String, nullable=False, index=True)
    session_date = Column(Date, nullable=False, index=True)
    scenario_id = Column(String, nullable=True)
    scenario_type = Column(String, nullable=True)
    regime = Column(String, nullable=True)
    event_tags_json = Column(Text, default="[]")
    liquidity_grade = Column(String, nullable=True)
    contracts = Column(Integer, nullable=False)
    entry_price = Column(Float, nullable=False)
    exit_price = Column(Float, nullable=True)
    entry_time = Column(DateTime, nullable=False)
    exit_time = Column(DateTime, nullable=True)
    fees = Column(Float, default=0, nullable=False)
    pnl = Column(Float, nullable=True)
    slippage = Column(Float, nullable=True)
    underlying_mfe = Column(Float, nullable=True)
    underlying_mae = Column(Float, nullable=True)
    exit_reason = Column(String, nullable=True)
    adhered_to_plan = Column(Boolean, nullable=True)
    notes = Column(Text, default="", nullable=False)


def _configure_sqlite_connection(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys = ON")
        try:
            cursor.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # A currently busy rollback-journal database should not prevent app startup.
            pass
        cursor.execute("PRAGMA synchronous = NORMAL")
    finally:
        cursor.close()


def _connection_string_for_path(db_path: Path) -> str:
    db_path = Path(db_path)
    if db_path == DB_PATH:
        return DB_CONNECTION_STR
    return f"sqlite:///{db_path.as_posix()}"


def get_engine(db_path: Path = DB_PATH):
    engine = create_engine(
        _connection_string_for_path(db_path),
        connect_args={"timeout": DB_BUSY_TIMEOUT_SECONDS, "check_same_thread": False},
    )
    event.listen(engine, "connect", _configure_sqlite_connection)
    return engine


def get_session_factory(engine=None):
    return sessionmaker(bind=engine or get_engine(), expire_on_commit=False)


def _sqlite_connect(db_path: Path):
    conn = sqlite3.connect(db_path, timeout=DB_BUSY_TIMEOUT_SECONDS)
    conn.execute(f"PRAGMA busy_timeout = {DB_BUSY_TIMEOUT_MS}")
    return conn


def _table_columns(db_path: Path, table: str) -> set[str]:
    if not db_path.exists():
        return set()
    with _sqlite_connect(db_path) as conn:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {row[1] for row in rows}


def schema_is_current(db_path: Path = DB_PATH) -> bool:
    if not db_path.exists():
        return True

    raw_columns = _table_columns(db_path, "raw_option_greeks")
    snapshot_columns = _table_columns(db_path, "gex_snapshots")
    run_columns = _table_columns(db_path, "collection_runs")

    if not raw_columns and not snapshot_columns and not run_columns:
        return True

    return (
        "snapshot_id" in raw_columns
        and "collection_run_id" in snapshot_columns
        and {"id", "started_at", "status"}.issubset(run_columns)
    )


def backup_database(db_path: Path = DB_PATH) -> Path | None:
    if not db_path.exists():
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = db_path.with_name(f"{db_path.stem}_legacy_{timestamp}{db_path.suffix}")
    shutil.move(str(db_path), str(backup_path))

    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            shutil.move(str(sidecar), str(backup_path) + suffix)

    return backup_path


def reset_database(db_path: Path = DB_PATH) -> Path | None:
    backup_path = backup_database(db_path)
    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    return backup_path


def initialize_database(
    reset_old_schema: bool = True,
    allow_legacy_on_lock: bool = False,
    db_path: Path = DB_PATH,
):
    if reset_old_schema and not schema_is_current(db_path):
        try:
            backup_database(db_path)
        except PermissionError:
            if allow_legacy_on_lock:
                return get_engine(db_path)
            raise

    engine = get_engine(db_path)
    Base.metadata.create_all(engine)
    from schema_migrations import run_migrations

    run_migrations(db_path, create_backup=Path(db_path).resolve() == DB_PATH.resolve())
    return engine


def compact_database(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        return

    with _sqlite_connect(db_path) as conn:
        conn.execute("VACUUM")


def optimize_database(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        return

    with _sqlite_connect(db_path) as conn:
        conn.execute("PRAGMA optimize")
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except sqlite3.OperationalError:
            pass
