"""Native NinjaTrader month-name contracts survive the complete wire path."""
from datetime import timedelta
import json

import pytest

pytest.importorskip("scipy")

from prediction.data import parse_utc
from test_prediction_cold_start import _assert_provisional
from test_prediction_integration import _regime_bars, _upload, _wait_ready, _wire_client, _wire_server


def hello(exchange, instrument, symbol):
    return exchange({"type": "HELLO", "instrument": instrument, "symbol": symbol,
                     "bar_minutes": 5, "mode": "live", "history_policy": "MergeBackAdjusted",
                     "feed_label": "NativeContractTest"})


@pytest.mark.parametrize("symbol", ["ES", "MES", "NQ", "MNQ"])
def test_native_contract_hello_history_live_forecast_preserve_exact_label(tmp_path, symbol):
    instrument = symbol + " DEC26"
    _, bars = _regime_bars()
    clock = [parse_utc(bars[5]["end_utc"]) + timedelta(seconds=1)]
    with _wire_server(tmp_path, lambda: clock[0]) as (coordinator, address):
        with _wire_client(address) as exchange:
            accepted = hello(exchange, instrument, symbol)
            assert accepted["status"] == "WAITING_FOR_HISTORY", accepted
            assert accepted["instrument"] == instrument
            _upload(exchange, bars[:5])
            committed = exchange({"type": "HISTORY_END"})
            assert committed["instrument"] == instrument
            child = coordinator.services[committed["series_id"]]
            forecast = exchange({"type": "BAR_BATCH", "source": "live", "bars": [bars[5]]})
            _assert_provisional(forecast)
            assert forecast["instrument"] == instrument
            assert child.store.get_forecasts(instrument) == [forecast]
            assert child.store.get_forecasts(symbol + " 12-26") == []
            assert {row["instrument"] for row in child.store.get_bars(instrument)} == {instrument}


def test_named_contract_fitted_artifact_restart_and_numeric_alias_isolation(tmp_path):
    instrument = "NQ DEC26"
    history, bars = _regime_bars()
    history = history[-78:][:30]
    clock = [parse_utc(bars[5]["end_utc"]) + timedelta(seconds=1)]
    artifact_bytes = None
    forecast = None
    for restarting in (False, True):
        with _wire_server(tmp_path, lambda: clock[0]) as (coordinator, address):
            with _wire_client(address) as exchange:
                assert hello(exchange, instrument, "NQ")["status"] == "WAITING_FOR_HISTORY"
                _upload(exchange, history + bars[:5])
                committed = exchange({"type": "HISTORY_END"})
                child = coordinator.services[committed["series_id"]]
                _wait_ready(exchange)
                current = exchange({"type": "BAR_BATCH", "source": "live", "bars": [bars[5]]})
                assert current["forecast_mode"] == "FITTED", current
                assert current["instrument"] == instrument
                artifact = next((child.data_dir / "models").glob("*.json"))
                saved = json.loads(artifact.read_text())
                assert saved["bundle"]["instrument"] == instrument
                if restarting:
                    assert current == forecast
                    assert artifact.read_bytes() == artifact_bytes
                else:
                    forecast, artifact_bytes = current, artifact.read_bytes()

            # A different spelling stays a different explicitly supplied series.
            with _wire_client(address) as alternate:
                assert hello(alternate, "NQ 12-26", "NQ")["status"] == "WAITING_FOR_HISTORY"
                _upload(alternate, bars[:5])
                numeric = alternate({"type": "HISTORY_END"})
                assert numeric["series_id"] != forecast["series_id"]
                assert numeric["instrument"] == "NQ 12-26"
        clock[0] += timedelta(seconds=1)


def test_rejected_hello_does_not_bind_contract_or_allow_history_commit(tmp_path):
    _, bars = _regime_bars()
    clock = lambda: parse_utc(bars[5]["end_utc"]) + timedelta(seconds=1)
    with _wire_server(tmp_path, clock) as (coordinator, address):
        with _wire_client(address) as exchange:
            error = hello(exchange, "NQ DEC26", "MNQ")
            assert error["status"] == "ERROR"
            assert error["instrument"] is None
            assert exchange({"type": "HISTORY_END"})["status"] == "ERROR"
            assert coordinator.services == {}
            assert coordinator.registry.list_series() == []
