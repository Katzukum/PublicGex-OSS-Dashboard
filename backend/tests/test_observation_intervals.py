from datetime import datetime, timedelta

from observation_intervals import build_observation_intervals, contract_identity


START = datetime(2026, 10, 6, 10)


def row(snapshot_id, minute, volume, *, osi="SPX-C", symbol="SPX"):
    return {
        "snapshot_id": snapshot_id,
        "timestamp": str(START + timedelta(minutes=minute)),
        "symbol": symbol,
        "expiration_date": "2026-10-06",
        "osi_symbol": osi,
        "option_type": "CALL",
        "volume": volume,
    }


def snapshot(snapshot_id, minute, rows):
    return {
        "snapshot_id": snapshot_id,
        "timestamp": str(START + timedelta(minutes=minute)),
        "symbol": "SPX",
        "contracts": rows,
    }


def test_identity_uses_upper_symbol_expiration_and_osi():
    value = row(1, 0, 0, symbol="spx")
    assert contract_identity(value) == ("SPX", "2026-10-06", "SPX-C")


def test_adjacent_integral_volume_and_exact_120_second_gap_are_valid():
    result = build_observation_intervals([
        snapshot(1, 0, [row(1, 0, 10)]),
        snapshot(2, 2, [row(2, 2, 17)]),
    ])
    interval = result["intervals"][0]
    assert interval["status"] == "valid"
    assert interval["volume_delta"] == 7
    assert interval["gap_seconds"] == 120


def test_missing_snapshot_contract_breaks_continuity_without_bridging():
    result = build_observation_intervals([
        snapshot(1, 0, [row(1, 0, 10)]),
        snapshot(2, 1, []),
        snapshot(3, 2, [row(3, 2, 30)]),
    ])
    assert [item["reason"] for item in result["intervals"]] == [
        "missing_right_contract", "missing_left_contract"
    ]
    assert result["coverage"]["valid_intervals"] == 0


def test_duplicate_reset_fractional_volume_equal_time_and_large_gap_have_reasons():
    duplicate = row(2, 1, 20)
    result = build_observation_intervals([
        snapshot(1, 0, [row(1, 0, 10)]),
        snapshot(2, 1, [duplicate, dict(duplicate)]),
        snapshot(3, 2, [row(3, 2, 5)]),
        snapshot(4, 2, [row(4, 2, 5.5)]),
        snapshot(5, 5, [row(5, 5, 8)]),
    ])
    reasons = result["coverage"]["discarded_by_reason"]
    assert reasons["duplicate_contract"] == 2
    assert reasons["non_increasing_time"] == 1
    assert reasons["gap_exceeded"] == 1


def test_volume_reset_and_fractional_counter_are_never_fabricated_as_zero():
    result = build_observation_intervals([
        snapshot(1, 0, [row(1, 0, 10)]),
        snapshot(2, 1, [row(2, 1, 5)]),
        snapshot(3, 2, [row(3, 2, 5.5)]),
    ])
    assert [item["reason"] for item in result["intervals"]] == [
        "volume_reset", "invalid_volume"
    ]
