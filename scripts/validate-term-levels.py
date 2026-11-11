"""Read-only matched-distance term-level validation on this app's history."""

from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from positioning_validation import validate_term_level_sources  # noqa: E402
from runtime_paths import data_path  # noqa: E402
from term_book import merged_strike_book  # noqa: E402

HORIZON_MINUTES = 15
MAX_GAP_SECONDS = 120


def _wall(rows, spot):
    calls = [row for row in rows if row["option_type"] == "CALL" and row["strike_price"] >= spot]
    puts = [row for row in rows if row["option_type"] == "PUT" and row["strike_price"] <= spot]
    walls = []
    if calls:
        walls.append(max(calls, key=lambda row: row["gex_value"])["strike_price"])
    if puts:
        walls.append(min(puts, key=lambda row: row["gex_value"])["strike_price"])
    return walls


def _touches(spot, strike, path):
    previous = spot
    for candidate in path:
        if (previous - strike) * (candidate - strike) <= 0:
            return True
        previous = candidate
    return False


def build_records(connection):
    connection.row_factory = sqlite3.Row
    snapshots = defaultdict(list)
    for row in connection.execute("SELECT id, symbol, timestamp, spot_price FROM gex_snapshots ORDER BY symbol, timestamp"):
        snapshots[row["symbol"]].append(row)
    terms = defaultdict(list)
    for row in connection.execute("SELECT id, snapshot_id, expiration_date FROM gex_term_snapshots WHERE is_combined = 0 ORDER BY expiration_date"):
        terms[row["snapshot_id"]].append(row)
    contracts = defaultdict(list)
    for row in connection.execute("SELECT term_snapshot_id, strike_price, option_type, gex_value FROM gex_term_contracts"):
        contracts[row["term_snapshot_id"]].append(dict(row))
    records = []
    for symbol, symbol_snaps in snapshots.items():
        for index, snapshot in enumerate(symbol_snaps):
            selected = terms.get(snapshot["id"], [])
            if not selected or not snapshot["spot_price"]:
                continue
            timestamp = datetime.fromisoformat(snapshot["timestamp"])
            end = timestamp + timedelta(minutes=HORIZON_MINUTES)
            forward = [row for row in symbol_snaps[index + 1:] if datetime.fromisoformat(row["timestamp"]) <= end]
            if not forward or datetime.fromisoformat(forward[-1]["timestamp"]) < end:
                continue
            path = [(timestamp, snapshot["spot_price"]), *[(datetime.fromisoformat(row["timestamp"]), row["spot_price"]) for row in forward]]
            if any((right[0] - left[0]).total_seconds() > MAX_GAP_SECONDS for left, right in zip(path, path[1:])):
                continue
            zero_rows = contracts[selected[0]["id"]]
            combined_rows = [row for term in selected for row in contracts[term["id"]]]
            for source, rows in (("0dte", zero_rows), ("combined_expirations", combined_rows)):
                for strike in _wall(rows, snapshot["spot_price"]):
                    records.append({"session_date": timestamp.date().isoformat(), "source": source,
                                    "distance_pct": 100 * abs(strike - snapshot["spot_price"]) / snapshot["spot_price"],
                                    "touched": _touches(snapshot["spot_price"], strike, [point[1] for point in path[1:]])})
            etf = {"SPX": "SPY", "NDX": "QQQ"}.get(symbol)
            if etf:
                etf_snap = next((row for row in reversed(snapshots[etf]) if row["timestamp"] <= snapshot["timestamp"]), None)
                if etf_snap and etf_snap["spot_price"] and terms.get(etf_snap["id"]):
                    etf_rows = [row for term in terms[etf_snap["id"]] for row in contracts[term["id"]]]
                    merged = merged_strike_book(combined_rows, etf_rows,
                                                snapshot["spot_price"], etf_snap["spot_price"])
                    if merged:
                        strike = max(merged, key=lambda row: abs(row["gex_value"]))["strike_price"]
                        records.append({"session_date": timestamp.date().isoformat(), "source": "merged_index_etf",
                                        "distance_pct": 100 * abs(strike - snapshot["spot_price"]) / snapshot["spot_price"],
                                        "touched": _touches(snapshot["spot_price"], strike, [point[1] for point in path[1:]])})
    return records


def main():
    db = data_path("gex_data.db")
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as connection:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='gex_term_snapshots'").fetchone() is None:
            print(json.dumps({"status": "unavailable", "reason": "term collection has not been enabled"}, indent=2))
            return
        records = build_records(connection)
    report = validate_term_level_sources(records)
    report["observations"] = len(records)
    report["status"] = "observational; decisions retain 0DTE levels"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
