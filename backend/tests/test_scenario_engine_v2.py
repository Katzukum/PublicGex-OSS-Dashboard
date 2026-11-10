from datetime import datetime, timedelta
import random

from levels import build_levels
from scenario_engine_v2 import build_scenario_workspace_v2


def _profile(strikes, call_gex, put_gex):
    return [
        {"strike_price": strike, "option_type": side, "gex_value": values.get(strike, 0)}
        for strike in strikes
        for side, values in (("CALL", call_gex), ("PUT", put_gex))
    ]


def _context(move=4, consumed=0.8, realized=0.01):
    return {"implied_move": move, "range_consumed": consumed, "realized_volatility_15m": realized,
            "event_risk": {"state": "NORMAL"}, "warnings": []}


def _snapshot(spot=101, zero=100, net=100, at="2026-11-10T15:00:00-05:00"):
    return {"id": 1, "symbol": "SPX", "timestamp": at, "spot_price": spot,
            "flip_strike": zero, "total_net_gex": net}


def _sweep(zero):
    return {"status": "ok", "zero_crossings": {"all": [zero]}}


def _result(snapshot, profile, *, context=None, sweep=None, history=None):
    return build_scenario_workspace_v2(snapshot, profile, context or _context(), {"score": 0.9},
                                       gamma_sweep=sweep or _sweep(snapshot["flip_strike"]),
                                       spot_history=history or [])


def test_level_definitions_and_proxy_fallback():
    snapshot = _snapshot()
    profile = _profile((95, 100, 101, 105), {95: 1, 100: 2, 101: 30, 105: 5},
                       {95: -8, 100: -4, 101: -20, 105: -1})
    levels = build_levels(snapshot, profile, _sweep(99.5), 4)
    assert levels["zero_gamma"] == 99.5
    assert levels["zero_gamma_quality"] == "modeled"
    assert levels["call_wall"] == 101
    assert levels["put_wall"] == 101
    assert levels["pin_strike"] == 101
    assert levels["pin_gross_share_near_spot"] > 0.15
    fallback = build_levels(snapshot, profile, {"status": "unavailable"}, 4)
    assert fallback["zero_gamma"] == 100
    assert fallback["zero_gamma_quality"] == "proxy"
    assert fallback["warnings"]


def test_late_positive_pin_and_early_no_pin():
    profile = _profile((99, 100, 101, 102), {99: 1, 100: 3, 101: 30, 102: 2},
                       {99: -2, 100: -2, 101: -10, 102: -1})
    late = _result(_snapshot(), profile)
    assert late["active_scenario"]["scenario_type"] == "PIN"
    assert late["active_scenario"]["pin_band_low"] == 99
    assert late["active_scenario"]["pin_band_high"] == 103
    early = _result(_snapshot(at="2026-11-10T14:29:00-05:00"), profile,
                    context=_context(consumed=0))
    assert early["active_scenario"]["scenario_type"] == "NO_TRADE"


def test_negative_regime_expansions_follow_price_move():
    profile = _profile((90, 95, 100, 105, 110), {90: 1, 95: 2, 100: 2, 105: 3, 110: 12},
                       {90: -2, 95: -12, 100: -1, 105: -2, 110: -1})
    now = datetime.fromisoformat("2026-11-10T15:00:00-05:00")
    prior = (now - timedelta(minutes=15)).isoformat()
    down = _result(_snapshot(spot=100, zero=105, net=-100), profile,
                   history=[{"timestamp": prior, "spot_price": 110}])
    assert down["active_scenario"]["scenario_type"] == "DOWNSIDE_EXPANSION"
    assert down["active_scenario"]["target"] == 95
    assert down["active_scenario"]["invalidation"] == 105
    up = _result(_snapshot(spot=100, zero=105, net=-100), profile,
                 history=[{"timestamp": prior, "spot_price": 90}])
    assert up["active_scenario"]["scenario_type"] == "UPSIDE_EXPANSION"
    assert up["active_scenario"]["target"] == 105
    assert up["active_scenario"]["invalidation"] == 95


def test_positive_regime_fades_the_wall_being_tested():
    call_profile = _profile((100, 102, 103, 104, 105),
                            {100: 1, 102: 2, 103: 1, 104: 30, 105: 1},
                            {100: -1, 102: -2, 103: -40, 104: -1, 105: -1})
    call_fade = _result(_snapshot(spot=104, zero=100, at="2026-11-10T13:00:00-05:00"), call_profile)
    assert call_fade["active_scenario"]["scenario_type"] == "FADE_CALL_WALL"
    assert call_fade["active_scenario"]["bias"] == "PUT"
    assert call_fade["active_scenario"]["target"] == 103

    put_profile = _profile((94, 95, 96, 97, 99),
                           {94: 1, 95: 1, 96: 1, 97: 40, 99: 1},
                           {94: -1, 95: -1, 96: -30, 97: -1, 99: -1})
    put_fade = _result(_snapshot(spot=96, zero=95, at="2026-11-10T13:00:00-05:00"), put_profile)
    assert put_fade["active_scenario"]["scenario_type"] == "FADE_PUT_WALL"
    assert put_fade["active_scenario"]["bias"] == "CALL"
    assert put_fade["active_scenario"]["target"] == 97


def test_missing_implied_move_and_transition_block_all_scenarios():
    profile = _profile((95, 100, 105), {95: 1, 100: 5, 105: 2}, {95: -2, 100: -1, 105: -1})
    missing = _result(_snapshot(), profile, context=_context(move=None))
    assert missing["active_scenario"]["scenario_type"] == "NO_TRADE"
    assert "implied move unavailable" in missing["eligibility"]["blockers"]
    transition = _result(_snapshot(spot=100, zero=100, net=100), profile)
    assert transition["active_scenario"]["scenario_type"] == "NO_TRADE"


def test_directional_level_and_regime_invariants_on_random_profiles():
    rng = random.Random(42)
    for index in range(200):
        spot = rng.uniform(95, 105)
        zero = rng.uniform(95, 105)
        net = rng.choice((-1, 1)) * rng.uniform(1, 100)
        profile = _profile(range(90, 111),
                           {strike: rng.uniform(0, 40) for strike in range(90, 111)},
                           {strike: -rng.uniform(0, 40) for strike in range(90, 111)})
        snap = _snapshot(spot=spot, zero=zero, net=net, at="2026-11-10T13:00:00-05:00") | {"id": index}
        history = [{"timestamp": "2026-11-10T12:45:00-05:00", "spot_price": spot + rng.uniform(-5, 5)}]
        result = _result(snap, profile, history=history)
        active = result["active_scenario"]
        assert active["scenario_type"] != "PIN"
        if active["scenario_type"] in ("UPSIDE_EXPANSION", "DOWNSIDE_EXPANSION"):
            assert result["regime"] == "negative"
        if active["bias"] == "CALL":
            assert active["target"] > spot > active["invalidation"]
            assert active["reward_to_risk"] >= 1
        elif active["bias"] == "PUT":
            assert active["target"] < spot < active["invalidation"]
            assert active["reward_to_risk"] >= 1
