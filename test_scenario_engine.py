import pytest

from scenario_engine import build_scenario_workspace


PROFILE = [{"strike_price": strike} for strike in range(95, 106)]
QUALITY = {"score": 0.9, "label": "HIGH", "warnings": [], "age_seconds": 5}
CONTEXT = {"event_risk": {"state": "NORMAL"}, "implied_move": 4, "warnings": []}


@pytest.mark.parametrize(
    ("snapshot", "score", "expected"),
    [
        ({"symbol": "SPX", "id": 1, "spot_price": 100, "flip_strike": 100, "total_net_gex": 10}, 0, "PIN_MEAN_REVERSION"),
        ({"symbol": "SPX", "id": 2, "spot_price": 101, "flip_strike": 100, "total_net_gex": -10}, 0.8, "UPSIDE_EXPANSION"),
        ({"symbol": "SPX", "id": 3, "spot_price": 99, "flip_strike": 100, "total_net_gex": -10}, -0.8, "DOWNSIDE_EXPANSION"),
        ({"symbol": "SPX", "id": 4, "spot_price": 101, "flip_strike": 100, "total_net_gex": -10}, 0, "NO_TRADE"),
    ],
)
def test_scenario_types(snapshot, score, expected):
    result = build_scenario_workspace(snapshot, PROFILE, CONTEXT, QUALITY, market_score=score)
    assert result["active_scenario"]["scenario_type"] == expected
    assert result["active_scenario"]["scenario_id"].startswith("scn_")


@pytest.mark.parametrize(
    ("quality", "context", "profile", "blocker"),
    [
        ({"score": 0.44, "warnings": []}, CONTEXT, PROFILE, "data quality below 45%"),
        (QUALITY, {"event_risk": {"state": "BLOCKED"}, "warnings": []}, PROFILE, "blocked event window"),
        (QUALITY, CONTEXT, [], "required snapshot/profile inputs unavailable"),
    ],
)
def test_every_eligibility_gate_forces_no_trade(quality, context, profile, blocker):
    snapshot = {"symbol": "SPX", "id": 1, "spot_price": 100, "flip_strike": 100, "total_net_gex": 10}
    result = build_scenario_workspace(snapshot, profile, context, quality, market_score=1)
    assert result["active_scenario"]["scenario_type"] == "NO_TRADE"
    assert blocker in result["eligibility"]["blockers"]


def test_missing_required_zone_is_not_manufactured():
    snapshot = {"symbol": "SPX", "id": 1, "spot_price": 100, "flip_strike": None, "total_net_gex": -10}
    result = build_scenario_workspace(snapshot, [{"strike_price": 100}], CONTEXT, QUALITY, market_score=1)
    assert result["active_scenario"]["scenario_type"] == "NO_TRADE"
    assert result["active_scenario"]["target_zone"] is None
