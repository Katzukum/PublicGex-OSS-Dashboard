"""Read bounded, causal option observations without application side effects."""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import text

from observation_intervals import (
    build_observation_intervals,
    finite_number,
    group_rows_by_snapshot,
    naive_observation_time,
    row_mapping,
)


MODEL = "research_observations_v1"
DEFAULT_OBSERVATION_TIMEZONE = "America/New_York"
QUOTE_CLOCK_POLICIES = {"naive_utc", "unknown"}


def _fetch(conn, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(conn, sqlite3.Connection):
        cursor = conn.execute(sql, params)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    return [row_mapping(row) for row in conn.execute(text(sql), params)]


def _table_columns(conn, table: str) -> set[str]:
    query = f"PRAGMA table_info({table})"
    if isinstance(conn, sqlite3.Connection):
        return {str(row[1]) for row in conn.execute(query).fetchall()}
    return {str(row_mapping(row).get("name")) for row in conn.execute(text(query))}


def _iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def localize_observation_time(
    value: Any, observation_timezone: str = DEFAULT_OBSERVATION_TIMEZONE
) -> tuple[datetime | None, str | None]:
    """Strictly localize a collection clock, rejecting DST gaps and folds."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None, "invalid_observation_time"
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc), None
    try:
        zone = ZoneInfo(observation_timezone)
    except ZoneInfoNotFoundError:
        return None, "invalid_observation_timezone"

    candidates: list[datetime] = []
    offsets = set()
    for fold in (0, 1):
        candidate = parsed.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(timezone.utc).astimezone(zone)
        if round_trip.replace(tzinfo=None) == parsed and round_trip.fold == fold:
            candidates.append(candidate)
            offsets.add(candidate.utcoffset())
    if not candidates:
        return None, "nonexistent_observation_time"
    if len(offsets) > 1:
        return None, "ambiguous_observation_time"
    return candidates[0].astimezone(timezone.utc), None


def normalize_quote_time(
    value: Any, *, naive_policy: str = "naive_utc"
) -> tuple[datetime | None, str | None]:
    """Normalize provider quote clocks under an explicit provenance policy."""
    if value is None or str(value).strip() == "":
        return None, "missing_quote_time"
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None, "invalid_quote_time"
    if parsed.tzinfo is None:
        if naive_policy == "unknown":
            return None, "unknown_naive_quote_clock"
        if naive_policy != "naive_utc":
            return None, "invalid_quote_clock_policy"
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc), None


def unavailable_research_observations(
    *, symbol: str = "", as_of: Any = "", observation_timezone: str = DEFAULT_OBSERVATION_TIMEZONE,
    warning: str = "Research observations are unavailable."
) -> dict[str, Any]:
    return {
        "model": MODEL,
        "status": "unavailable",
        "symbol": str(symbol or "").upper(),
        "session": None,
        "as_of": str(as_of or ""),
        "available_at": None,
        "observation_timezone": observation_timezone,
        "snapshots": [],
        "rows": [],
        "spot_observations": [],
        "intervals": [],
        "coverage": {
            "snapshots": 0,
            "contracts": 0,
            "spot_samples": 0,
            "valid_intervals": 0,
            "discarded_intervals": 0,
            "discarded_by_reason": {},
            "clock_exclusions": {},
            "max_gap_seconds": None,
        },
        "warnings": [warning],
    }


def load_session_observations(
    conn,
    *,
    symbol: str,
    as_of: Any,
    snapshot_id: int,
    observation_timezone: str = DEFAULT_OBSERVATION_TIMEZONE,
    quote_clock_policy: str = "naive_utc",
) -> dict[str, Any]:
    """Load every symbol snapshot in one calendar session through a cutoff.

    Exactly two data queries are used: one for snapshot headers and one for all
    option rows.  The selected timestamp and snapshot id form a lexicographic
    upper bound, which prevents same-time future rows from leaking into replay.
    The caller owns the connection and any read-only policy.
    """
    symbol = str(symbol or "").strip().upper()
    end = naive_observation_time(as_of)
    try:
        upper_id = int(snapshot_id)
    except (TypeError, ValueError, OverflowError):
        upper_id = 0
    if not symbol or end is None or upper_id <= 0:
        return unavailable_research_observations(
            symbol=symbol, as_of=as_of, observation_timezone=observation_timezone,
            warning="A symbol, naive collection timestamp, and positive snapshot id are required.",
        )
    if quote_clock_policy not in QUOTE_CLOCK_POLICIES:
        return unavailable_research_observations(
            symbol=symbol, as_of=as_of, observation_timezone=observation_timezone,
            warning="The quote clock policy is invalid.",
        )
    try:
        ZoneInfo(observation_timezone)
    except ZoneInfoNotFoundError:
        return unavailable_research_observations(
            symbol=symbol, as_of=as_of, observation_timezone=observation_timezone,
            warning="The observation timezone is invalid.",
        )

    session_start = datetime.combine(end.date(), time.min)
    session_end = session_start + timedelta(days=1)
    params = {
        "symbol": symbol,
        "start": str(session_start),
        "session_end": str(session_end),
        "end": str(as_of),
        "snapshot_id": upper_id,
    }
    headers = _fetch(
        conn,
        """
        SELECT id AS snapshot_id, timestamp, symbol, spot_price
        FROM gex_snapshots
        WHERE upper(symbol) = :symbol
          AND timestamp >= :start AND timestamp < :session_end
          AND (
            julianday(timestamp) < julianday(:end)
            OR (julianday(timestamp) = julianday(:end) AND id <= :snapshot_id)
          )
        ORDER BY timestamp, id
        """,
        params,
    )
    if not headers:
        return unavailable_research_observations(
            symbol=symbol, as_of=as_of, observation_timezone=observation_timezone,
            warning="No snapshots exist at or before the requested session cutoff.",
        )

    columns = _table_columns(conn, "raw_option_greeks")
    desired = (
        "expiration_date", "osi_symbol", "strike_price", "option_type", "delta", "gamma",
        "open_interest", "underlying_price", "gex_value", "bid", "ask", "mid_price",
        "last_price", "bid_size", "ask_size", "volume", "implied_volatility",
        "bid_timestamp", "ask_timestamp", "last_timestamp",
    )
    projections = [
        f"r.{name} AS {name}" if name in columns else f"NULL AS {name}" for name in desired
    ]
    raw_rows = _fetch(
        conn,
        f"""
        SELECT r.snapshot_id, r.timestamp AS observed_timestamp, r.symbol,
               {', '.join(projections)}
        FROM raw_option_greeks r
        JOIN gex_snapshots gs ON gs.id = r.snapshot_id
        WHERE upper(gs.symbol) = :symbol
          AND gs.timestamp >= :start AND gs.timestamp < :session_end
          AND (
            julianday(gs.timestamp) < julianday(:end)
            OR (julianday(gs.timestamp) = julianday(:end) AND gs.id <= :snapshot_id)
          )
        ORDER BY gs.timestamp, gs.id, r.osi_symbol
        """,
        params,
    )
    grouped = group_rows_by_snapshot(raw_rows)
    clock_exclusions: dict[str, int] = {}
    normalized_rows: list[dict[str, Any]] = []
    normalized_snapshots: list[dict[str, Any]] = []
    spot_observations: list[dict[str, Any]] = []

    def record_clock(reason: str | None) -> None:
        if reason:
            clock_exclusions[reason] = clock_exclusions.get(reason, 0) + 1

    for header_value in headers:
        header = row_mapping(header_value)
        sid = int(header["snapshot_id"])
        observed_utc, observed_reason = localize_observation_time(
            header.get("timestamp"), observation_timezone
        )
        record_clock(observed_reason)
        display_time = str(header.get("timestamp"))
        available_at = _iso_utc(observed_utc) if observed_utc else None
        contracts = []
        for raw in grouped.get(sid, []):
            row = dict(raw)
            for field in (
                "observed_timestamp", "expiration_date", "bid_timestamp",
                "ask_timestamp", "last_timestamp",
            ):
                value = row.get(field)
                if value is not None and hasattr(value, "isoformat"):
                    row[field] = value.isoformat(sep=" ") if isinstance(value, datetime) else value.isoformat()
            row["symbol"] = str(row.get("symbol") or symbol).upper()
            row["timestamp"] = display_time
            row["available_at"] = available_at
            for field in ("bid_timestamp", "ask_timestamp", "last_timestamp"):
                normalized, reason = normalize_quote_time(
                    row.get(field), naive_policy=quote_clock_policy
                )
                row[f"{field}_utc"] = _iso_utc(normalized) if normalized else None
                if row.get(field) is not None:
                    record_clock(reason)
            contracts.append(row)
            normalized_rows.append(row)
        spot = finite_number(header.get("spot_price"))
        snapshot = {
            "snapshot_id": sid,
            "timestamp": display_time,
            "available_at": available_at,
            "_time": naive_observation_time(header.get("timestamp")) if observed_utc else None,
            "symbol": symbol,
            "spot": spot,
            "contracts": contracts,
        }
        normalized_snapshots.append(snapshot)
        if spot is not None and spot > 0 and available_at is not None:
            spot_observations.append(
                {
                    "snapshot_id": sid,
                    "timestamp": display_time,
                    "available_at": available_at,
                    "price": spot,
                }
            )

    interval_result = build_observation_intervals(normalized_snapshots)
    public_snapshots = []
    for snapshot in normalized_snapshots:
        public_snapshot = dict(snapshot)
        public_snapshot.pop("_time", None)
        public_snapshots.append(public_snapshot)
    interval_coverage = interval_result["coverage"]
    coverage = {
        **interval_coverage,
        "contracts": len(normalized_rows),
        "spot_samples": len(spot_observations),
        "clock_exclusions": dict(sorted(clock_exclusions.items())),
    }
    warnings = []
    if clock_exclusions:
        warnings.append(
            "Some collection or quote clocks could not be normalized; affected research intervals remain unavailable."
        )
    if interval_coverage["discarded_intervals"]:
        warnings.append(
            "Missing, duplicate, reset, invalid, or widely spaced observations break interval continuity."
        )
    if quote_clock_policy == "naive_utc":
        warnings.append(
            "Naive provider quote timestamps are interpreted as UTC under the collector serialization convention."
        )
    latest_available = next(
        (item["available_at"] for item in reversed(normalized_snapshots) if item["available_at"]), None
    )
    status = "partial" if clock_exclusions or interval_coverage["discarded_intervals"] else "ready"
    return {
        "model": MODEL,
        "status": status,
        "symbol": symbol,
        "session": end.date().isoformat(),
        "as_of": str(as_of),
        "available_at": latest_available,
        "observation_timezone": observation_timezone,
        "snapshots": public_snapshots,
        "rows": normalized_rows,
        "spot_observations": spot_observations,
        "intervals": interval_result["intervals"],
        "coverage": coverage,
        "warnings": warnings,
    }


# A concise compatibility alias for callers that treat this as a reader object.
load_research_observations = load_session_observations
