"""Read-only, causal five-minute research data; never imports the live app.

Stored naive timestamps are assumed to be America/New_York wall time. This is
an explicit historical-data assumption, not recovered exchange quote time.
The wall anchor is a total-side-GEX-weighted average of two contract walls;
it is NOT the gamma-weighted center of the entire option chain.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta
import hashlib
from pathlib import Path
import sqlite3
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

import numpy as np


CALENDAR_SOURCES = [
    "https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx",
    "https://ir.theice.com/press/news-details/2024/The-New-York-Stock-Exchange-Will-Close-Markets-on-January-9-to-Honor-the-Passing-of-Former-President-Jimmy-Carter-on-National-Day-of-Mourning/default.aspx",
    "https://www.nyse.com/trade/hours-calendars",
]
HOLIDAYS = frozenset("""
2025-01-01 2025-01-09 2025-01-20 2025-02-17 2025-04-18 2025-05-26
2025-06-19 2025-07-04 2025-09-01 2025-11-27 2025-12-25
2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19
2026-07-03 2026-09-07 2026-11-26 2026-12-25
""".split())
EARLY_CLOSES = frozenset("2025-07-03 2025-11-28 2025-12-24 2026-11-27 2026-12-24".split())
CORE_COLUMNS = (
    "spot_price", "total_net_gex", "total_call_gex", "total_put_gex",
    "max_call_gex_strike", "max_put_gex_strike",
)


def session_bounds(day: date | str) -> tuple[datetime, datetime] | None:
    """Verified cash-session calendar, deliberately restricted to 2025–2026."""
    day = date.fromisoformat(day) if isinstance(day, str) else day
    if day.year not in (2025, 2026):
        raise ValueError(f"Calendar is only verified for 2025–2026: {day}")
    if day.weekday() >= 5 or day.isoformat() in HOLIDAYS:
        return None
    close = time(13) if day.isoformat() in EARLY_CLOSES else time(16)
    return datetime.combine(day, time(9, 30)), datetime.combine(day, close)


def _timestamp(value) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo("America/New_York")).replace(tzinfo=None)
    return parsed


def _signature(row: dict) -> tuple:
    return tuple(row.get(key) for key in CORE_COLUMNS)


def _valid(row: dict) -> bool:
    try:
        spot, net, call, put, call_wall, put_wall = (float(row[k]) for k in CORE_COLUMNS)
    except (TypeError, ValueError, KeyError):
        return False
    gross = abs(call) + abs(put)
    return bool(
        np.isfinite([spot, net, call, put, call_wall, put_wall]).all()
        and min(spot, call_wall, put_wall, gross) > 0
        and abs(net / gross) <= 1 + 1e-8
        and np.isclose(net, call + put, rtol=1e-7, atol=1e-6)
    )


def _age_summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "median_seconds": None, "p95_seconds": None, "max_seconds": None}
    return {
        "count": len(values), "median_seconds": float(np.median(values)),
        "p95_seconds": float(np.percentile(values, 95)), "max_seconds": float(max(values)),
    }


def build_blocks(
    rows: Iterable[dict], *, source: str, legacy: bool = False,
    grid_minutes: int = 5, min_block_points: int = 10,
    price_anchor_window: int = 6,
) -> tuple[list[dict], dict]:
    """Sample prices as of each grid point and features from the preceding poll.

    Price age limits are 90s modern / 210s legacy. Lagged feature limits are
    150s / 420s, allowing one whole collection cycle. The immediately previous
    snapshot must strictly predate the observed price snapshot. Prices are
    collection-start spot proxies, not independently timestamped market ticks.
    Missing grid points break blocks; rolling price anchors restart per block.
    """
    if grid_minutes <= 0 or min_block_points < 1 or price_anchor_window < 1:
        raise ValueError("Grid, minimum block length, and rolling window must be positive")
    price_limit, feature_limit = (210.0, 420.0) if legacy else (90.0, 150.0)
    counts = Counter()
    grouped = defaultdict(list)
    seen = {}
    for incoming in rows:
        counts["input_rows"] += 1
        row = dict(incoming)
        stamp = _timestamp(row["timestamp"])
        row["timestamp"] = stamp
        key = row["symbol"], stamp
        signature = _signature(row)
        if key in seen:
            if seen[key] != signature:
                raise ValueError(f"Conflicting snapshots at {key}")
            counts["duplicate_rows"] += 1
            continue
        seen[key] = signature
        bounds = session_bounds(stamp.date())
        if bounds is None or not bounds[0] <= stamp <= bounds[1]:
            counts["outside_cash_session_rows"] += 1
            continue
        counts["regular_session_rows"] += 1
        if not _valid(row):
            counts["invalid_core_rows"] += 1
        grouped[(row["symbol"], stamp.date().isoformat())].append(row)

    blocks, price_ages, feature_ages = [], [], []
    sessions = []
    step = timedelta(minutes=grid_minutes)
    for (symbol, session), session_rows in sorted(grouped.items()):
        session_rows.sort(key=lambda r: r["timestamp"])
        times = np.array([r["timestamp"] for r in session_rows], dtype="datetime64[us]")
        opening, closing = session_bounds(session)
        points, fragments, grid = [], [], opening
        while grid <= closing:
            counts["candidate_grid_points"] += 1
            idx = int(np.searchsorted(times, np.datetime64(grid), side="right")) - 1
            reason = None
            if idx < 0:
                reason = "grid_without_price"
            elif idx == 0:
                reason = "grid_without_prior_feature"
            else:
                price_row, feature_row = session_rows[idx], session_rows[idx - 1]
                price_age = (grid - price_row["timestamp"]).total_seconds()
                feature_age = (grid - feature_row["timestamp"]).total_seconds()
                if price_age > price_limit:
                    reason = "stale_price_grid_points"
                elif feature_age > feature_limit:
                    reason = "stale_feature_grid_points"
                elif not _valid(price_row) or not _valid(feature_row):
                    reason = "invalid_core_grid_points"
                else:
                    assert feature_row["timestamp"] < price_row["timestamp"] <= grid
                    call, put = abs(feature_row["total_call_gex"]), abs(feature_row["total_put_gex"])
                    gross = call + put
                    anchor = (call * feature_row["max_call_gex_strike"] + put * feature_row["max_put_gex_strike"]) / gross
                    points.append({
                        "timestamp": grid, "observed_at": price_row["timestamp"],
                        "feature_at": feature_row["timestamp"],
                        "y": np.log(price_row["spot_price"]) * 10000,
                        "g": feature_row["total_net_gex"] / gross,
                        "anchor": np.log(anchor) * 10000,
                        "price_age": price_age, "feature_age": feature_age,
                    })
                    counts["valid_grid_points_before_warmup"] += 1
            if reason:
                counts[reason] += 1
                if points:
                    fragments.append(points)
                    points = []
            grid += step
        if points:
            fragments.append(points)
        kept = 0
        for sequence, fragment in enumerate(fragments):
            warmup = min(len(fragment), price_anchor_window - 1)
            counts["price_anchor_warmup_points"] += warmup
            retained = fragment[warmup:]
            if len(retained) < min_block_points:
                counts["short_block_points"] += len(retained)
                counts["short_blocks"] += 1
                continue
            all_prices = np.array([p["y"] for p in fragment], dtype=float)
            # A trailing window including the current available spot is causal.
            rolling = np.convolve(all_prices, np.ones(price_anchor_window) / price_anchor_window, mode="valid")
            block = {
                "symbol": symbol, "session": session,
                "block": f"{source}:{symbol}:{session}:{sequence}", "source": source,
                "legacy": legacy, "price_anchor": rolling,
            }
            for key in ("timestamp", "observed_at", "feature_at"):
                block[key] = np.array([p[key] for p in retained], dtype="datetime64[us]")
            for key in ("y", "g", "anchor"):
                block[key] = np.array([p[key] for p in retained], dtype=float)
            blocks.append(block)
            kept += len(retained)
            price_ages.extend(p["price_age"] for p in retained)
            feature_ages.extend(p["feature_age"] for p in retained)
        sessions.append({"symbol": symbol, "session": session, "raw_rows": len(session_rows), "retained_grid_points": kept})
    counts["retained_blocks"] = len(blocks)
    counts["retained_grid_points"] = sum(len(b["y"]) for b in blocks)
    counts["sessions_with_rows"] = len(grouped)
    counts["sessions_retained"] = len({(b["symbol"], b["session"]) for b in blocks})
    return blocks, {
        "source": source, "legacy": legacy, "counts": dict(counts), "sessions": sessions,
        "max_price_age_seconds": price_limit, "max_feature_age_seconds": feature_limit,
        "retained_price_age": _age_summary(price_ages), "retained_feature_age": _age_summary(feature_ages),
    }


@contextmanager
def read_only_database(path: Path | str):
    """Open without migration, application imports, writes, or WAL checkpointing.

    Frozen backups with no WAL content use immutable mode to avoid sidecars.
    Active/WAL databases use SQLite's read-only transaction view. SQLite may
    still coordinate existing WAL/SHM files; database pages cannot be written.
    """
    path = Path(path).resolve(strict=True)
    wal = Path(str(path) + "-wal")
    is_backup = "legacy_" in path.name or "pre_migration_" in path.name
    immutable = is_backup and (not wal.exists() or wal.stat().st_size == 0)
    uri = path.as_uri() + "?mode=ro" + ("&immutable=1" if immutable else "")
    connection = sqlite3.connect(uri, uri=True, timeout=10)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        yield connection
    finally:
        connection.close()


def _semantic_check(connection, rows: list[dict], symbols: Sequence[str], samples: int) -> dict:
    """Bounded indexed raw-row checks; never scan the full options table."""
    columns = {r[1] for r in connection.execute("PRAGMA table_info(raw_option_greeks)")}
    required = {"option_type", "gex_value", "strike_price", "gamma", "open_interest"}
    if not required <= columns:
        return {"compatible": False, "reason": "Missing raw Greeks needed for semantic verification", "samples": []}
    checked = []
    for symbol in symbols:
        candidates = [r for r in rows if r["symbol"] == symbol and _valid(r)]
        indices = sorted(set(np.linspace(0, len(candidates) - 1, min(samples, len(candidates)), dtype=int))) if candidates else []
        for index in indices:
            row = candidates[index]
            fields = "option_type,gex_value,strike_price,gamma,open_interest"
            if "snapshot_id" in columns:
                raw = connection.execute(f"SELECT {fields} FROM raw_option_greeks WHERE snapshot_id=?", (row["id"],)).fetchall()
            else:
                raw = connection.execute(f"SELECT {fields} FROM raw_option_greeks WHERE symbol=? AND timestamp=?", (symbol, row["timestamp"])).fetchall()
            calls, puts = [], []
            for item in raw:
                side = str(item["option_type"] or "").upper()
                (puts if "PUT" in side or ("CALL" not in side and (item["gex_value"] or 0) < 0) else calls).append(item)
            call = sum(float(r["gex_value"] or 0) for r in calls)
            put = sum(float(r["gex_value"] or 0) for r in puts)
            gross = abs(call) + abs(put)
            raw_ratio = (call + put) / gross if gross else None
            gamma_call = sum(float(r["gamma"] or 0) * float(r["open_interest"] or 0) for r in calls)
            gamma_put = sum(float(r["gamma"] or 0) * float(r["open_interest"] or 0) for r in puts)
            gamma_gross = abs(gamma_call) + abs(gamma_put)
            gamma_ratio = (gamma_call - gamma_put) / gamma_gross if gamma_gross else None
            summary_ratio = row["total_net_gex"] / (abs(row["total_call_gex"]) + abs(row["total_put_gex"]))
            # Equal-GEX ties may legitimately pick any tied strike.
            call_peak = max((float(r["gex_value"] or 0) for r in calls), default=0)
            put_peak = min((float(r["gex_value"] or 0) for r in puts), default=0)
            call_walls = {float(r["strike_price"]) for r in calls if np.isclose(float(r["gex_value"] or 0), call_peak, rtol=1e-8, atol=1e-8)}
            put_walls = {float(r["strike_price"]) for r in puts if np.isclose(float(r["gex_value"] or 0), put_peak, rtol=1e-8, atol=1e-8)}
            ratio_match = raw_ratio is not None and gamma_ratio is not None and bool(np.isclose(summary_ratio, raw_ratio, atol=1e-7, rtol=1e-7) and np.isclose(summary_ratio, gamma_ratio, atol=1e-7, rtol=1e-7))
            walls_match = row["max_call_gex_strike"] in call_walls and row["max_put_gex_strike"] in put_walls
            checked.append({
                "symbol": symbol, "snapshot_id": row["id"], "timestamp": str(row["timestamp"]),
                "raw_rows": len(raw), "summary_ratio": summary_ratio,
                "raw_gex_ratio": raw_ratio, "raw_gamma_oi_ratio": gamma_ratio,
                "ratio_match": ratio_match, "contract_walls_match": walls_match,
                "compatible": bool(ratio_match and walls_match),
            })
    return {"compatible": bool(checked) and all(s["compatible"] for s in checked), "samples": checked,
            "limitation": "Sample checks support common ratio/wall definitions; they do not establish identical chain coverage or provider quality across versions."}


def default_sources(root: Path | str = ".") -> list[Path]:
    root = Path(root)
    paths = [root / "gex_data.db"]
    paths.extend(sorted(root.glob("gex_data_pre_migration_*.db")))
    legacy = sorted(root.glob("gex_data_legacy_*.db"))
    if legacy:
        paths.append(legacy[0])
    return [p for p in paths if p.exists()]


def load_blocks(
    root: Path | str = ".", symbols: Sequence[str] = ("SPX", "NDX"),
    sources: Sequence[Path | str] | None = None, *, grid_minutes: int = 5,
    min_block_points: int = 10, price_anchor_window: int = 6,
    semantic_samples_per_symbol: int = 3,
) -> tuple[list[dict], dict]:
    """Return common finite model blocks and a JSON-serializable quality audit.

    Incompatible sources are explicitly excluded. Duplicate symbol/timestamps
    retain source-order precedence only when every core feature agrees; a
    conflicting duplicate fails rather than silently choosing one revision.
    """
    if not symbols or semantic_samples_per_symbol < 1:
        raise ValueError("At least one symbol and one semantic sample are required")
    paths = list(sources) if sources is not None else default_sources(root)
    seen, blocks, source_audits = {}, [], []
    for input_path in paths:
        path = Path(input_path).resolve()
        with read_only_database(path) as connection:
            columns = {r[1] for r in connection.execute("PRAGMA table_info(gex_snapshots)")}
            if not {"id", "timestamp", "symbol", *CORE_COLUMNS} <= columns:
                raise ValueError(f"Missing core snapshot columns: {path}")
            fields = ",".join(("id", "timestamp", "symbol", *CORE_COLUMNS))
            placeholders = ",".join("?" for _ in symbols)
            rows = [dict(r) for r in connection.execute(f"SELECT {fields} FROM gex_snapshots WHERE symbol IN ({placeholders}) ORDER BY symbol,timestamp,id", tuple(symbols))]
            semantics = _semantic_check(connection, rows, symbols, semantic_samples_per_symbol)
            legacy = "collection_run_id" not in columns
        fingerprint = hashlib.sha256(repr([(r["symbol"], r["timestamp"], _signature(r)) for r in rows]).encode()).hexdigest()
        source_audit = {"path": str(path), "source": path.name, "legacy": legacy, "snapshot_rows": len(rows), "snapshot_sha256": fingerprint, "semantics": semantics}
        if not semantics["compatible"]:
            source_audit["excluded"] = "Raw sample ratio or contract-wall semantics did not verify"
            source_audits.append(source_audit)
            continue
        unique, duplicates = [], 0
        for row in rows:
            key = row["symbol"], _timestamp(row["timestamp"])
            signature = _signature(row)
            if key in seen:
                if seen[key] != signature:
                    raise ValueError(f"Conflicting cross-source snapshots at {key}")
                duplicates += 1
                continue
            seen[key] = signature
            unique.append(row)
        built, data_audit = build_blocks(unique, source=path.name, legacy=legacy, grid_minutes=grid_minutes, min_block_points=min_block_points, price_anchor_window=price_anchor_window)
        blocks.extend(built)
        source_audit.update(data_audit)
        source_audit["cross_source_duplicate_rows"] = duplicates
        source_audits.append(source_audit)
    blocks.sort(key=lambda b: (b["session"], b["symbol"], b["timestamp"][0], b["source"]))
    return blocks, {
        "symbols": list(symbols), "grid_minutes": grid_minutes,
        "min_block_points": min_block_points, "price_anchor_window": price_anchor_window,
        "timestamp_assumption": "Naive historical collector timestamps are America/New_York local wall time; upstream quote time is not recovered.",
        "calendar": "Explicit NYSE cash-session holidays/early closes for 2025–2026; other years rejected; unscheduled intraday halts are not independently identified.",
        "calendar_sources": CALENDAR_SOURCES,
        "feature_availability": "Features use the immediately previous snapshot, strictly earlier than the current observed spot snapshot; a conservative one-poll lag approximates collection completion.",
        "price_availability_limitation": "Current spot is timestamped at collection start, before the API response. It is an as-of proxy, not proof of quote availability at the exact grid instant.",
        "anchor_definition": "Log of total-side-absolute-GEX-weighted call/put contract-wall average, multiplied by 10000; a wall proxy, not the full-chain gamma center.",
        "price_anchor_definition": "Trailing mean of six available five-minute log prices by default, including current price; resets at every gap/session/source boundary.",
        "sources": source_audits,
        "retained_blocks": len(blocks), "retained_points": sum(len(b["y"]) for b in blocks),
        "retained_sessions": len({b["session"] for b in blocks}),
        "retained_symbol_sessions": len({(b["symbol"], b["session"]) for b in blocks}),
        "warnings": ["Rows and overlapping horizons are dependent; evaluate by chronological held-out sessions.", "No interpolation or forward fill crosses a missing grid point or session boundary.", "Sampled raw checks cannot establish all historical data-provider semantics."],
    }
