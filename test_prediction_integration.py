"""Real model, TCP protocol and durable storage integration; no market claims."""

from contextlib import contextmanager
from datetime import date, timedelta
import json
import math
import socket
import threading
import time

import numpy as np
import pytest

pytest.importorskip("scipy")

from prediction.data import iso_utc, parse_utc, session_bounds
from prediction.models import fit_bundle, validate_bundle
from prediction.service import ForecastServer, PredictionCoordinator
from prediction.store import PredictionStore


INSTRUMENT = "ES 12-26"
FORECAST_DAY = date(2026, 9, 30)


def _regime_bars():
    """Twenty complete prior sessions plus a held-out day with persistent states."""
    rng = np.random.default_rng(92)
    sessions = []
    day = date(2026, 9, 1)
    while day <= FORECAST_DAY:
        bounds = session_bounds(day)
        if bounds is not None:
            count = int((bounds[1] - bounds[0]) / timedelta(minutes=5))
            log_price = np.log(6000.0 + 2 * len(sessions)) * 10000
            state, previous_return = int(rng.integers(2)), 0.0
            bars = []
            for index in range(count):
                if index:
                    if rng.random() > 0.93:
                        state = 1 - state
                    previous_return = 0.12 * previous_return + rng.normal(0, (1.0, 5.0)[state])
                    log_price += previous_return
                bars.append({
                    "end_utc": iso_utc(bounds[0] + timedelta(minutes=5 * (index + 1))),
                    "close": float(np.exp(log_price / 10000)),
                })
            sessions.append(bars)
        day += timedelta(days=1)
    assert len(sessions) == 21
    return [bar for session in sessions[:-1] for bar in session], sessions[-1]


@contextmanager
def _wire_server(data_dir, clock, fit=fit_bundle):
    """Bind an actual ephemeral localhost port and clean up threads on failure."""
    with PredictionCoordinator(data_dir, clock=clock, fit=fit) as coordinator:
        with ForecastServer(coordinator, port=0) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
            thread.start()
            try:
                yield coordinator, server.server_address
            finally:
                server.shutdown()
                thread.join(timeout=5)
                assert not thread.is_alive(), "Forecast server did not shut down"


@contextmanager
def _wire_client(address):
    with socket.create_connection(address, timeout=5) as client:
        with client.makefile("rwb") as stream:
            def exchange(payload):
                stream.write((json.dumps({"schema_version": 2, **payload}, allow_nan=False) + "\n").encode("utf-8"))
                stream.flush()
                response = stream.readline()
                assert response, "Forecast server disconnected without a response"
                return json.loads(response)

            yield exchange


@contextmanager
def _wire_service(data_dir, clock, fit):
    with _wire_server(data_dir, clock, fit) as (coordinator, address):
        with _wire_client(address) as exchange:
            yield coordinator, exchange


def _hello(exchange, policy="MergeBackAdjusted", label="PropFeed"):
    response = exchange({
        "type": "HELLO", "instrument": INSTRUMENT, "symbol": "ES",
        "bar_minutes": 5, "mode": "live", "history_policy": policy,
        "feed_label": label,
    })
    assert response["status"] == "WAITING_FOR_HISTORY"
    assert response["schema_version"] == 2
    assert response["history_policy"] == policy
    assert response["feed_label"] == label


def _upload(exchange, bars):
    for offset in range(0, len(bars), 256):
        batch = bars[offset:offset + 256]
        response = exchange({"type": "BAR_BATCH", "source": "history", "bars": batch})
        assert response["status"] == "WAITING_FOR_HISTORY", response
        assert response["accepted_bars"] == len(batch)


def _wait_ready(exchange):
    deadline = time.monotonic() + 30
    while True:
        response = exchange({"type": "PING"})
        if response["status"] != "TRAINING":
            assert response["status"] == "WARMING_UP", response
            return response
        assert time.monotonic() < deadline, "Actual model fitting timed out"
        time.sleep(0.01)


def test_real_tcp_fit_forecast_restart_and_prospective_endpoint(tmp_path):
    history, today = _regime_bars()
    origin_bar = today[12]  # Twelve historical closes, then a genuinely new live close.
    clock = [parse_utc(origin_bar["end_utc"]) + timedelta(seconds=1)]
    fit_started, release_fit = threading.Event(), threading.Event()
    fit_calls = []

    def observed_real_fit(instrument, blocks, trained_through):
        # Synchronization makes the TRAINING/PING check deterministic. Both
        # numerical fits and every inference remain the production functions.
        fit_calls.append((instrument, len(blocks), trained_through))
        fit_started.set()
        if not release_fit.wait(timeout=10):
            raise RuntimeError("Integration test failed to release the real fit")
        return fit_bundle(instrument, blocks, trained_through)

    with _wire_service(tmp_path, lambda: clock[0], observed_real_fit) as (coordinator, exchange):
        _hello(exchange)
        _upload(exchange, history + today[:12])
        assert fit_calls == []
        assert coordinator.services == {}  # Uploads are staged until HISTORY_END.

        try:
            response = exchange({"type": "HISTORY_END"})
            assert response["status"] == "TRAINING", response
            service = coordinator.services[response["series_id"]]
            assert fit_started.wait(timeout=2)
            assert exchange({"type": "PING"})["status"] == "TRAINING"
        finally:
            release_fit.set()

        _wait_ready(exchange)
        assert fit_calls == [(INSTRUMENT, 20, "2026-09-29")]
        assert service.store.get_forecasts(INSTRUMENT) == []

        forecast = exchange({"type": "BAR_BATCH", "source": "live", "bars": [origin_bar]})
        assert forecast["type"] == "FORECAST", forecast
        assert forecast["status"] == "SHADOW"
        assert forecast["instrument"] == INSTRUMENT
        assert forecast["series_id"] == response["series_id"]
        assert forecast["history_policy"] == "MergeBackAdjusted"
        assert forecast["feed_label"] == "PropFeed"
        assert forecast["origin_utc"] == origin_bar["end_utc"]
        assert forecast["origin_price"] == origin_bar["close"]
        assert forecast["trained_through"] == "2026-09-29"
        assert forecast["nominal_coverage"] == 0.8
        assert forecast["calibration_status"] == "uncalibrated"
        assert 0 <= forecast["high_vol_probability"] <= 1
        for model in ("garch", "markov"):
            for horizon in (15, 30):
                levels = [forecast[f"{model}_{horizon}_{part}"] for part in ("lower", "center", "upper")]
                assert all(math.isfinite(value) and value > 0 for value in levels)
                assert levels[0] < levels[1] < levels[2]
        assert exchange({"type": "PING"}) == forecast
        assert service.store.get_forecasts(INSTRUMENT) == [forecast]
        assert service.store.report_metrics(INSTRUMENT) == []

        artifacts = list((service.data_dir / "models").glob("*.json"))
        assert len(artifacts) == 1
        artifact_path = artifacts[0]
        artifact_bytes = artifact_path.read_bytes()
        artifact_mtime = artifact_path.stat().st_mtime_ns
        saved = json.loads(artifact_bytes)
        validate_bundle(saved["bundle"], instrument=INSTRUMENT)
        assert saved["bundle"]["model_id"] == forecast["parameter_model_id"]
        assert forecast["model_id"].startswith(forecast["parameter_model_id"] + "-")
        assert saved["series"]["series_id"] == forecast["series_id"]
        assert saved["training"]["sessions"] == 20
        assert saved["forecast_session"] == FORECAST_DAY.isoformat()

    # Reconnect after a full service/store restart at the same still-fresh
    # origin. The saved artifact and issued forecast must both survive intact.
    clock[0] += timedelta(seconds=5)
    with _wire_service(tmp_path, lambda: clock[0], observed_real_fit) as (coordinator, exchange):
        _hello(exchange)
        _upload(exchange, today[:12])
        response = exchange({"type": "HISTORY_END"})
        assert response["status"] == "WARMING_UP", response
        assert response["series_id"] == forecast["series_id"]
        service = coordinator.services[response["series_id"]]
        assert service.store.get_forecasts(INSTRUMENT) == [forecast]
        replayed = exchange({"type": "BAR_BATCH", "source": "live", "bars": [origin_bar]})
        assert replayed == forecast
        assert fit_calls == [(INSTRUMENT, 20, "2026-09-29")]
        assert artifact_path.read_bytes() == artifact_bytes
        assert artifact_path.stat().st_mtime_ns == artifact_mtime
        assert service.store.get_forecasts(INSTRUMENT) == [forecast]

        endpoint = today[15]
        assert parse_utc(endpoint["end_utc"]) - parse_utc(origin_bar["end_utc"]) == timedelta(minutes=15)
        clock[0] = parse_utc(endpoint["end_utc"]) + timedelta(seconds=1)
        response = exchange({"type": "BAR_BATCH", "source": "live", "bars": [endpoint]})
        # Missing intervening bars reset inference, but the exact prospective
        # endpoint still evaluates both original 15-minute forecasts.
        assert response["status"] == "WARMING_UP"
        metrics = service.store.report_metrics(INSTRUMENT)
        assert {(row["model"], row["horizon_minutes"], row["count"]) for row in metrics} == {
            ("garch", 15, 1), ("markov", 15, 1),
        }
        assert all(row["model_id"] == forecast["model_id"] for row in metrics)
        evaluations = service.store.get_evaluations(INSTRUMENT)
        assert len(evaluations) == 2
        assert all(row["forecast_id"] == forecast["id"] and row["endpoint_utc"] == endpoint["end_utc"]
                   and row["actual_close"] == endpoint["close"] for row in evaluations)
        # Re-delivery does not duplicate or rewrite forecasts/evaluations.
        assert exchange({"type": "BAR_BATCH", "source": "live", "bars": [endpoint]})["status"] == "WARMING_UP"
        assert service.store.get_evaluations(INSTRUMENT) == evaluations
        assert service.store.report_metrics(INSTRUMENT) == metrics
        assert service.store.get_forecasts(INSTRUMENT) == [forecast]

    with PredictionStore(service.data_dir / "forecasts.sqlite3", clock=lambda: clock[0]) as reopened:
        assert reopened.get_forecasts(INSTRUMENT) == [forecast]
        assert reopened.get_evaluations(INSTRUMENT) == evaluations
        assert reopened.report_metrics(INSTRUMENT) == metrics
    assert len(fit_calls) == 1


def test_tcp_merge_policy_and_feed_label_separate_supplied_series(tmp_path):
    history, today = _regime_bars()
    clock = lambda: parse_utc(today[12]["end_utc"]) + timedelta(seconds=1)
    identities = []
    with _wire_server(tmp_path, clock) as (coordinator, address):
        profiles = [(policy, "PropFeed") for policy in (
            "DoNotMerge", "MergeBackAdjusted", "MergeNonBackAdjusted",
        )] + [("MergeBackAdjusted", "OtherPropFeed")]
        for policy, label in profiles:
            with _wire_client(address) as exchange:
                _hello(exchange, policy=policy, label=label)
                _upload(exchange, history[:1])
                response = exchange({"type": "HISTORY_END"})
                assert response["status"] == "WARMING_UP", response
                assert response["history_policy"] == policy
                assert response["feed_label"] == label
                identities.append((response["profile_id"], response["series_id"]))
                child = coordinator.services[response["series_id"]]
                assert len(child.store.get_bars(INSTRUMENT)) == 1
                assert not child.jobs and not child.models
        assert len({profile for profile, _ in identities}) == 4
        assert len({series for _, series in identities}) == 4
        assert len(coordinator.report()["series"]) == 4


def test_tcp_history_revision_refits_and_retires_old_connection_without_rewriting(tmp_path):
    history, today = _regime_bars()
    origin_bar = today[12]
    clock = [parse_utc(origin_bar["end_utc"]) + timedelta(seconds=1)]
    fit_calls = []

    def counted_real_fit(instrument, blocks, trained_through):
        fit_calls.append((instrument, len(blocks), trained_through))
        return fit_bundle(instrument, blocks, trained_through)

    with _wire_server(tmp_path, lambda: clock[0], counted_real_fit) as (coordinator, address):
        with _wire_client(address) as old_exchange:
            _hello(old_exchange)
            _upload(old_exchange, history + today[:12])
            original_response = old_exchange({"type": "HISTORY_END"})
            assert original_response["status"] == "TRAINING", original_response
            old_child = coordinator.services[original_response["series_id"]]
            _wait_ready(old_exchange)
            old_forecast = old_exchange({"type": "BAR_BATCH", "source": "live", "bars": [origin_bar]})
            assert old_forecast["type"] == "FORECAST", old_forecast
            original_bars = old_child.store.get_bars(INSTRUMENT)
            original_artifact = next((old_child.data_dir / "models").glob("*.json"))
            original_artifact_bytes = original_artifact.read_bytes()

            # A provider revision changes the historical price basis. The new
            # snapshot must get a fresh lineage and actual numerical refit.
            adjust = lambda bar: {**bar, "close": bar["close"] + 25.0}
            revised_history = [adjust(bar) for bar in history + today[:12]]
            clock[0] += timedelta(seconds=5)
            with _wire_client(address) as new_exchange:
                _hello(new_exchange)
                _upload(new_exchange, revised_history)
                assert old_exchange({"type": "PING"}) == old_forecast
                assert len(coordinator.services) == 1  # Staging does not publish.
                revision_response = new_exchange({"type": "HISTORY_END"})
                assert revision_response["status"] == "TRAINING", revision_response
                assert revision_response["series_id"] != original_response["series_id"]
                assert revision_response["profile_id"] == original_response["profile_id"]
                assert revision_response["revision"] == original_response["revision"] + 1
                new_child = coordinator.services[revision_response["series_id"]]
                assert new_child.data_dir != old_child.data_dir
                assert old_exchange({"type": "PING"})["status"] == "ERROR"
                _wait_ready(new_exchange)
                new_forecast = new_exchange({"type": "BAR_BATCH", "source": "live", "bars": [adjust(origin_bar)]})
                assert new_forecast["type"] == "FORECAST", new_forecast
                assert new_forecast["series_id"] == revision_response["series_id"]
                assert new_forecast["model_id"] != old_forecast["model_id"]
                assert len(fit_calls) == 2
                assert all(call == (INSTRUMENT, 20, "2026-09-29") for call in fit_calls)

                endpoint = today[15]
                clock[0] = parse_utc(endpoint["end_utc"]) + timedelta(seconds=1)
                rejected = old_exchange({"type": "BAR_BATCH", "source": "live", "bars": [endpoint]})
                assert rejected["status"] == "ERROR"
                accepted = new_exchange({"type": "BAR_BATCH", "source": "live", "bars": [adjust(endpoint)]})
                assert accepted["status"] == "WARMING_UP"
                assert old_child.store.get_bars(INSTRUMENT) == original_bars
                assert old_child.store.get_forecasts(INSTRUMENT) == [old_forecast]
                assert old_child.store.get_evaluations(INSTRUMENT) == []
                assert original_artifact.read_bytes() == original_artifact_bytes
                evaluations = new_child.store.get_evaluations(INSTRUMENT)
                assert len(evaluations) == 2
                assert all(row["model_id"] == new_forecast["model_id"] and row["horizon_minutes"] == 15
                           and row["actual_close"] == adjust(endpoint)["close"] for row in evaluations)
                report = {row["series_id"]: row for row in coordinator.report()["series"]}
                assert report[old_forecast["series_id"]]["is_current"] is False
                assert report[old_forecast["series_id"]]["metrics"] == []
                assert report[new_forecast["series_id"]]["is_current"] is True
                assert len(report[new_forecast["series_id"]]["metrics"]) == 2
