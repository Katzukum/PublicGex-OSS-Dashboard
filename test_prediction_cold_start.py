"""Exercise cold-start forecasting through real TCP, models and durable stores."""

from datetime import timedelta
import json
import math
import threading

import numpy as np
import pytest

pytest.importorskip("scipy")

from prediction.data import parse_utc
from prediction.models import fit_bundle
from prediction.store import PredictionStore
from test_prediction_integration import (
    INSTRUMENT, _hello, _regime_bars, _upload, _wire_client, _wire_server,
)


def _live(exchange, bar):
    return exchange({"type": "BAR_BATCH", "source": "live", "bars": [bar]})


def _assert_provisional(forecast):
    assert forecast["type"] == "FORECAST", forecast
    assert forecast["forecast_mode"] == "PROVISIONAL"
    assert forecast["model_family"] == "EWMA"
    assert forecast["available_models"] == ["ewma"]
    assert forecast["high_vol_probability"] is None
    assert forecast["high_vol_horizon_minutes"] is None
    assert forecast["trained_through"] is None
    assert forecast["training_end_utc"] is None
    assert not any(key.startswith(("garch_", "markov_")) for key in forecast)
    for horizon in (15, 30):
        levels = [forecast[f"ewma_{horizon}_{part}"] for part in ("lower", "center", "upper")]
        assert all(math.isfinite(value) and value > 0 for value in levels)
        assert levels[0] < levels[1] < levels[2]
        assert levels[1] == forecast["origin_price"]


def test_six_closes_without_prior_days_issue_and_score_durable_ewma(tmp_path):
    _, today = _regime_bars()
    clock = [parse_utc(today[5]["end_utc"]) + timedelta(seconds=1)]
    attempted = []

    def unexpected_fit(*args):
        attempted.append(args)
        raise AssertionError("Five historical closes must not trigger numerical fitting")

    with _wire_server(tmp_path, lambda: clock[0], unexpected_fit) as (coordinator, address):
        with _wire_client(address) as exchange:
            _hello(exchange)
            _upload(exchange, today[:5])
            response = exchange({"type": "HISTORY_END"})
            assert response["status"] == "WARMING_UP", response
            child = coordinator.services[response["series_id"]]
            forecast = _live(exchange, today[5])
            _assert_provisional(forecast)
            assert forecast["observed_closes"] == 6
            assert forecast["origin_utc"] == today[5]["end_utc"]
            assert forecast["series_id"] == response["series_id"]
            assert child.store.get_forecasts(INSTRUMENT) == [forecast]
            assert exchange({"type": "PING"}) == forecast
            assert attempted == []

            # Exact live endpoints score EWMA only. Missing intermediate bars
            # reset the filter and do not create extra forecasts.
            endpoint = today[8]
            clock[0] = parse_utc(endpoint["end_utc"]) + timedelta(seconds=1)
            assert _live(exchange, endpoint)["status"] == "WARMING_UP"
            metrics = child.store.report_metrics(INSTRUMENT)
            assert [(row["model"], row["horizon_minutes"], row["count"]) for row in metrics] == [("ewma", 15, 1)]
            evaluations = child.store.get_evaluations(INSTRUMENT)
            assert len(evaluations) == 1
            assert evaluations[0]["forecast_id"] == forecast["id"]
            assert evaluations[0]["actual_close"] == endpoint["close"]
            assert child.store.get_forecasts(INSTRUMENT) == [forecast]
            assert attempted == []
            database = child.data_dir / "forecasts.sqlite3"

    with PredictionStore(database, clock=lambda: clock[0]) as reopened:
        assert reopened.get_forecasts(INSTRUMENT) == [forecast]
        assert reopened.get_evaluations(INSTRUMENT) == evaluations
        assert reopened.report_metrics(INSTRUMENT) == metrics


def test_partial_prior_session_fits_while_ewma_origin_stays_immutable(tmp_path):
    history, today = _regime_bars()
    partial = history[-78:][:30]  # 29 returns; much less than 80% of one session.
    clock = [parse_utc(today[5]["end_utc"]) + timedelta(seconds=1)]
    started, release = threading.Event(), threading.Event()
    captured = []

    def gated_real_fit(instrument, blocks, trained_through):
        captured.append((instrument, [block["y"].copy() for block in blocks], trained_through))
        started.set()
        if not release.wait(timeout=10):
            raise RuntimeError("Cold-start test failed to release the real fit")
        return fit_bundle(instrument, blocks, trained_through)

    with _wire_server(tmp_path, lambda: clock[0], gated_real_fit) as (coordinator, address):
        with _wire_client(address) as exchange:
            _hello(exchange)
            _upload(exchange, partial + today[:5])
            try:
                response = exchange({"type": "HISTORY_END"})
                assert response["status"] == "TRAINING", response
                child = coordinator.services[response["series_id"]]
                assert started.wait(timeout=2)
                future = next(iter(child.jobs.values()))
                provisional = _live(exchange, today[5])
                _assert_provisional(provisional)
                assert "background" in provisional["mode_reason"]
                assert provisional["training_returns"] == 29
                assert provisional["training_sessions"] == 1
                assert exchange({"type": "PING"}) == provisional
            finally:
                release.set()
            bundle = future.result(timeout=30)
            assert bundle["diagnostics"]["garch"]["converged"]
            assert bundle["diagnostics"]["markov"]["converged"]
            # Harvesting the finished fit cannot relabel or rewrite this origin.
            assert exchange({"type": "PING"}) == provisional
            assert child.store.get_forecasts(INSTRUMENT) == [provisional]
            assert len(captured) == 1
            assert captured[0][0] == INSTRUMENT
            assert captured[0][2] == "2026-09-29"
            assert len(captured[0][1]) == 1
            np.testing.assert_allclose(captured[0][1][0], np.log([bar["close"] for bar in partial]) * 10000, rtol=0, atol=1e-10)

            clock[0] = parse_utc(today[6]["end_utc"]) + timedelta(seconds=1)
            fitted = _live(exchange, today[6])
            assert fitted["type"] == "FORECAST", fitted
            assert fitted["forecast_mode"] == "FITTED"
            assert fitted["available_models"] == ["garch", "markov"]
            assert fitted["training_end_utc"] == partial[-1]["end_utc"]
            assert parse_utc(fitted["training_end_utc"]) < parse_utc(fitted["origin_utc"])
            assert fitted["training_sessions"] == 1
            assert fitted["training_returns"] == 29
            assert 0 <= fitted["high_vol_probability"] <= 1
            assert fitted["model_id"] != provisional["model_id"]
            assert not any(key.startswith("ewma_") for key in fitted)
            assert child.store.get_forecasts(INSTRUMENT) == [fitted, provisional]
            artifact = json.loads(next((child.data_dir / "models").glob("*.json")).read_text(encoding="utf-8"))
            assert artifact["training"]["training_returns"] == 29
            assert artifact["training"]["training_end_utc"] == partial[-1]["end_utc"]


def test_failed_fit_keeps_fresh_ewma_forecasts_available(tmp_path):
    history, today = _regime_bars()
    partial = history[-78:][:30]
    clock = [parse_utc(today[5]["end_utc"]) + timedelta(seconds=1)]
    attempts = []

    def failed_fit(instrument, blocks, trained_through):
        attempts.append((instrument, sum(len(block["y"]) - 1 for block in blocks), trained_through))
        raise RuntimeError("Synthetic numerical fit failure")

    with _wire_server(tmp_path, lambda: clock[0], failed_fit) as (coordinator, address):
        with _wire_client(address) as exchange:
            _hello(exchange)
            _upload(exchange, partial + today[:5])
            response = exchange({"type": "HISTORY_END"})
            child = coordinator.services[response["series_id"]]
            future = next(iter(child.jobs.values()))
            with pytest.raises(RuntimeError, match="Synthetic numerical fit failure"):
                future.result(timeout=10)
            assert exchange({"type": "PING"})["status"] == "WARMING_UP"
            forecast = _live(exchange, today[5])
            _assert_provisional(forecast)
            assert "fit unavailable" in forecast["mode_reason"]
            clock[0] = parse_utc(today[6]["end_utc"]) + timedelta(seconds=1)
            following = _live(exchange, today[6])
            _assert_provisional(following)
            assert "fit unavailable" in following["mode_reason"]
            assert attempts == [(INSTRUMENT, 29, "2026-09-29")]
            assert child.store.get_forecasts(INSTRUMENT) == [following, forecast]


def test_today_only_real_fit_excludes_origin_from_inputs_and_saved_evidence(tmp_path):
    _, today = _regime_bars()
    # Include an unusually different origin close in the uploaded history.
    # A correct fit can use the preceding 21 returns, but not this shock.
    origin = {**today[22], "close": today[22]["close"] + 500.0}
    clock = [parse_utc(origin["end_utc"]) + timedelta(seconds=1)]
    captured = []

    def observed_real_fit(instrument, blocks, trained_through):
        captured.append((instrument, [block["y"].copy() for block in blocks], trained_through))
        return fit_bundle(instrument, blocks, trained_through)

    with _wire_server(tmp_path, lambda: clock[0], observed_real_fit) as (coordinator, address):
        with _wire_client(address) as exchange:
            _hello(exchange)
            _upload(exchange, today[:22] + [origin])
            response = exchange({"type": "HISTORY_END"})
            assert response["status"] == "TRAINING", response
            child = coordinator.services[response["series_id"]]
            next(iter(child.jobs.values())).result(timeout=30)
            assert exchange({"type": "PING"})["status"] == "WARMING_UP"
            forecast = _live(exchange, origin)
            assert forecast["type"] == "FORECAST", forecast
            assert forecast["forecast_mode"] == "FITTED"
            assert forecast["trained_through"] == "2026-09-30"
            assert forecast["training_sessions"] == 1
            assert forecast["training_returns"] == 21
            assert forecast["training_end_utc"] == today[21]["end_utc"]
            assert parse_utc(forecast["training_end_utc"]) < parse_utc(forecast["origin_utc"])
            assert len(captured) == 1
            assert captured[0][0] == INSTRUMENT
            assert captured[0][2] == "2026-09-30"
            assert len(captured[0][1]) == 1
            np.testing.assert_allclose(captured[0][1][0], np.log([bar["close"] for bar in today[:22]]) * 10000, rtol=0, atol=1e-10)
            artifact = json.loads(next((child.data_dir / "models").glob("*.json")).read_text(encoding="utf-8"))
            assert artifact["training"]["training_end_utc"] == today[21]["end_utc"]
            assert artifact["training"]["training_points"] == 22
            assert artifact["training"]["training_returns"] == 21
            assert child.store.get_forecasts(INSTRUMENT) == [forecast]
