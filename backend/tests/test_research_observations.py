from datetime import datetime, timezone
import json
import sqlite3

from sqlalchemy import create_engine

from research_observations import (
    load_session_observations,
    localize_observation_time,
    normalize_quote_time,
)


def schema(conn):
    conn.executescript("""
        CREATE TABLE gex_snapshots(
            id INTEGER PRIMARY KEY, symbol TEXT, timestamp TEXT, spot_price REAL
        );
        CREATE TABLE raw_option_greeks(
            id INTEGER PRIMARY KEY, snapshot_id INTEGER, timestamp TEXT, symbol TEXT,
            expiration_date TEXT, osi_symbol TEXT, strike_price REAL, option_type TEXT,
            delta REAL, gamma REAL, open_interest INTEGER, underlying_price REAL,
            gex_value REAL, bid REAL, ask REAL, mid_price REAL, last_price REAL,
            bid_size INTEGER, ask_size INTEGER, volume INTEGER, implied_volatility REAL,
            bid_timestamp TEXT, ask_timestamp TEXT, last_timestamp TEXT
        );
    """)


def add_snapshot(conn, sid, timestamp, *, raw=True, expiry="2026-10-06", oi=0, volume=0):
    conn.execute("INSERT INTO gex_snapshots VALUES(?,?,?,?)", (sid, "SPX", timestamp, 100 + sid))
    if raw:
        conn.execute(
            "INSERT INTO raw_option_greeks VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sid, sid, timestamp, "SPX", expiry, f"SPX-{expiry}-{sid}", 100, "CALL",
                .5, .01, oi, 100 + sid, 0, 1, 2, 1.5, 1.75, 1, 1, volume, .2,
                "2026-10-06 14:00:00", "2026-10-06T14:00:00+00:00", "2026-10-06 14:00:30",
            ),
        )


def test_reader_loads_all_polls_expirations_zero_oi_and_honors_pair_cutoff():
    conn = sqlite3.connect(":memory:")
    schema(conn)
    add_snapshot(conn, 1, "2026-10-06 10:00:00", expiry="2026-10-06", oi=0, volume=10)
    add_snapshot(conn, 2, "2026-10-06 10:01:00", raw=False)
    add_snapshot(conn, 3, "2026-10-06 10:02:00", expiry="2026-10-09", oi=0, volume=20)
    add_snapshot(conn, 4, "2026-10-06 10:02:00", volume=999)
    add_snapshot(conn, 5, "2026-10-07 10:00:00", volume=999)
    conn.execute("PRAGMA query_only=ON")

    result = load_session_observations(
        conn, symbol="spx", as_of="2026-10-06 10:02:00", snapshot_id=3
    )

    assert [item["snapshot_id"] for item in result["snapshots"]] == [1, 2, 3]
    assert {row["expiration_date"] for row in result["rows"]} == {"2026-10-06", "2026-10-09"}
    assert all(row["open_interest"] == 0 for row in result["rows"])
    assert result["snapshots"][1]["contracts"] == []
    assert result["coverage"]["snapshots"] == 3
    assert result["rows"][0]["bid_timestamp_utc"].endswith("Z")
    assert result["available_at"] == "2026-10-06T14:02:00Z"
    json.dumps(result)
    conn.close()


def test_reader_supports_sqlalchemy_connection_and_old_missing_quote_columns():
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            "CREATE TABLE gex_snapshots(id INTEGER PRIMARY KEY, symbol TEXT, timestamp TEXT, spot_price REAL)"
        )
        conn.exec_driver_sql(
            """CREATE TABLE raw_option_greeks(
                snapshot_id INTEGER, timestamp TEXT, symbol TEXT, expiration_date TEXT,
                osi_symbol TEXT, strike_price REAL, option_type TEXT, delta REAL,
                gamma REAL, open_interest INTEGER, underlying_price REAL, volume INTEGER
            )"""
        )
        conn.exec_driver_sql(
            "INSERT INTO gex_snapshots VALUES(1, 'SPX', '2026-10-06 10:00:00', 100)"
        )
        conn.exec_driver_sql(
            "INSERT INTO raw_option_greeks VALUES(1,'2026-10-06 10:00:00','SPX','2026-10-06','C',100,'CALL',.5,.01,0,100,0)"
        )
    with engine.connect() as conn:
        result = load_session_observations(
            conn, symbol="SPX", as_of="2026-10-06 10:00:00", snapshot_id=1
        )
    assert len(result["rows"]) == 1
    assert result["rows"][0]["last_timestamp"] is None
    engine.dispose()


def test_clock_normalization_is_explicit_and_rejects_dst_ambiguity():
    normal, reason = localize_observation_time("2026-10-06 10:00:00", "America/New_York")
    assert reason is None
    assert normal == datetime(2026, 10, 6, 14, tzinfo=timezone.utc)
    assert localize_observation_time("2026-11-01 01:30:00", "America/New_York")[1] == "ambiguous_observation_time"
    assert localize_observation_time("2026-03-08 02:30:00", "America/New_York")[1] == "nonexistent_observation_time"
    assert normalize_quote_time("2026-10-06 14:00:00", naive_policy="unknown")[1] == "unknown_naive_quote_clock"
    offset, reason = normalize_quote_time("2026-10-06T10:00:00-04:00", naive_policy="unknown")
    assert reason is None
    assert offset == datetime(2026, 10, 6, 14, tzinfo=timezone.utc)
