from option_envelopes import build_option_envelopes


def option(strike, side, *, gamma=.01, volume=10, oi=10, delta=None):
    if delta is None:
        delta = .5 if side == "CALL" else -.5
    return {
        "strike_price": strike,
        "option_type": side,
        "gamma": gamma,
        "volume": volume,
        "open_interest": oi,
        "delta": delta,
        "underlying_price": 100,
        "expiration_date": "2026-10-06",
    }


def test_flow_concentration_is_signed_ranked_and_returns_absolute_strengths():
    rows = [
        option(98, "CALL", volume=10, gamma=.01),
        option(100, "PUT", volume=20, gamma=.01),
        option(102, "CALL", volume=15, gamma=.01),
    ]
    result = build_option_envelopes(rows, spot=100, source="volume_gamma", symbol="SPY")
    assert result["major_gamma"] == 100
    assert result["major_strength"] == 2000
    assert result["minor_gamma"] == 102
    assert result["minor_strength"] == 1500


def test_exact_call_put_cancellation_does_not_create_a_ranked_level():
    rows = [
        option(99, "CALL", volume=10), option(99, "PUT", volume=10),
        option(101, "CALL", volume=5),
    ]
    result = build_option_envelopes(rows, spot=100, symbol="SPY")
    assert result["major_gamma"] == 101
    assert result["minor_gamma"] is None
    assert result["status"] == "partial"


def test_ranking_ties_use_distance_then_lower_strike():
    rows = [option(98, "CALL"), option(101, "CALL"), option(99, "CALL")]
    result = build_option_envelopes(rows, spot=100, symbol="SPY")
    assert result["major_gamma"] == 99
    assert result["minor_gamma"] == 101


def test_zero_weight_is_measured_but_missing_weight_is_excluded():
    rows = [option(99, "CALL", volume=0), option(101, "PUT", volume=None)]
    result = build_option_envelopes(rows, spot=100, symbol="SPY")
    assert result["coverage"]["usable_contracts"] == 1
    assert result["coverage"]["excluded_by_reason"] == {"invalid_weight": 1}
    assert result["major_gamma"] is None


def test_structural_and_flow_modes_use_their_own_weights():
    rows = [
        option(99, "CALL", volume=100, oi=1),
        option(101, "PUT", volume=1, oi=100),
    ]
    flow = build_option_envelopes(rows, spot=100, source="volume_gamma", symbol="SPY")
    structural = build_option_envelopes(rows, spot=100, source="oi_gamma", symbol="SPY")
    assert flow["major_gamma"] == 99
    assert structural["major_gamma"] == 101


def test_nearest_crossing_tie_chooses_lower_and_all_zero_curve_is_indeterminate(monkeypatch):
    rows = [option(99, "CALL"), option(101, "PUT")]

    def tied(*args, **kwargs):
        return {
            "status": "ok", "range": {"min": 90, "max": 110, "step": .25},
            "zero_crossings": {"all": [101, 99]}, "skipped_contracts": {},
            "points": [{"spot": 90, "net_gex": 1}, {"spot": 110, "net_gex": -1}],
        }

    monkeypatch.setattr("option_envelopes.build_gamma_sweep", tied)
    result = build_option_envelopes(rows, spot=100, symbol="SPY")
    assert result["zero_gamma"] == 99

    def zero(*args, **kwargs):
        return {
            "status": "ok", "range": {"min": 90, "max": 110, "step": .25},
            "zero_crossings": {"all": [90, 91, 92]}, "skipped_contracts": {},
            "points": [{"spot": 90, "net_gex": 0}, {"spot": 110, "net_gex": 0}],
        }

    monkeypatch.setattr("option_envelopes.build_gamma_sweep", zero)
    result = build_option_envelopes(rows, spot=100, symbol="SPY")
    assert result["zero_gamma"] is None
    assert any("indeterminate" in warning for warning in result["warnings"])


def test_boundary_flags_and_no_crossing_warning_are_explicit(monkeypatch):
    rows = [option(90, "CALL", volume=100), option(100, "PUT"), option(110, "CALL")]

    def no_crossing(*args, **kwargs):
        return {
            "status": "ok", "range": {"min": 90, "max": 110, "step": .25},
            "zero_crossings": {"all": []}, "skipped_contracts": {},
            "points": [{"spot": 90, "net_gex": 1}, {"spot": 110, "net_gex": 2}],
        }

    monkeypatch.setattr("option_envelopes.build_gamma_sweep", no_crossing)
    result = build_option_envelopes(rows, spot=100, symbol="SPY")
    assert result["coverage"]["boundary_flags"]["major_gamma"] is True
    assert result["zero_gamma"] is None
    assert any("outside crossing is unknown" in warning for warning in result["warnings"])
