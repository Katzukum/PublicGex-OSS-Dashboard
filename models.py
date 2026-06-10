import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import Column, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, create_engine
from sqlalchemy.orm import declarative_base, relationship, sessionmaker


DB_CONNECTION_STR = "sqlite:///gex_data.db"
DB_PATH = Path("gex_data.db")

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

    snapshot = relationship("GexSnapshot", back_populates="raw_options")

    __table_args__ = (
        Index("idx_raw_symbol_time", "symbol", "timestamp"),
        Index("idx_raw_snapshot_strike", "snapshot_id", "strike_price"),
    )


def get_engine():
    return create_engine(DB_CONNECTION_STR)


def get_session_factory(engine=None):
    return sessionmaker(bind=engine or get_engine())


def _table_columns(db_path: Path, table: str) -> set[str]:
    if not db_path.exists():
        return set()
    with sqlite3.connect(db_path) as conn:
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

    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            shutil.move(str(sidecar), str(backup_path) + suffix)

    return backup_path


def reset_database(db_path: Path = DB_PATH) -> Path | None:
    backup_path = backup_database(db_path)
    engine = get_engine()
    Base.metadata.create_all(engine)
    return backup_path


def initialize_database(reset_old_schema: bool = True, allow_legacy_on_lock: bool = False):
    if reset_old_schema and not schema_is_current(DB_PATH):
        try:
            backup_database(DB_PATH)
        except PermissionError:
            if allow_legacy_on_lock:
                return get_engine()
            raise

    engine = get_engine()
    Base.metadata.create_all(engine)
    return engine


def compact_database(db_path: Path = DB_PATH) -> None:
    if not db_path.exists():
        return

    with sqlite3.connect(db_path) as conn:
        conn.execute("VACUUM")
