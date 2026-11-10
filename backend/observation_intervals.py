"""Shared mechanics for causal option observation intervals.

The helpers in this module are deliberately database agnostic.  They accept
plain mappings (or SQLAlchemy rows), exclude duplicate contract identities,
and validate only *adjacent* snapshot pairs.  A missing or invalid observation
therefore breaks continuity instead of being bridged by a later snapshot.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import math
from typing import Any, Iterable, Mapping


MAX_OBSERVATION_GAP_SECONDS = 120


def row_mapping(row: Any) -> dict[str, Any]:
    """Return a mutable plain mapping for sqlite, SQLAlchemy, or dict rows."""
    if isinstance(row, dict):
        return dict(row)
    if hasattr(row, "_mapping"):
        return dict(row._mapping)
    return dict(row)


def finite_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def naive_observation_time(value: Any) -> datetime | None:
    """Parse the application's stored collection clock without guessing zones."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is None else None


def contract_identity(row: Mapping[str, Any]) -> tuple[str, str, str] | None:
    """Return the stable identity required for cross-snapshot comparison."""
    osi = str(row.get("osi_symbol") or "").strip()
    expiration = str(row.get("expiration_date") or "")[:10]
    symbol = str(row.get("symbol") or "").strip().upper()
    if not symbol or not expiration or not osi:
        return None
    return symbol, expiration, osi


def option_side(row: Mapping[str, Any], *, lowercase: bool = False) -> str | None:
    value = str(row.get("option_type") or "").upper()
    side = "CALL" if "CALL" in value else "PUT" if "PUT" in value else None
    return side.lower() if side and lowercase else side


def unique_contracts(
    rows: Iterable[Any],
) -> tuple[dict[tuple[str, str, str], dict[str, Any]], set[tuple[str, str, str]], int]:
    """Index rows by identity, excluding every copy of duplicate identities."""
    indexed: dict[tuple[str, str, str], dict[str, Any]] = {}
    duplicates: set[tuple[str, str, str]] = set()
    unidentified = 0
    for value in rows:
        row = row_mapping(value)
        identity = contract_identity(row)
        if identity is None:
            unidentified += 1
            continue
        if identity in indexed:
            duplicates.add(identity)
        indexed[identity] = row
    for identity in duplicates:
        indexed.pop(identity, None)
    return indexed, duplicates, unidentified


def cumulative_volume(value: Any, *, require_integral: bool = True) -> int | float | None:
    number = finite_number(value)
    if number is None or number < 0:
        return None
    if require_integral and not number.is_integer():
        return None
    return int(number) if number.is_integer() else number


def validate_volume_interval(
    left_row: Mapping[str, Any] | None,
    right_row: Mapping[str, Any] | None,
    *,
    require_integral: bool = True,
) -> tuple[int | float | None, str | None]:
    """Validate cumulative volume at both endpoints and return its delta."""
    if left_row is None:
        return None, "missing_left_contract"
    if right_row is None:
        return None, "missing_right_contract"
    left = cumulative_volume(left_row.get("volume"), require_integral=require_integral)
    right = cumulative_volume(right_row.get("volume"), require_integral=require_integral)
    if left is None or right is None:
        return None, "invalid_volume"
    if right < left:
        return None, "volume_reset"
    return right - left, None


def _snapshot_header(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = (
        snapshot.get("_time")
        if "_time" in snapshot
        else naive_observation_time(snapshot.get("timestamp"))
    )
    try:
        snapshot_id = int(snapshot.get("snapshot_id", snapshot.get("id")))
    except (TypeError, ValueError, OverflowError):
        snapshot_id = 0
    return {
        "snapshot_id": snapshot_id,
        "timestamp": snapshot.get("timestamp"),
        "available_at": snapshot.get("available_at"),
        "_time": timestamp,
        "symbol": str(snapshot.get("symbol") or "").upper(),
        "spot": snapshot.get("spot", snapshot.get("spot_price")),
    }


def build_observation_intervals(
    snapshots: Iterable[Mapping[str, Any]],
    *,
    max_gap_seconds: int = MAX_OBSERVATION_GAP_SECONDS,
) -> dict[str, Any]:
    """Build strict adjacent intervals from snapshot headers plus contracts.

    Each input snapshot may contain contracts under ``contracts`` or ``rows``.
    Empty snapshots must still be supplied so they break adjacency correctly.
    The result includes invalid identity intervals and reason counts, allowing
    consumers to distinguish unknown volume from measured zero activity.
    """
    prepared = []
    duplicate_rows = 0
    unidentified_rows = 0
    for value in snapshots:
        snapshot = dict(value)
        header = _snapshot_header(snapshot)
        contracts, duplicates, unidentified = unique_contracts(
            snapshot.get("contracts", snapshot.get("rows", [])) or []
        )
        duplicate_rows += len(duplicates)
        unidentified_rows += unidentified
        prepared.append((header, contracts, duplicates))

    # Preserve the caller's snapshot order. Database readers order by
    # (timestamp, id); retaining that order ensures an invalid clock remains a
    # continuity break at its actual poll instead of being sorted away.
    intervals: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    max_gap: float | None = None

    for (left, left_rows, left_duplicates), (right, right_rows, right_duplicates) in zip(
        prepared, prepared[1:]
    ):
        left_time, right_time = left["_time"], right["_time"]
        gap = (right_time - left_time).total_seconds() if left_time and right_time else None
        if gap is not None:
            max_gap = gap if max_gap is None else max(max_gap, gap)

        pair_reason = None
        if left_time is None or right_time is None:
            pair_reason = "invalid_snapshot_time"
        elif left["symbol"] != right["symbol"] or left_time.date() != right_time.date():
            pair_reason = "session_change"
        elif gap is None or gap <= 0:
            pair_reason = "non_increasing_time"
        elif gap > max_gap_seconds:
            pair_reason = "gap_exceeded"

        identities = sorted(
            set(left_rows) | set(right_rows) | left_duplicates | right_duplicates
        )
        if not identities:
            reason = pair_reason or "empty_snapshot"
            reasons[reason] += 1
            intervals.append(
                {
                    "left": left,
                    "right": right,
                    "identity": None,
                    "left_row": None,
                    "right_row": None,
                    "gap_seconds": gap,
                    "volume_delta": None,
                    "status": "discarded",
                    "reason": reason,
                }
            )
            continue

        for identity in identities:
            reason = pair_reason
            if reason is None and (identity in left_duplicates or identity in right_duplicates):
                reason = "duplicate_contract"
            volume_delta = None
            if reason is None:
                volume_delta, reason = validate_volume_interval(
                    left_rows.get(identity), right_rows.get(identity), require_integral=True
                )
            if reason:
                reasons[reason] += 1
            intervals.append(
                {
                    "left": left,
                    "right": right,
                    "identity": identity,
                    "left_row": left_rows.get(identity),
                    "right_row": right_rows.get(identity),
                    "gap_seconds": gap,
                    "volume_delta": volume_delta,
                    "status": "valid" if reason is None else "discarded",
                    "reason": reason,
                }
            )

    for interval in intervals:
        interval["left"].pop("_time", None)
        interval["right"].pop("_time", None)

    return {
        "intervals": intervals,
        "coverage": {
            "snapshots": len(prepared),
            "snapshot_pairs": max(0, len(prepared) - 1),
            "valid_intervals": sum(item["status"] == "valid" for item in intervals),
            "discarded_intervals": sum(item["status"] != "valid" for item in intervals),
            "discarded_by_reason": dict(sorted(reasons.items())),
            "duplicate_identities": duplicate_rows,
            "unidentified_rows": unidentified_rows,
            "max_gap_seconds": max_gap,
        },
    }


def group_rows_by_snapshot(rows: Iterable[Any]) -> dict[int, list[dict[str, Any]]]:
    grouped: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for value in rows:
        row = row_mapping(value)
        try:
            grouped[int(row.get("snapshot_id"))].append(row)
        except (TypeError, ValueError, OverflowError):
            continue
    return dict(grouped)
