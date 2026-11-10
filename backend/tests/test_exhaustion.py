from datetime import datetime, timedelta

import pytest

from exhaustion import build_exhaustion_events, build_observed_spot_bars


FIRST_END = datetime(2026, 10, 6, 10, 1)


def point(index, flows, *, cumulative=None, ratio=1.0, usable=True, ema=None):
    at = FIRST_END + timedelta(minutes=index)
    buckets = {}
    signs = {"call_buy": 1, "call_sell": -1, "put_buy": -1, "put_sell": 1}
    for name in signs:
        value = float(flows.get(name, 0))
        buckets[name] = {
            "classified_volume": 1 if value else 0,
            "delta_shares": signs[name] * value / 100,
            "delta_dollars": signs[name] * value,
            "gamma_activity": value,
        }
    return {
        "timestamp": at.isoformat(), "available_at": at.isoformat(), "status": "ready",
        "usable": usable, "classification_ratio": ratio,
        "cumulative_dollars": float(index if cumulative is None else cumulative),
        "ema_15": ema, "buckets": buckets,
    }


def pressure(points):
    return {
        "model": "estimated_hedge_pressure_v1", "status": "ready", "symbol": "SPX",
        "session": "2026-10-06", "as_of": points[-1]["available_at"], "points": points,
    }


def spot_rows(count, closes=None):
    closes = closes or [100 + index for index in range(count)]
    rows = []
    for index, close in enumerate(closes):
        start = FIRST_END + timedelta(minutes=index - 1)
        rows += [
            {"timestamp": (start + timedelta(seconds=10)).isoformat(), "price": close},
            {"timestamp": (start + timedelta(seconds=40)).isoformat(), "price": close},
        ]
    return rows


def declining_extreme_flows(count=31, buckets=("call_buy",)):
    values = []
    for index in range(count):
        amount = 1
        if index == count - 3:
            amount = 100
        elif index == count - 2:
            amount = 90
        elif index == count - 1:
            amount = 80
        values.append({name: amount for name in buckets})
    return values


def run(points, spots=None, config=None, envelopes=(), as_of=None):
    return build_exhaustion_events(
        pressure(points), spots if spots is not None else spot_rows(len(points)), envelopes,
        config=config, as_of=(as_of or points[-1]["available_at"]),
    )


def test_observed_bars_require_two_distinct_sample_times_and_completion():
    rows = [
        {"timestamp": "2026-10-06T10:00:10", "price": 100},
        {"timestamp": "2026-10-06T10:00:40", "price": 102},
        {"timestamp": "2026-10-06T10:01:10", "price": 103},
        {"timestamp": "2026-10-06T10:02:10", "price": 999},
    ]
    bars = build_observed_spot_bars(rows, as_of="2026-10-06T10:02:00")
    assert bars[0] == {
        "timestamp": "2026-10-06T10:01:00", "available_at": "2026-10-06T10:01:00",
        "open": 100, "high": 102, "low": 100, "close": 102, "sample_count": 2,
        "status": "ready", "eligible": True, "bar_source": "collector_spot_observations",
    }
    assert bars[1]["sample_count"] == 1
    assert bars[1]["eligible"] is False
    assert len(bars) == 2


def test_all_four_buckets_emit_with_expected_labels_and_hedge_directions():
    names = tuple(("call_buy", "call_sell", "put_buy", "put_sell"))
    flows = declining_extreme_flows(buckets=names)
    points = [point(index, value) for index, value in enumerate(flows)]
    result = run(points, config={"exhaustion_price_filter_enabled": False})
    events = {(event["side"], event["action"]): event for event in result["events"]}
    assert set(events) == {("CALL", "BUY"), ("CALL", "SELL"), ("PUT", "BUY"), ("PUT", "SELL")}
    assert events[("CALL", "BUY")]["hedge_direction"] == 1
    assert events[("CALL", "SELL")]["hedge_direction"] == -1
    assert events[("PUT", "BUY")]["hedge_direction"] == -1
    assert events[("PUT", "SELL")]["hedge_direction"] == 1
    assert events[("CALL", "BUY")]["label"] == "Estimated Call Buy"
    assert all(event["evidence_flags"]["flow_deceleration"] for event in events.values())
    assert all("flow_deceleration" in event["evidence"] for event in events.values())
    assert all(event["flow_percentile"] == pytest.approx(28 / 30) for event in events.values())


@pytest.mark.parametrize(
    "bucket,current_close,expected_position",
    [("call_buy", 107, .70), ("put_buy", 103, .30)],
)
def test_price_filter_boundaries_are_inclusive(bucket, current_close, expected_position):
    flows = declining_extreme_flows(buckets=(bucket,))
    points = [point(index, value) for index, value in enumerate(flows)]
    closes = [100.0] * len(points)
    closes[-5:] = [100, 110, 105, 106, current_close]
    result = run(points, spots=spot_rows(len(points), closes))
    assert len(result["events"]) == 1
    assert result["events"][0]["price_position"] == pytest.approx(expected_position)


def test_equal_flow_ties_use_less_than_or_equal_percentile_definition():
    points = [point(index, {"call_buy": 10}) for index in range(31)]
    # Create weakening without changing the tied bucket flow.
    points[-3]["cumulative_dollars"] = 10
    points[-2]["cumulative_dollars"] = 20
    points[-1]["cumulative_dollars"] = 20
    result = run(points, config={"exhaustion_price_filter_enabled": False})
    assert result["events"][0]["flow_percentile"] == 1
    assert result["events"][0]["evidence_flags"]["pressure_turn"] is True


def test_ema_reversal_can_supply_the_required_weakening_evidence():
    points = [point(index, {"call_buy": 1}, cumulative=100) for index in range(31)]
    points[-1]["buckets"]["call_buy"]["delta_dollars"] = 100
    points[-2]["cumulative_dollars"], points[-2]["ema_15"] = 20, 10
    points[-1]["cumulative_dollars"], points[-1]["ema_15"] = 5, 10
    result = run(
        points, spots=spot_rows(len(points), [100.0] * len(points)),
        config={"exhaustion_price_filter_enabled": False},
    )
    assert result["events"][0]["evidence"] == ["ema15_reversal"]


def test_price_pressure_divergence_can_emit_without_other_weakening_flags():
    points = [point(index, {"call_buy": 1}, cumulative=100) for index in range(31)]
    points[-1]["buckets"]["call_buy"]["delta_dollars"] = 100
    points[-1]["cumulative_dollars"] = 90
    closes = [100.0] * len(points)
    closes[-6:] = [101, 102, 103, 104, 105, 106]
    result = run(points, spots=spot_rows(len(points), closes))
    event = result["events"][0]
    assert event["evidence_flags"]["pressure_price_divergence"] is True
    assert event["evidence_flags"]["pressure_turn"] is False
    assert event["evidence_flags"]["flow_deceleration"] is False


def test_envelope_proximity_alone_cannot_trigger_event():
    points = [point(index, {"call_buy": 10}, cumulative=index) for index in range(31)]
    current_price = 130
    envelopes = [{
        "available_at": points[-1]["available_at"],
        "flow": {"major": {"level": current_price}, "minor": {"level": current_price + 10}},
    }]
    result = run(points, config={"exhaustion_price_filter_enabled": False}, envelopes=envelopes)
    assert result["events"] == []
    assert result["status"] == "ready"


def test_cooldown_suppresses_before_five_minutes_and_allows_exact_boundary():
    amounts = [1] * 36
    amounts[28:31] = [100, 90, 80]
    amounts[31] = 1
    amounts[32:36] = [100, 90, 80, 70]
    points = [point(index, {"call_buy": amount}) for index, amount in enumerate(amounts)]
    result = run(points, config={"exhaustion_price_filter_enabled": False, "exhaustion_sensitivity": "low"})
    event_times = [event["timestamp"] for event in result["events"]]
    assert event_times == [points[30]["timestamp"], points[35]["timestamp"]]
    assert result["coverage"]["suppressed_by_cooldown"] == 2


def test_filter_disabled_still_requires_an_eligible_current_bar():
    flows = declining_extreme_flows()
    points = [point(index, value) for index, value in enumerate(flows)]
    spots = spot_rows(len(points) - 1)
    spots.append({"timestamp": (FIRST_END + timedelta(minutes=29, seconds=10)).isoformat(), "price": 130})
    result = run(points, spots=spots, config={"exhaustion_price_filter_enabled": False})
    assert result["events"] == []


def test_all_sensitivity_presets_and_config_identity_are_deterministic():
    flows = declining_extreme_flows()
    points = [point(index, value) for index, value in enumerate(flows)]
    results = {
        name: run(points, config={"exhaustion_price_filter_enabled": False, "exhaustion_sensitivity": name})
        for name in ("low", "medium", "high", "strong")
    }
    high, strong = results["high"], results["strong"]
    assert all(len(results[name]["events"]) == 1 for name in ("low", "medium", "high"))
    assert strong["events"] == []
    assert high["config_fingerprint"] != strong["config_fingerprint"]
    assert run(points, config={"exhaustion_price_filter_enabled": False})["events"][0]["id"] == high["events"][0]["id"]


def test_future_data_does_not_change_prefix_events():
    flows = declining_extreme_flows()
    points = [point(index, value) for index, value in enumerate(flows)]
    prefix = run(points, config={"exhaustion_price_filter_enabled": False})
    future_points = points + [point(31, {"call_buy": 999}, cumulative=-999)]
    future_spots = spot_rows(len(future_points))
    replay = run(
        future_points, spots=future_spots, config={"exhaustion_price_filter_enabled": False},
        as_of=points[-1]["available_at"],
    )
    assert replay["events"] == prefix["events"]
    assert replay["current"] == prefix["current"]


def test_normalized_utc_availability_clocks_align_pressure_and_spot_bars():
    flows = declining_extreme_flows()
    points = [point(index, value) for index, value in enumerate(flows)]
    for value in points:
        local = datetime.fromisoformat(value["timestamp"])
        value["available_at"] = (local + timedelta(hours=4)).isoformat() + "Z"
    spots = spot_rows(len(points))
    for value in spots:
        local = datetime.fromisoformat(value["timestamp"])
        value["available_at"] = (local + timedelta(hours=4)).isoformat() + "Z"
    result = run(points, spots=spots, config={"exhaustion_price_filter_enabled": False})
    assert len(result["events"]) == 1
    assert result["events"][0]["timestamp"] == points[-1]["timestamp"]
    assert result["events"][0]["available_at"].endswith("Z")

