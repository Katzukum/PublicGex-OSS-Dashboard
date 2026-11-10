from datetime import datetime, timedelta

import pytest

from flow_pressure import build_estimated_pressure


START = datetime(2026, 10, 6, 10, 0, 10)


def row(sid, at, *, osi="C", side="CALL", volume=0, delta=.5, gamma=.01,
        bid=1, ask=3, last=3, quote_time=None, symbol="SPX"):
    quote_time = quote_time if quote_time is not None else at
    return {
        "snapshot_id": sid, "timestamp": at.isoformat(), "symbol": symbol,
        "expiration_date": "2026-10-06", "osi_symbol": osi, "option_type": side,
        "volume": volume, "delta": delta, "gamma": gamma, "underlying_price": 100,
        "bid": bid, "ask": ask, "last_price": last,
        "bid_timestamp": quote_time.isoformat(), "ask_timestamp": quote_time.isoformat(),
        "last_timestamp": quote_time.isoformat(),
    }


def calculate(rows, as_of=None, config=None):
    return build_estimated_pressure(
        rows, symbol="SPX", as_of=(as_of or datetime(2026, 10, 6, 10, 2)).isoformat(), config=config,
    )


def test_four_bucket_signs_and_right_endpoint_minute():
    left, right = START, START + timedelta(seconds=40)
    rows = []
    specs = [
        ("CB", "CALL", .5, 3), ("CS", "CALL", .5, 1),
        ("PB", "PUT", -.5, 3), ("PS", "PUT", -.5, 1),
    ]
    for osi, side, delta, last in specs:
        rows += [row(1, left, osi=osi, side=side, delta=delta, last=last),
                 row(2, right, osi=osi, side=side, volume=5, delta=delta, last=last)]
    result = calculate(rows)
    point = result["points"][0]
    assert point["timestamp"] == "2026-10-06T10:01:00"
    assert point["impulse_dollars"] == 0
    assert point["buckets"]["call_buy"]["delta_dollars"] == 25_000
    assert point["buckets"]["call_sell"]["delta_dollars"] == -25_000
    assert point["buckets"]["put_buy"]["delta_dollars"] == -25_000
    assert point["buckets"]["put_sell"]["delta_dollars"] == 25_000
    assert result["coverage"]["classification_ratio"] == 1


def test_midpoint_is_neutral_and_bad_clock_is_unclassified_not_zero():
    left, right = START, START + timedelta(seconds=40)
    rows = [
        row(1, left, osi="neutral"), row(2, right, osi="neutral", volume=4, last=2),
        row(1, left, osi="unknown"), row(2, right, osi="unknown", volume=6, quote_time=left - timedelta(minutes=3)),
    ]
    result = calculate(rows)
    point = result["points"][0]
    assert point["impulse_dollars"] == 0
    assert point["neutral_volume"] == 4
    assert point["unclassified_volume"] == 6
    assert point["classification_ratio"] == pytest.approx(.4)
    assert point["status"] == "partial"
    assert result["coverage"]["discarded_by_reason"]["stale_quote"] == 1


def test_unchanged_volume_is_measured_zero_but_unclassified_only_is_null():
    left, right = START, START + timedelta(seconds=40)
    measured = calculate([row(1, left, volume=7), row(2, right, volume=7)])
    assert measured["points"][0]["impulse_dollars"] == 0
    assert measured["points"][0]["usable"] is True
    assert measured["coverage"]["classification_ratio"] is None

    unknown = calculate([
        row(1, left), row(2, right, volume=5, bid=None),
    ])
    assert unknown["points"][0]["impulse_dollars"] is None
    assert unknown["points"][0]["cumulative_dollars"] is None
    assert unknown["points"][0]["usable"] is False


@pytest.mark.parametrize(
    "mutator, reason",
    [
        (lambda rows: rows + [dict(rows[-1])], "duplicate_contract"),
        (lambda rows: [rows[0], {**rows[1], "volume": 1}], "volume_reset"),
        (lambda rows: [rows[0], {**rows[1], "timestamp": (START + timedelta(seconds=121)).isoformat()}], "gap_over_120_seconds"),
    ],
)
def test_invalid_intervals_never_create_impulses(mutator, reason):
    rows = [row(1, START, volume=10), row(2, START + timedelta(seconds=40), volume=20)]
    result = calculate(mutator(rows), as_of=datetime(2026, 10, 6, 10, 4))
    assert result["points"] == []
    assert result["coverage"]["discarded_by_reason"][reason] >= 1


def test_missing_gamma_only_nulls_gamma_activity():
    rows = [row(1, START), row(2, START + timedelta(seconds=40), volume=5, gamma=None)]
    point = calculate(rows)["points"][0]
    assert point["impulse_dollars"] == 25_000
    assert point["buckets"]["call_buy"]["gamma_activity"] is None


def test_reader_normalized_utc_clocks_are_used_with_local_display_minutes():
    left, right = START, START + timedelta(seconds=40)
    rows = [row(1, left), row(2, right, volume=5)]
    for value, available in zip(rows, ("2026-10-06T14:00:10Z", "2026-10-06T14:00:50Z")):
        value["available_at"] = available
        value["bid_timestamp_utc"] = available
        value["ask_timestamp_utc"] = available
        value["last_timestamp_utc"] = available
    result = calculate(rows)
    assert result["points"][0]["timestamp"] == "2026-10-06T10:01:00"
    assert result["points"][0]["available_at"] == "2026-10-06T14:01:00Z"
    assert result["points"][0]["impulse_dollars"] == 25_000


def test_quote_future_skew_boundary_and_missing_clocks_are_explicit():
    left, right = START, START + timedelta(seconds=40)
    rows = []
    for osi, skew in (("allowed", 5), ("future", 6)):
        rows += [row(1, left, osi=osi),
                 row(2, right, osi=osi, volume=5, quote_time=right + timedelta(seconds=skew))]
    missing = row(2, right, osi="missing", volume=5)
    missing["bid_timestamp"] = None
    rows += [row(1, left, osi="missing"), missing]
    result = calculate(rows)
    assert result["coverage"]["classified_volume"] == 5
    assert result["coverage"]["unclassified_volume"] == 10
    assert result["coverage"]["discarded_by_reason"]["future_quote"] == 1
    assert result["coverage"]["missing_clock_count"] == 1


def test_exact_120_second_snapshot_gap_is_valid():
    right = START + timedelta(seconds=120)
    result = calculate([row(1, START), row(2, right, volume=5)], as_of=right + timedelta(minutes=1))
    assert result["coverage"]["valid_intervals"] == 1
    assert result["points"][0]["impulse_dollars"] == 25_000


def test_gap_breaks_ema_and_cumulative_resumes_without_connecting_point():
    rows = []
    volumes = [0, 1, 2, 3, 4, 5]
    times = [START, START + timedelta(seconds=40), START + timedelta(minutes=1, seconds=40),
             START + timedelta(minutes=2, seconds=40), START + timedelta(minutes=4, seconds=50),
             START + timedelta(minutes=5, seconds=30)]
    for sid, (at, volume) in enumerate(zip(times, volumes), 1):
        rows.append(row(sid, at, volume=volume))
    result = calculate(rows, as_of=datetime(2026, 10, 6, 10, 7), config={"pressure_ema_1": 2, "pressure_ema_2": 3})
    by_time = {point["timestamp"]: point for point in result["points"]}
    assert by_time["2026-10-06T10:01:00"]["ema_2"] is None
    assert by_time["2026-10-06T10:02:00"]["ema_2"] == pytest.approx(8_333.333333333332)
    assert by_time["2026-10-06T10:03:00"]["ema_2"] == pytest.approx(12_777.777777777777)
    assert by_time["2026-10-06T10:04:00"]["cumulative_dollars"] is None
    assert by_time["2026-10-06T10:05:00"]["cumulative_dollars"] is None
    assert by_time["2026-10-06T10:06:00"]["cumulative_dollars"] == 20_000
    assert by_time["2026-10-06T10:06:00"]["ema_2"] is None


def test_current_minute_is_provisional_and_future_append_cannot_change_prefix():
    left, right = START, datetime(2026, 10, 6, 10, 1, 20)
    rows = [row(1, left), row(2, right, volume=5)]
    cutoff = datetime(2026, 10, 6, 10, 1, 30)
    prefix = calculate(rows, cutoff)
    assert prefix["points"] == []
    assert prefix["provisional"]["impulse_dollars"] == 25_000
    future = rows + [row(3, datetime(2026, 10, 6, 10, 2, 20), volume=100)]
    assert calculate(future, cutoff) == prefix

