from gamma_sweep import build_gamma_sweep


def option_row(strike, option_type="CALL", delta=0.5, gamma=0.08, open_interest=100):
    return {
        "strike_price": strike,
        "option_type": option_type,
        "delta": delta,
        "gamma": gamma,
        "open_interest": open_interest,
        "underlying_price": 100,
        "expiration_date": "2026-07-17",
        "gex_value": 0,
    }


def point_at(points, spot):
    return min(points, key=lambda point: abs(point["spot"] - spot))


def test_gamma_sweep_unavailable_when_no_valid_rows():
    result = build_gamma_sweep([], 100, "SPY")

    assert result["status"] == "unavailable"
    assert result["points"] == []


def test_gamma_sweep_points_stay_inside_collected_strike_range():
    rows = [option_row(99), option_row(101)]

    result = build_gamma_sweep(rows, 100, "SPY")

    assert result["status"] == "ok"
    spots = [point["spot"] for point in result["points"]]
    assert min(spots) >= 99
    assert max(spots) <= 101


def test_gamma_sweep_hedge_demand_is_anchored_at_current_spot():
    rows = [option_row(99), option_row(101)]

    result = build_gamma_sweep(rows, 100, "SPY")

    anchor = point_at(result["points"], 100)
    assert abs(anchor["hedge_shares"]) < 1e-6


def test_gamma_sweep_call_and_put_sign_convention():
    call_rows = [option_row(99, "CALL", delta=0.55), option_row(101, "CALL", delta=0.45)]
    put_rows = [option_row(99, "PUT", delta=-0.45), option_row(101, "PUT", delta=-0.55)]

    call_result = build_gamma_sweep(call_rows, 100, "SPY")
    put_result = build_gamma_sweep(put_rows, 100, "SPY")

    assert point_at(call_result["points"], 100)["net_gex"] > 0
    assert point_at(put_result["points"], 100)["net_gex"] < 0


def test_gamma_sweep_skips_invalid_contracts_without_failing():
    rows = [
        option_row(99),
        option_row(101),
        option_row(100, gamma=0),
        option_row(100, delta=2),
    ]

    result = build_gamma_sweep(rows, 100, "SPY")

    assert result["status"] == "ok"
    assert result["skipped_contracts"]["invalid_greeks"] == 1
    assert result["skipped_contracts"]["invalid_delta"] == 1


def test_gamma_sweep_skips_nan_greeks_without_poisoning_points():
    rows = [
        option_row(99),
        option_row(101),
        option_row(100, gamma=float("nan")),
    ]

    result = build_gamma_sweep(rows, 100, "SPY")

    assert result["status"] == "ok"
    assert result["skipped_contracts"]["invalid_greeks"] == 1
    assert all(point["net_gex"] == point["net_gex"] for point in result["points"])
    assert all(point["hedge_shares"] == point["hedge_shares"] for point in result["points"])


def test_gamma_sweep_current_spot_matches_static_gex_units():
    rows = [
        option_row(99, "CALL", delta=0.55, gamma=0.05, open_interest=10),
        option_row(101, "PUT", delta=-0.45, gamma=0.04, open_interest=20),
    ]
    spot = 100
    expected = (0.05 * 10 * 100 * spot * spot * 0.01) - (0.04 * 20 * 100 * spot * spot * 0.01)

    result = build_gamma_sweep(rows, spot, "SPY")

    assert result["status"] == "ok"
    assert abs(result["current"]["net_gex"] - expected) < 1e-6
    assert abs(result["current"]["hedge_shares"]) < 1e-6
