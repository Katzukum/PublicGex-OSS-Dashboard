import csv
import sqlite3
import tempfile
import unittest
from pathlib import Path

from export_historical_gamma_levels import CSV_FIELDS, export_historical_gamma_levels


def _create_test_db(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE gex_snapshots (
            id INTEGER PRIMARY KEY,
            timestamp TEXT,
            symbol TEXT,
            spot_price REAL
        );

        CREATE TABLE raw_option_greeks (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER,
            strike_price REAL,
            option_type TEXT,
            gex_value REAL,
            open_interest INTEGER
        );
        """
    )
    return conn


def _insert_snapshot(conn, snapshot_id, timestamp, symbol, spot):
    conn.execute(
        "INSERT INTO gex_snapshots (id, timestamp, symbol, spot_price) VALUES (?, ?, ?, ?)",
        (snapshot_id, timestamp, symbol, spot),
    )


def _insert_option(conn, snapshot_id, strike, option_type, gex, open_interest=1):
    conn.execute(
        """
        INSERT INTO raw_option_greeks
            (snapshot_id, strike_price, option_type, gex_value, open_interest)
        VALUES (?, ?, ?, ?, ?)
        """,
        (snapshot_id, strike, option_type, gex, open_interest),
    )


class HistoricalGammaExportTests(unittest.TestCase):
    def test_export_schema_and_snapshot_spot_price(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gex_data.db"
            out_path = Path(tmp) / "HistoricalGammaLevels.csv"
            conn = _create_test_db(db_path)
            _insert_snapshot(conn, 1, "2026-06-11 09:30:00", "NDX", 20000.25)
            _insert_option(conn, 1, 19990, "PUT", -100)
            _insert_option(conn, 1, 20010, "CALL", 150)
            conn.commit()
            conn.close()

            result = export_historical_gamma_levels(db_path, out_path)

            self.assertEqual(result["row_count"], 2)
            with out_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(CSV_FIELDS, list(rows[0].keys()))
            self.assertEqual(rows[0]["symbol"], "NDX")
            self.assertEqual(rows[0]["snapshot_spot_price"], "20000.25")
            self.assertEqual(rows[0]["is_key_level"], "1")

    def test_export_limits_to_five_levels_per_side(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gex_data.db"
            out_path = Path(tmp) / "HistoricalGammaLevels.csv"
            conn = _create_test_db(db_path)
            _insert_snapshot(conn, 1, "2026-06-11 09:30:00", "SPX", 5000)

            for idx, strike in enumerate(range(4940, 5000, 10), start=1):
                _insert_option(conn, 1, strike, "PUT", -idx)
            for idx, strike in enumerate(range(5000, 5060, 10), start=1):
                _insert_option(conn, 1, strike, "CALL", idx)

            conn.commit()
            conn.close()

            result = export_historical_gamma_levels(db_path, out_path)

            self.assertEqual(result["row_count"], 10)
            with out_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            below = [float(row["strike"]) for row in rows if float(row["strike"]) < 5000]
            above = [float(row["strike"]) for row in rows if float(row["strike"]) >= 5000]
            self.assertEqual(len(below), 5)
            self.assertEqual(len(above), 5)

    def test_export_only_requested_symbols(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gex_data.db"
            out_path = Path(tmp) / "HistoricalGammaLevels.csv"
            conn = _create_test_db(db_path)
            _insert_snapshot(conn, 1, "2026-06-11 09:30:00", "NDX", 20000)
            _insert_snapshot(conn, 2, "2026-06-11 09:30:00", "SPY", 700)
            _insert_option(conn, 1, 19990, "PUT", -100)
            _insert_option(conn, 2, 690, "PUT", -100)
            conn.commit()
            conn.close()

            result = export_historical_gamma_levels(db_path, out_path, symbols=("NDX",))

            self.assertEqual(result["symbols"], ["NDX"])
            with out_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual({row["symbol"] for row in rows}, {"NDX"})


if __name__ == "__main__":
    unittest.main()
