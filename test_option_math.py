from datetime import datetime, time

from backtest_gamma_butterflies import _charm_exposure, _d1_total_vol
from option_math import charm_exposure, infer_total_vol


def test_runtime_and_backtest_option_math_match():
    row = {
        "option_type": "CALL",
        "delta": 0.55,
        "gamma": 0.02,
        "underlying_price": 100,
        "open_interest": 25,
        "expiration_date": "2026-08-14",
    }
    implied, reason = infer_total_vol(row, 100)
    assert reason is None
    assert _d1_total_vol(row, 100, "CALL") == (implied["d1"], implied["total_vol"])
    timestamp = datetime(2026, 8, 14, 14, 30)
    assert _charm_exposure(row, 100, timestamp, time(16, 0)) == charm_exposure(row, 100, timestamp, time(16, 0))


def test_invalid_contract_reports_unmatched_math():
    implied, reason = infer_total_vol({"option_type": "CALL", "delta": 0.5, "gamma": 0}, 100)
    assert implied is None
    assert reason == "invalid_greeks"
