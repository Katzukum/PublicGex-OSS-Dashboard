import argparse
import csv
import sqlite3
from pathlib import Path

from gex_levels import aggregate_gamma_levels


DEFAULT_DB_PATH = Path("gex_data.db")
DEFAULT_OUTPUT_PATH = (
    Path.home()
    / "Documents"
    / "NinjaTrader 8"
    / "OpenGammaCache"
    / "HistoricalGammaLevels.csv"
)
DEFAULT_SYMBOLS = ("NDX", "SPX")

CSV_FIELDS = [
    "timestamp",
    "symbol",
    "snapshot_spot_price",
    "strike",
    "gex",
    "is_key_level",
    "type",
]


def _connect_readonly(db_path: Path):
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def _snapshot_rows(conn, symbols):
    placeholders = ",".join("?" for _ in symbols)
    query = f"""
        SELECT id, timestamp, symbol, spot_price
        FROM gex_snapshots
        WHERE symbol IN ({placeholders})
          AND spot_price IS NOT NULL
          AND spot_price > 0
        ORDER BY timestamp ASC, symbol ASC, id ASC
    """
    return conn.execute(query, tuple(symbols))


def _raw_rows_for_snapshot(conn, snapshot_id: int):
    query = """
        SELECT strike_price, option_type, gex_value, open_interest
        FROM raw_option_greeks
        WHERE snapshot_id = ?
        ORDER BY strike_price ASC
    """
    return [dict(row) for row in conn.execute(query, (snapshot_id,))]


def iter_historical_gamma_rows(db_path=DEFAULT_DB_PATH, symbols=DEFAULT_SYMBOLS, per_side=5):
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    symbols = tuple(str(symbol).upper() for symbol in symbols)
    if not symbols:
        return

    conn = _connect_readonly(db_path)
    try:
        conn.row_factory = sqlite3.Row
        for snapshot in _snapshot_rows(conn, symbols):
            raw_rows = _raw_rows_for_snapshot(conn, snapshot["id"])
            if not raw_rows:
                continue

            spot = float(snapshot["spot_price"] or 0)
            levels = aggregate_gamma_levels(raw_rows, spot=spot, per_side=per_side)
            for level in levels:
                yield {
                    "timestamp": snapshot["timestamp"],
                    "symbol": snapshot["symbol"],
                    "snapshot_spot_price": spot,
                    "strike": level["strike"],
                    "gex": level["gex"],
                    "is_key_level": "1",
                    "type": level["type"],
                }
    finally:
        conn.close()


def export_historical_gamma_levels(
    db_path=DEFAULT_DB_PATH,
    output_path=DEFAULT_OUTPUT_PATH,
    symbols=DEFAULT_SYMBOLS,
    per_side=5,
):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    row_count = 0
    symbols_seen = set()
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in iter_historical_gamma_rows(db_path, symbols=symbols, per_side=per_side):
            writer.writerow(row)
            row_count += 1
            symbols_seen.add(row["symbol"])

    return {
        "output_path": output_path,
        "row_count": row_count,
        "symbols": sorted(symbols_seen),
    }


def parse_args():
    parser = argparse.ArgumentParser(description="Export historical key gamma levels for NinjaTrader backtests.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to gex_data.db")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="Output CSV path")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS), help="Comma-separated symbols to export")
    parser.add_argument("--per-side", type=int, default=5, help="Key gamma levels to export below and above spot")
    return parser.parse_args()


def main():
    args = parse_args()
    symbols = [symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()]
    result = export_historical_gamma_levels(
        db_path=Path(args.db),
        output_path=Path(args.output),
        symbols=symbols,
        per_side=args.per_side,
    )
    print(
        f"Wrote {result['row_count']} rows for {', '.join(result['symbols']) or 'no symbols'} "
        f"to {result['output_path']}"
    )


if __name__ == "__main__":
    main()
