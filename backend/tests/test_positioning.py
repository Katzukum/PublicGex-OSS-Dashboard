from datetime import datetime, timedelta
import sqlite3
import unittest

from positioning import build_positioning, load_positioning


START = datetime(2026, 10, 2, 10)


def contract(sid, minute, *, side="CALL", strike=100, volume=0, oi=10,
             gamma=.01, expiry="2026-10-02", symbol="SPX"):
    return {
        "snapshot_id": sid, "timestamp": str(START + timedelta(minutes=minute)),
        "symbol": symbol, "expiration_date": expiry,
        "osi_symbol": f"{symbol}-{expiry}-{side}-{strike}", "strike_price": strike,
        "option_type": side, "gamma": gamma, "open_interest": oi,
        "volume": volume, "underlying_price": 100,
    }


def calculate(rows, sid, minute=15, spot=100):
    return build_positioning(rows, snapshot_id=sid, symbol="SPX",
                             as_of=str(START + timedelta(minutes=minute)), spot=spot)


class PositioningTests(unittest.TestCase):
    def test_unsigned_windows_use_current_gamma_and_spot(self):
        rows = []
        for i in range(16):
            rows.extend([contract(i + 1, i, volume=i * 10, gamma=.02 if i == 15 else .01),
                         contract(i + 1, i, side="PUT", volume=i * 5, gamma=.03)])
        result = calculate(rows, 16)
        level = result["strikes"][0]
        self.assertEqual(level["call_oi_gex"], 2000)
        self.assertEqual(level["put_oi_gex"], 3000)
        self.assertEqual(level["gross_oi_gex"], 5000)
        self.assertEqual(level["net_oi_proxy"], -1000)
        self.assertEqual(level["activity_5m"], 17500)
        self.assertEqual(level["activity_15m"], 52500)
        self.assertEqual(level["oi_persistence"], 1)
        self.assertEqual(result["dealer_direction"], "unknown")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["coverage"]["observed_minutes"], 15)

    def test_zero_oi_contracts_still_contribute_activity(self):
        rows = [contract(1, 14, volume=50, oi=0), contract(2, 15, volume=60, oi=0)]
        result = calculate(rows, 2)
        level = result["strikes"][0]
        self.assertEqual(level["gross_oi_gex"], 0)
        self.assertEqual(level["call_activity_5m"], 1000)
        self.assertIsNone(level["put_activity_5m"])
        self.assertIsNone(level["activity_5m"])
        self.assertEqual(result["top_levels"], [])

    def test_single_observation_is_unknown_activity_and_persistence(self):
        result = calculate([contract(1, 15, volume=100)], 1)
        level = result["strikes"][0]
        self.assertIsNone(level["call_activity_5m"])
        self.assertIsNone(level["oi_persistence"])
        self.assertEqual(result["coverage"]["valid_activity_contracts"], 0)

    def test_unchanged_volume_is_known_zero(self):
        rows = [contract(1, 14, volume=100), contract(2, 15, volume=100)]
        result = calculate(rows, 2)
        self.assertEqual(result["strikes"][0]["call_activity_5m"], 0)
        self.assertEqual(result["coverage"]["valid_activity_contracts"], 1)

    def test_window_boundary_never_allocates_pre_window_volume(self):
        rows = [contract(1, 9.5, volume=10), contract(2, 10.5, volume=90),
                contract(3, 11.5, volume=100), contract(4, 13, volume=110),
                contract(5, 15, volume=120)]
        result = calculate(rows, 5)
        self.assertEqual(result["strikes"][0]["call_activity_5m"], 3000)
        self.assertEqual(result["strikes"][0]["call_activity_15m"], 11000)

    def test_counter_reset_restarts_baseline_without_negative_activity(self):
        rows = [contract(1, 13, volume=100), contract(2, 14, volume=2),
                contract(3, 15, volume=7)]
        result = calculate(rows, 3)
        self.assertEqual(result["strikes"][0]["call_activity_5m"], 500)
        self.assertTrue(any("reset" in warning for warning in result["warnings"]))

    def test_missing_volume_breaks_sequence(self):
        rows = [contract(1, 12, volume=10), contract(2, 13, volume=None),
                contract(3, 14, volume=40), contract(4, 15, volume=45)]
        result = calculate(rows, 4)
        self.assertEqual(result["strikes"][0]["call_activity_5m"], 500)

    def test_missing_contract_does_not_bridge_intermediate_snapshot(self):
        rows = [contract(1, 13, volume=10), contract(1, 13, side="PUT", volume=10),
                contract(2, 14, volume=20), contract(3, 15, volume=30),
                contract(3, 15, side="PUT", volume=100)]
        level = calculate(rows, 3)["strikes"][0]
        self.assertEqual(level["call_activity_5m"], 2000)
        self.assertIsNone(level["put_activity_5m"])
        self.assertIsNone(level["activity_5m"])

    def test_gap_over_120_seconds_excludes_accumulated_volume(self):
        rows = [contract(1, 10, volume=10), contract(2, 13, volume=100),
                contract(3, 14, volume=110), contract(4, 15, volume=120)]
        result = calculate(rows, 4)
        self.assertEqual(result["strikes"][0]["call_activity_5m"], 2000)
        self.assertEqual(result["coverage"]["observed_minutes"], 2)
        self.assertEqual(result["coverage"]["max_gap_seconds"], 180)

    def test_filters_future_other_symbol_expiry_and_previous_day(self):
        rows = [contract(1, 14, volume=10), contract(2, 15, volume=20),
                contract(3, 16, volume=999999), contract(4, 15, volume=999999),
                contract(1, 14, volume=999999, expiry="2026-10-05"),
                contract(1, 14, volume=999999, symbol="NDX"),
                contract(5, -1440 + 14, volume=999999)]
        result = calculate(rows, 2)
        self.assertEqual(len(result["strikes"]), 1)
        self.assertEqual(result["strikes"][0]["call_activity_15m"], 1000)

    def test_missing_gamma_is_not_fabricated_zero_activity(self):
        rows = [contract(1, 14, volume=10), contract(2, 15, volume=20, gamma=None)]
        result = calculate(rows, 2)
        self.assertIsNone(result["strikes"][0]["call_activity_5m"])
        self.assertEqual(result["status"], "partial")

    def test_duplicate_contract_is_excluded_instead_of_double_counted(self):
        rows = [contract(1, 14, volume=10), contract(2, 15, volume=20),
                contract(2, 15, volume=20)]
        result = calculate(rows, 2)
        self.assertEqual(result["status"], "unavailable")

    def test_persistence_ranking_requires_repeated_adequate_snapshots(self):
        rows = []
        for minute in (13, 14, 15):
            for strike in range(100, 106):
                oi = 1000 if strike == 105 and minute == 15 else 106 - strike
                rows.append(contract(minute, minute, strike=strike, oi=oi, volume=minute))
        result = calculate(rows, 15)
        levels = {level["strike"]: level for level in result["strikes"]}
        self.assertEqual(levels[105]["oi_persistence"], 1 / 3)
        self.assertEqual(levels[100]["oi_persistence"], 1)
        self.assertEqual(result["top_levels"], [100, 101, 102, 103, 104])


class PositioningQueryTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript("""
            CREATE TABLE gex_snapshots(id INTEGER PRIMARY KEY, symbol TEXT, timestamp TEXT, spot_price REAL);
            CREATE TABLE raw_option_greeks(snapshot_id INTEGER, timestamp TEXT, symbol TEXT,
                expiration_date TEXT, osi_symbol TEXT, strike_price REAL, option_type TEXT,
                gamma REAL, open_interest INTEGER, volume INTEGER, underlying_price REAL);
        """)

    def tearDown(self):
        self.conn.close()

    def insert(self, row, raw=True):
        self.conn.execute("INSERT OR IGNORE INTO gex_snapshots VALUES(?,?,?,?)",
                          (row["snapshot_id"], row["symbol"], row["timestamp"], row["underlying_price"]))
        if raw:
            self.conn.execute("INSERT INTO raw_option_greeks VALUES(?,?,?,?,?,?,?,?,?,?,?)", tuple(row.values()))

    def test_loader_selects_one_baseline_and_never_future_or_other_expiry(self):
        for i in range(21):
            self.insert(contract(i + 1, i, volume=i * 10))
            self.insert(contract(i + 1, i, side="PUT", volume=i * 5))
        # A second expiry in earlier snapshots cannot become the selected profile.
        self.insert(contract(1, 0, volume=999999, expiry="2026-10-05"))
        self.conn.execute("PRAGMA query_only=ON")
        queries = []
        self.conn.set_trace_callback(queries.append)
        result = load_positioning(self.conn, 17)
        level = result["strikes"][0]
        self.assertEqual(level["activity_15m"], 22500)
        self.assertEqual(result["as_of"], str(START + timedelta(minutes=16)))
        self.assertEqual(len(queries), 2)
        self.assertEqual(result["coverage"]["observed_minutes"], 15)

    def test_empty_snapshot_breaks_activity_sequence(self):
        self.insert(contract(1, 13, volume=10))
        self.insert(contract(2, 14, volume=20), raw=False)
        self.insert(contract(3, 15, volume=100))
        result = load_positioning(self.conn, 3)
        self.assertIsNone(result["strikes"][0]["call_activity_15m"])

    def test_loader_missing_snapshot_is_unavailable(self):
        self.assertEqual(load_positioning(self.conn, 999)["status"], "unavailable")


def test_dashboard_and_trace_expose_positioning_without_changing_legacy_profile(monkeypatch):
    import atexit
    import appy
    # This unit test never starts the native runtime. Do not create it during
    # interpreter shutdown after pytest has restored its temporary import path.
    atexit.unregister(appy.shutdown_runtime)
    from models import Base, CollectionRun, GexSnapshot, RawOptionGreek
    from sqlalchemy import create_engine

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(CollectionRun.__table__.insert(), {"id": 1})
        for sid, minute in ((1, 14), (2, 15)):
            timestamp = START + timedelta(minutes=minute)
            conn.execute(GexSnapshot.__table__.insert(), {
                "id": sid, "collection_run_id": 1, "timestamp": timestamp,
                "symbol": "SPX", "spot_price": 100, "total_net_gex": 0,
                "total_call_gex": 1000, "total_put_gex": -1000,
            })
            for strike, oi in ((100, 10), (101, 0)):
                for side in ("CALL", "PUT"):
                    conn.execute(RawOptionGreek.__table__.insert(), {
                        "snapshot_id": sid, "timestamp": timestamp, "symbol": "SPX",
                        "expiration_date": timestamp.date(), "osi_symbol": f"{strike}-{side}",
                        "strike_price": strike, "option_type": side, "delta": .5 if side == "CALL" else -.5,
                        "gamma": .01, "open_interest": oi, "underlying_price": 100,
                        "gex_value": oi * 100 * (1 if side == "CALL" else -1), "volume": minute * 10,
                    })
    monkeypatch.setattr(appy, "engine", engine)
    monkeypatch.setattr(appy, "DB_SCHEMA_CURRENT", True)
    monkeypatch.setattr(appy, "_trace_cache", {})
    dashboard = appy._dashboard_data_from_engine(engine, "SPX", snapshot_id=2)
    assert "error" not in dashboard
    assert {row["strike_price"] for row in dashboard["profile"]} == {100}
    zero_oi = next(row for row in dashboard["positioning"]["strikes"] if row["strike"] == 101)
    assert zero_oi["gross_oi_gex"] == 0
    assert zero_oi["activity_5m"] == 2000
    trace = appy.get_trace_data("SPX")
    assert "error" not in trace
    assert {row["strike"] for row in trace["latest_profile"]} == {100}
    assert trace["positioning"] == dashboard["positioning"]
    assert trace["positioning"]["as_of"] == trace["timestamp"]
    with engine.connect() as conn:
        _, candidates = appy._latest_snapshot_and_raw_rows(conn, "SPX", snapshot_id=2)
        assert {row["strike_price"] for row in candidates} == {100}
    engine.dispose()


if __name__ == "__main__":
    unittest.main()
