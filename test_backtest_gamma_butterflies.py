import sqlite3
import tempfile
import unittest
from pathlib import Path

from backtest_gamma_butterflies import (
    _center_strike,
    _score_gamma_pit_levels,
    _score_gamma_wall_pit_levels,
    _score_hybrid_levels,
    iter_gamma_butterfly_backtest,
)


def _create_db(path: Path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE gex_snapshots (
            id INTEGER PRIMARY KEY,
            timestamp DATETIME,
            symbol VARCHAR,
            spot_price FLOAT
        );

        CREATE TABLE raw_option_greeks (
            id INTEGER PRIMARY KEY,
            snapshot_id INTEGER,
            timestamp DATETIME,
            symbol VARCHAR,
            expiration_date DATE,
            osi_symbol VARCHAR,
            strike_price FLOAT,
            option_type VARCHAR,
            delta FLOAT,
            gamma FLOAT,
            open_interest INTEGER,
            underlying_price FLOAT,
            gex_value FLOAT
        );
        """
    )
    conn.commit()
    return conn


def _insert_snapshot(conn, snapshot_id, timestamp, spot):
    conn.execute(
        "INSERT INTO gex_snapshots (id, timestamp, symbol, spot_price) VALUES (?, ?, 'SPX', ?)",
        (snapshot_id, timestamp, spot),
    )


def _insert_call(conn, row_id, snapshot_id, strike, delta, gamma, gex, open_interest=100):
    conn.execute(
        """
        INSERT INTO raw_option_greeks (
            id, snapshot_id, timestamp, symbol, expiration_date, osi_symbol,
            strike_price, option_type, delta, gamma, open_interest,
            underlying_price, gex_value
        )
        VALUES (?, ?, '2026-06-22 15:00:00', 'SPX', '2026-06-22', ?, ?, 'CALL', ?, ?, ?, 100, ?)
        """,
        (
            row_id,
            snapshot_id,
            f"SPXW260622C{int(strike * 1000):08d}",
            strike,
            delta,
            gamma,
            open_interest,
            gex,
        ),
    )


def _insert_put(conn, row_id, snapshot_id, strike, delta, gamma, gex, open_interest=100):
    conn.execute(
        """
        INSERT INTO raw_option_greeks (
            id, snapshot_id, timestamp, symbol, expiration_date, osi_symbol,
            strike_price, option_type, delta, gamma, open_interest,
            underlying_price, gex_value
        )
        VALUES (?, ?, '2026-06-22 15:00:00', 'SPX', '2026-06-22', ?, ?, 'PUT', ?, ?, ?, 100, ?)
        """,
        (
            row_id,
            snapshot_id,
            f"SPXW260622P{int(strike * 1000):08d}",
            strike,
            delta,
            gamma,
            open_interest,
            gex,
        ),
    )


class GammaButterflyBacktestTests(unittest.TestCase):
    def test_hybrid_center_method_blends_normalized_gex_and_charm(self):
        summary = {
            100.0: {"strike": 100.0, "net_gex": 100.0, "net_charm": 1.0},
            105.0: {"strike": 105.0, "net_gex": 60.0, "net_charm": 10.0},
        }

        _score_hybrid_levels(summary, gex_weight=0.5, charm_weight=0.5)

        self.assertEqual(_center_strike(summary, "hybrid")["strike"], 105.0)

    def test_gamma_pit_center_method_finds_valley_between_walls(self):
        summary = {
            95.0: {"strike": 95.0, "net_gex": 900.0},
            100.0: {"strike": 100.0, "net_gex": 100.0},
            105.0: {"strike": 105.0, "net_gex": -800.0},
        }

        _score_gamma_pit_levels(summary, pit_window=1)

        self.assertEqual(_center_strike(summary, "gamma-pit")["strike"], 100.0)
        self.assertGreater(summary[100.0]["pit_score"], 0)

    def test_gamma_wall_pit_uses_valley_between_major_gex_levels(self):
        summary = {
            90.0: {"strike": 90.0, "net_gex": 1200.0, "pit_score": 0.0},
            95.0: {"strike": 95.0, "net_gex": 75.0, "pit_score": 0.0},
            100.0: {"strike": 100.0, "net_gex": -1100.0, "pit_score": 0.0},
            105.0: {"strike": 105.0, "net_gex": 500.0, "pit_score": 0.0},
        }

        _score_gamma_wall_pit_levels(summary, pit_wall_count=2)

        selected = _center_strike(summary, "gamma-pit-walls")
        self.assertEqual(selected["strike"], 95.0)
        self.assertEqual(selected["pit_left_wall_strike"], 90.0)
        self.assertEqual(selected["pit_right_wall_strike"], 100.0)

    def test_builds_daily_trade_and_scores_settlement_profit(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gex_data.db"
            conn = _create_db(db_path)
            _insert_snapshot(conn, 1, "2026-06-22 14:30:00", 99.5)
            _insert_snapshot(conn, 2, "2026-06-22 15:00:00", 100.0)
            _insert_snapshot(conn, 3, "2026-06-22 15:30:00", 101.0)
            _insert_snapshot(conn, 4, "2026-06-22 16:00:00", 102.8)

            _insert_call(conn, 1, 2, 95, 0.8, 0.06, 100)
            _insert_call(conn, 2, 2, 100, 0.5, 0.08, 1000)
            _insert_call(conn, 3, 2, 105, 0.2, 0.06, 100)
            conn.commit()
            conn.close()

            rows = list(
                iter_gamma_butterfly_backtest(
                    db_path=db_path,
                    start_time="14:00",
                    end_time="15:00",
                    entry_time="15:00",
                    settlement_time="16:00",
                    side="call",
                    hit_center_tolerance=0.25,
                )
            )

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["center_strike"], 100)
        self.assertEqual(row["lower_strike"], 95)
        self.assertEqual(row["upper_strike"], 105)
        self.assertTrue(row["settled_in_tent"])
        self.assertTrue(row["settled_profitable"])
        self.assertGreater(row["pnl_points"], 0)

    def test_charm_center_method_uses_largest_charm_exposure(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gex_data.db"
            conn = _create_db(db_path)
            _insert_snapshot(conn, 1, "2026-06-22 15:00:00", 100.0)
            _insert_snapshot(conn, 2, "2026-06-22 16:00:00", 104.0)

            _insert_call(conn, 1, 1, 95, 0.8, 0.05, 100)
            _insert_call(conn, 2, 1, 100, 0.5, 0.08, 1000)
            _insert_call(conn, 3, 1, 105, 0.2, 0.06, 100, open_interest=100000)
            _insert_call(conn, 4, 1, 110, 0.05, 0.03, 100)
            conn.commit()
            conn.close()

            rows = list(
                iter_gamma_butterfly_backtest(
                    db_path=db_path,
                    start_time="14:00",
                    end_time="15:00",
                    entry_time="15:00",
                    settlement_time="16:00",
                    center_method="charm",
                    side="call",
                )
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["center_method"], "charm")
        self.assertEqual(rows[0]["center_strike"], 105)
        self.assertNotEqual(rows[0]["center_strike"], 100)

    def test_auto_side_uses_puts_when_center_is_below_entry_spot(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "gex_data.db"
            conn = _create_db(db_path)
            _insert_snapshot(conn, 1, "2026-06-22 15:00:00", 100.0)
            _insert_snapshot(conn, 2, "2026-06-22 16:00:00", 96.0)

            _insert_call(conn, 1, 1, 90, 0.95, 0.02, 100)
            _insert_call(conn, 2, 1, 95, 0.8, 0.05, 2000)
            _insert_call(conn, 3, 1, 100, 0.5, 0.08, 100)
            _insert_put(conn, 4, 1, 90, -0.05, 0.02, -100)
            _insert_put(conn, 5, 1, 95, -0.2, 0.05, -1000)
            _insert_put(conn, 6, 1, 100, -0.5, 0.08, -100)
            conn.commit()
            conn.close()

            rows = list(
                iter_gamma_butterfly_backtest(
                    db_path=db_path,
                    start_time="14:00",
                    end_time="15:00",
                    entry_time="15:00",
                    settlement_time="16:00",
                )
            )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["center_strike"], 95)
        self.assertEqual(rows[0]["pricing_side"], "PUT")


if __name__ == "__main__":
    unittest.main()
