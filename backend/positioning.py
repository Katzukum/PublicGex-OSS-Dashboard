"""Observed OI concentration and unsigned activity; never a dealer inventory model.

Activity uses complete, consecutive cumulative-volume intervals, at most 120s
apart, with both endpoints inside the requested window. Each interval is valued
with the selected snapshot's gamma and spot. A missing/reset counter or missing
snapshot breaks the sequence; no volume is imputed across that break.
"""

from collections import defaultdict
from datetime import datetime, timedelta
import math
import sqlite3

from sqlalchemy import text


WINDOWS_MINUTES = (5, 15)
MAX_GAP_SECONDS = 120
METHOD_WARNING = (
    "Activity is unsigned traded-contract turnover, valued with current snapshot gamma and spot; "
    "it is not new open interest or dealer positioning. Only complete intervals of at most 120 seconds are counted."
)
PERSISTENCE_WARNING = (
    "Persistence is the frequency in the top five gross OI strikes across adequately observed snapshots "
    "in the last 15 minutes. Top levels rank by persistence, then current gross OI exposure."
)


def _number(value):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _time(value):
    try:
        result = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        # Database collection timestamps use the same local, naive clock.
        return result if result.tzinfo is None else None
    except (TypeError, ValueError):
        return None


def _mapping(row):
    return dict(row._mapping) if hasattr(row, "_mapping") else dict(row)


def unavailable_positioning(as_of="", warning="Positioning observations are unavailable."):
    return {
        "model": "oi_activity_v1", "as_of": str(as_of), "dealer_direction": "unknown",
        "status": "unavailable", "warnings": [METHOD_WARNING, PERSISTENCE_WARNING, warning],
        "windows_minutes": list(WINDOWS_MINUTES),
        "coverage": {"observed_minutes": 0.0, "max_gap_seconds": None,
                     "contracts": 0, "valid_activity_contracts": 0},
        "strikes": [], "top_levels": [],
    }


def _identity(row):
    osi = str(row.get("osi_symbol") or "").strip()
    expiry = str(row.get("expiration_date") or "")[:10]
    return (str(row.get("symbol") or "").upper(), expiry, osi) if osi and expiry else None


def _side(row):
    value = str(row.get("option_type") or "").upper()
    return "call" if "CALL" in value else "put" if "PUT" in value else None


def _exposure(row, spot):
    gamma, oi = _number(row.get("gamma")), _number(row.get("open_interest"))
    if gamma is None or oi is None or gamma < 0 or oi < 0 or spot <= 0:
        return None
    return gamma * oi * spot * spot  # 100-share multiplier times a 1% move.


def build_positioning(rows, *, snapshot_id, symbol, as_of, spot):
    """Pure calculation from bounded rows; filters identities, date and future data again.

    observed_minutes sums full snapshot intervals no longer than MAX_GAP_SECONDS
    within 15 minutes. valid_activity_contracts counts current contracts with at
    least one valid 15-minute interval. Strike-side activity is null if any
    current contract on that side has no valid interval; totals require both
    sides. Gaps/reset intervals are excluded, and status remains partial.
    """
    end, spot = _time(as_of), _number(spot)
    if end is None or spot is None or spot <= 0:
        return unavailable_positioning(as_of, "A valid snapshot time and spot are required.")
    symbol = str(symbol).upper()
    start = end - timedelta(minutes=max(WINDOWS_MINUTES))
    eligible = []
    for value in rows:
        row = _mapping(value)
        timestamp = _time(row.get("timestamp"))
        if (timestamp is None or timestamp.date() != end.date() or timestamp > end
                or str(row.get("symbol") or "").upper() != symbol):
            continue
        if timestamp == end and int(row.get("snapshot_id") or 0) > int(snapshot_id):
            continue
        row["_time"] = timestamp
        eligible.append(row)

    warnings = [METHOD_WARNING, PERSISTENCE_WARNING]
    current = {}
    duplicate_keys = set()
    for row in eligible:
        if int(row.get("snapshot_id") or 0) != int(snapshot_id):
            continue
        key, strike = _identity(row), _number(row.get("strike_price"))
        if key is None or _side(row) is None or strike is None or strike <= 0:
            continue
        if key in current:
            duplicate_keys.add(key)
        current[key] = row
    for key in duplicate_keys:
        current.pop(key, None)
    if not current:
        return unavailable_positioning(as_of, "No uniquely identified current contracts are available.")
    partial = bool(duplicate_keys)
    if duplicate_keys:
        warnings.append("Duplicate current contract rows were excluded.")

    observations = defaultdict(dict)
    snapshot_times = {}
    duplicates = set()
    for row in eligible:
        sid = int(row.get("snapshot_id") or 0)
        snapshot_times[sid] = row["_time"]
        key = _identity(row)
        if key not in current:
            continue
        if key in observations[sid]:
            duplicates.add((sid, key))
        observations[sid][key] = row
    for sid, key in duplicates:
        observations[sid].pop(key, None)
    ordered = sorted(
        ((timestamp, sid, observations[sid]) for sid, timestamp in snapshot_times.items()),
        key=lambda item: (item[0], item[1]),
    )
    before_window = [item for item in ordered if item[0] < start]
    ordered = before_window[-1:] + [item for item in ordered if item[0] >= start]
    window_snapshots = [(timestamp, sid, items) for timestamp, sid, items in ordered if timestamp >= start]
    gaps = [(right[0] - left[0]).total_seconds() for left, right in zip(window_snapshots, window_snapshots[1:])]
    observed_seconds = sum(gap for gap in gaps if 0 < gap <= MAX_GAP_SECONDS)
    if any(gap > MAX_GAP_SECONDS for gap in gaps):
        partial = True
        warnings.append("Collection gaps over 120 seconds were excluded from activity.")
    if not ordered or ordered[0][0] > start:
        partial = True
        warnings.append("Less than 15 minutes of history is available; activity covers observed intervals only.")

    activity = {key: {window: None for window in WINDOWS_MINUTES} for key in current}
    invalid_intervals = False
    for left, right in zip(ordered, ordered[1:]):
        gap = (right[0] - left[0]).total_seconds()
        if gap <= 0 or gap > MAX_GAP_SECONDS:
            continue
        for key in current:
            previous, latest = left[2].get(key), right[2].get(key)
            if previous is None or latest is None:
                invalid_intervals = True
                continue
            old_volume, new_volume = _number(previous.get("volume")), _number(latest.get("volume"))
            gamma = _number(current[key].get("gamma"))
            if (old_volume is None or new_volume is None or min(old_volume, new_volume) < 0
                    or new_volume < old_volume or gamma is None or gamma < 0):
                invalid_intervals = True
                continue
            amount = (new_volume - old_volume) * gamma * spot * spot
            for window in WINDOWS_MINUTES:
                if left[0] >= end - timedelta(minutes=window):
                    activity[key][window] = (activity[key][window] or 0.0) + amount
    if invalid_intervals or duplicates:
        partial = True
        warnings.append("Missing, duplicate or reset contract observations were excluded; activity can be incomplete.")

    levels = {}
    by_strike_side = defaultdict(list)
    for key, row in current.items():
        strike, side = float(row["strike_price"]), _side(row)
        level = levels.setdefault(strike, {
            "strike": strike, "call_oi_gex": 0.0, "put_oi_gex": 0.0,
            "gross_oi_gex": 0.0, "net_oi_proxy": 0.0, "oi_persistence": None,
        })
        exposure = _exposure(row, spot)
        if exposure is None:
            partial = True
        else:
            level[f"{side}_oi_gex"] += exposure
        by_strike_side[(strike, side)].append(key)
    if any(_exposure(row, spot) is None for row in current.values()):
        warnings.append("Some current OI or gamma values are missing; concentration excludes those contracts.")

    persistent_counts = defaultdict(int)
    adequate_snapshots = 0
    for _, _, items in window_snapshots:
        # Require the same current contract universe, valid OI/gamma and spot.
        # This avoids promoting levels merely because a competing row vanished.
        if len(items) != len(current):
            continue
        historical_exposures = {}
        adequate = True
        for row in items.values():
            historical_spot = _number(row.get("underlying_price"))
            exposure = _exposure(row, historical_spot or 0)
            if exposure is None:
                adequate = False
                break
            strike = float(row["strike_price"])
            historical_exposures[strike] = historical_exposures.get(strike, 0) + exposure
        if not adequate or not any(value > 0 for value in historical_exposures.values()):
            continue
        adequate_snapshots += 1
        top = sorted(historical_exposures, key=lambda strike: (-historical_exposures[strike], strike))
        for strike in [strike for strike in top if historical_exposures[strike] > 0][:5]:
            persistent_counts[strike] += 1

    for strike, level in levels.items():
        level["gross_oi_gex"] = level["call_oi_gex"] + level["put_oi_gex"]
        level["net_oi_proxy"] = level["call_oi_gex"] - level["put_oi_gex"]
        if adequate_snapshots > 1:
            level["oi_persistence"] = persistent_counts[strike] / adequate_snapshots
        for window in WINDOWS_MINUTES:
            for side in ("call", "put"):
                values = [activity[key][window] for key in by_strike_side[(strike, side)]]
                level[f"{side}_activity_{window}m"] = sum(values) if values and all(v is not None for v in values) else None
            call, put = level[f"call_activity_{window}m"], level[f"put_activity_{window}m"]
            level[f"activity_{window}m"] = call + put if call is not None and put is not None else None
    valid_contracts = sum(activity[key][15] is not None for key in current)
    if valid_contracts < len(current):
        partial = True
        warnings.append("Unobserved activity remains unavailable rather than zero.")
    if adequate_snapshots < 2:
        partial = True
        warnings.append("Persistence needs at least two adequately observed snapshots.")
    ranked = sorted(
        (level for level in levels.values() if level["gross_oi_gex"] > 0),
        key=lambda level: (-(level["oi_persistence"] if level["oi_persistence"] is not None else -1),
                           -level["gross_oi_gex"], level["strike"]),
    )
    return {
        "model": "oi_activity_v1", "as_of": str(as_of), "dealer_direction": "unknown",
        "status": "partial" if partial else "ready", "warnings": warnings,
        "windows_minutes": list(WINDOWS_MINUTES),
        "coverage": {"observed_minutes": round(observed_seconds / 60, 3),
                     "max_gap_seconds": max(gaps) if gaps else None,
                     "contracts": len(current), "valid_activity_contracts": valid_contracts},
        "strikes": [levels[strike] for strike in sorted(levels)],
        "top_levels": [level["strike"] for level in ranked[:5]],
    }


def _fetch(conn, sql, params):
    if isinstance(conn, sqlite3.Connection):
        cursor = conn.execute(sql, params)
        names = [column[0] for column in cursor.description]
        return [dict(zip(names, row)) for row in cursor.fetchall()]
    return [_mapping(row) for row in conn.execute(text(sql), params)]


def load_positioning(conn, snapshot_id):
    """Read a selected snapshot and one bounded history batch, without writes.

    Accepts SQLAlchemy Connection or sqlite3.Connection. The caller owns its
    connection and read-only policy. No ORM initialization or collector imports.
    """
    snapshots = _fetch(conn, "SELECT id, symbol, timestamp, spot_price FROM gex_snapshots WHERE id = :id", {"id": int(snapshot_id)})
    if not snapshots:
        return unavailable_positioning(warning="Selected snapshot was not found.")
    snapshot = snapshots[0]
    end = _time(snapshot["timestamp"])
    if end is None:
        return unavailable_positioning(snapshot["timestamp"], "Selected snapshot time is invalid.")
    params = {
        "id": int(snapshot_id), "symbol": snapshot["symbol"],
        "end": str(snapshot["timestamp"]), "start": str(end - timedelta(minutes=15)),
        "session_start": str(datetime.combine(end.date(), datetime.min.time())),
    }
    rows = _fetch(conn, """
        WITH baseline AS (
            SELECT id FROM gex_snapshots
            WHERE symbol = :symbol AND timestamp >= :session_start AND timestamp < :start
            ORDER BY timestamp DESC, id DESC LIMIT 1
        ), selected AS (
            SELECT id FROM gex_snapshots
            WHERE symbol = :symbol AND timestamp >= :start AND timestamp >= :session_start
              AND (timestamp < :end OR (timestamp = :end AND id <= :id))
            UNION SELECT id FROM baseline
        )
        SELECT gs.id AS snapshot_id, gs.timestamp, gs.symbol, r.expiration_date, r.osi_symbol,
               r.strike_price, r.option_type, r.gamma, r.open_interest, r.volume, r.underlying_price
        FROM selected s
        JOIN gex_snapshots gs ON gs.id = s.id
        LEFT JOIN raw_option_greeks r ON r.snapshot_id = s.id AND r.symbol = :symbol
          AND r.expiration_date IN (SELECT expiration_date FROM raw_option_greeks WHERE snapshot_id = :id)
        ORDER BY gs.timestamp, gs.id, r.osi_symbol
    """, params)
    return build_positioning(rows, snapshot_id=snapshot["id"], symbol=snapshot["symbol"],
                             as_of=snapshot["timestamp"], spot=snapshot["spot_price"])
