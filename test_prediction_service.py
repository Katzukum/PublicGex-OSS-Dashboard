"""Behavioral tests for live chronology, contract isolation and the TCP bridge."""
from datetime import date, datetime, timedelta, timezone
import json
import socket
import threading
import time

import pytest

pytest.importorskip("scipy")
from prediction.data import (iso_utc, live_closes, normalize_bars, session_bounds,
                             training_data, validate_instrument)
from prediction.service import ForecastServer, PredictionCoordinator, PredictionService

UTC = timezone.utc
INSTRUMENT = "ES 12-26"


def bars_for(day, count=78, price=6000.0):
    opening, _ = session_bounds(day)
    return [{"end_utc": iso_utc(opening + timedelta(minutes=5 * (i + 1))),
             "close": price + (i % 7) * .25} for i in range(count)]


def historical_bars():
    rows = []
    current = date(2026, 9, 1)
    while current < date(2026, 9, 30):
        if session_bounds(current):
            rows.extend(bars_for(current))
        current += timedelta(days=1)
    return rows


def seed_history(service):
    rows = historical_bars()
    for start in range(0, len(rows), 256):
        service.ingest(INSTRUMENT, rows[start:start + 256], "history")


@pytest.fixture
def service(tmp_path, monkeypatch):
    clock = [datetime(2026, 9, 30, 14, 30, 1, tzinfo=UTC)]
    calls = []

    def fit(instrument, blocks, trained_through):
        calls.append((instrument, blocks, trained_through))
        return {"instrument": instrument, "model_id": "fixture-v1", "trained_through": trained_through}

    def predict(bundle, closes):
        result = {"model_id": bundle["model_id"], "nominal_coverage": .8, "high_vol_probability": .25}
        for model in ("garch", "markov"):
            for horizon in (15, 30):
                result.update({f"{model}_{horizon}_lower": closes[-1] - horizon,
                               f"{model}_{horizon}_center": closes[-1],
                               f"{model}_{horizon}_upper": closes[-1] + horizon})
        return result

    monkeypatch.setattr("prediction.service.validate_bundle", lambda *a, **kw: None)
    with PredictionService(tmp_path, clock=lambda: clock[0], fit=fit, predict=predict) as instance:
        instance.test_clock, instance.fit_calls = clock, calls
        yield instance


def wait_for_fit(service):
    for _ in range(200):
        response = service.poll(INSTRUMENT)
        if response["status"] != "TRAINING":
            return response
        time.sleep(.005)
    pytest.fail("Test fit failed to finish")


def test_data_boundaries_timezone_calendar_and_contracts():
    assert session_bounds(date(2026, 11, 26)) is None
    assert session_bounds(date(2026, 11, 27))[1].hour == 18  # 13:00 EST
    assert session_bounds(date(2027, 11, 26))[1].hour == 18
    assert session_bounds(date(2026, 9, 30))[0].hour == 13  # 09:30 EDT
    with pytest.raises(ValueError, match="2025"):
        session_bounds(date(2028, 1, 3))
    for value in ("SPX", "ES", "ES ##-##", "NQ 10-26"):
        with pytest.raises(ValueError):
            validate_instrument(value)
    with pytest.raises(ValueError):
        validate_instrument("ES 12-26", "NQ")
    now = datetime(2026, 9, 30, 14, 30, 1, tzinfo=UTC)
    assert len(normalize_bars(bars_for(date(2026, 9, 30), 12), now)) == 12
    for invalid in ({"end_utc": "2026-09-30T14:30:00", "close": 6000},
                    {"end_utc": "2026-09-30T14:35:00Z", "close": 6000},
                    {"end_utc": "2026-09-30T14:29:00Z", "close": 6000},
                    {"end_utc": "2026-09-30T14:30:00Z", "close": float("nan")}):
        with pytest.raises(ValueError):
            normalize_bars([invalid], now)


def test_training_excludes_current_session_and_gaps_reset():
    prior = historical_bars()
    today = bars_for(date(2026, 9, 30))
    blocks, evidence = training_data(prior + today, date(2026, 9, 30))
    assert evidence["trained_through"] == "2026-09-29"
    assert evidence["sessions"] == 20
    assert sum(len(b["y"]) for b in blocks) == 20 * 78
    assert evidence["training_returns"] == 20 * 77
    assert len(live_closes(today[:6], today[5]["end_utc"])) == 6
    assert live_closes(today[:5], today[4]["end_utc"]) == []
    assert live_closes(today[:8] + today[9:12], today[11]["end_utc"]) == []
    assert live_closes(prior + today[:2], today[1]["end_utc"]) == []


def test_history_cannot_issue_forecast_and_other_contract_does_not_help(service):
    seed_history(service)
    service.ingest(INSTRUMENT, bars_for(date(2026, 9, 30), 12), "history")
    assert wait_for_fit(service)["status"] == "WARMING_UP"
    assert service.store.get_forecasts(INSTRUMENT) == []
    assert service.poll("ES 03-27")["status"] == "WARMING_UP"
    assert len(service.fit_calls) == 1
    assert service.fit_calls[0][2] == "2026-09-29"


def test_live_fixed_origin_expiry_and_prospective_scoring(service):
    seed_history(service)
    today = bars_for(date(2026, 9, 30))
    service.ingest(INSTRUMENT, today[:12], "history")
    wait_for_fit(service)
    service.ingest(INSTRUMENT, [today[11]], "live")
    forecast = service.poll(INSTRUMENT)
    assert forecast["type"] == "FORECAST"
    assert forecast["origin_price"] == today[11]["close"]
    assert len(service.store.get_forecasts(INSTRUMENT)) == 1
    assert service.poll(INSTRUMENT) == forecast
    # Losing the next bar makes the display stale, not a recentered prediction.
    service.test_clock[0] += timedelta(seconds=391)
    assert service.poll(INSTRUMENT)["status"] == "STALE"
    assert len(service.store.get_forecasts(INSTRUMENT)) == 1
    # An exact future endpoint can be scored when genuinely received live.
    service.test_clock[0] = datetime(2026, 9, 30, 14, 45, 1, tzinfo=UTC)
    service.ingest(INSTRUMENT, [today[14]], "live")
    metrics = service.store.report_metrics(INSTRUMENT)
    assert len(metrics) == 2
    assert all(row["count"] == 1 for row in metrics)
    # The missing intermediate observations prevent a new forecast.
    assert service.poll(INSTRUMENT)["status"] == "WARMING_UP"


def test_late_replay_is_rejected_but_horizons_can_cross_cash_close(service):
    service.ingest(INSTRUMENT, bars_for(date(2026, 9, 30), 1), "history")
    with pytest.raises(ValueError, match="90 seconds"):
        service.ingest(INSTRUMENT, bars_for(date(2026, 9, 30), 1), "live")
    seed_history(service)
    wait_for_fit(service)
    service.test_clock[0] = datetime(2026, 9, 30, 19, 35, 1, tzinfo=UTC)
    service.ingest(INSTRUMENT, bars_for(date(2026, 9, 30), 73), "history")
    service.ingest(INSTRUMENT, [bars_for(date(2026, 9, 30), 73)[-1]], "live")
    forecast = service.poll(INSTRUMENT)
    assert forecast["type"] == "FORECAST"
    assert forecast["session_scope"] == "ALL_HOURS"
    assert forecast["session_kind"] == "EXTENDED"


def test_tcp_protocol_actual_socket_and_history_commit(service):
    with PredictionCoordinator(service.data_dir / "coordinator", clock=service.clock,
                               fit=service.fit, predict=service.predict) as coordinator, ForecastServer(coordinator, 0) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client:
                stream = client.makefile("rwb")

                def exchange(payload):
                    stream.write((json.dumps({"schema_version": 2, **payload}) + "\n").encode())
                    stream.flush()
                    return json.loads(stream.readline())

                assert exchange({"type": "PING"})["status"] == "ERROR"
                assert exchange({"type": "HELLO", "instrument": INSTRUMENT, "symbol": "ES", "bar_minutes": 5, "mode": "live", "history_policy": "MergeBackAdjusted", "feed_label": "PropFeed"})["status"] == "WAITING_FOR_HISTORY"
                assert exchange({"type": "BAR_BATCH", "source": "live", "bars": [bars_for(date(2026, 9, 30), 12)[-1]]})["status"] == "ERROR"
                assert exchange({"type": "BAR_BATCH", "source": "history", "bars": bars_for(date(2026, 9, 29))})["accepted_bars"] == 78
                assert coordinator.registry.list_series() == []  # Uploads have no side effects before commit.
                committed = exchange({"type": "HISTORY_END"})
                assert committed["status"] in ("TRAINING", "WARMING_UP")
                child = coordinator.services[committed["series_id"]]
                assert child.training_info[(INSTRUMENT, date(2026, 9, 30))]["sessions"] == 1
                assert committed["series_id"]
                assert committed["history_policy"] == "MergeBackAdjusted"
                assert committed["feed_label"] == "PropFeed"
                assert exchange({"type": "BAR_BATCH", "source": "history", "bars": bars_for(date(2026, 9, 28))})["status"] == "ERROR"
                stream.close()
        finally:
            server.shutdown()
            thread.join(2)


def test_close_rejects_further_work_cleanly(service):
    service.close()
    with pytest.raises(RuntimeError, match="stopping"):
        service.poll(INSTRUMENT)
    with pytest.raises(RuntimeError, match="stopping"):
        service.ingest(INSTRUMENT, bars_for(date(2026, 9, 29)), "history")


def test_failed_staged_history_does_not_create_a_series(service):
    with PredictionCoordinator(service.data_dir / "coordinator", clock=service.clock) as coordinator, ForecastServer(coordinator, 0) as server:
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        try:
            with socket.create_connection(server.server_address, timeout=2) as client, client.makefile("rwb") as stream:
                def exchange(payload):
                    stream.write((json.dumps({"schema_version": 2, **payload}) + "\n").encode())
                    stream.flush()
                    return json.loads(stream.readline())
                hello = {"type": "HELLO", "instrument": INSTRUMENT, "symbol": "ES", "bar_minutes": 5,
                         "mode": "live", "history_policy": "MergeNonBackAdjusted", "feed_label": "PropFeed"}
                assert exchange({**hello, "schema_version": 1})["status"] == "ERROR"
                assert exchange(hello)["status"] == "WAITING_FOR_HISTORY"
                original = bars_for(date(2026, 9, 29), 1)
                assert exchange({"type": "BAR_BATCH", "source": "history", "bars": original})["status"] == "WAITING_FOR_HISTORY"
                changed = [{**original[0], "close": original[0]["close"] + 5}]
                assert exchange({"type": "BAR_BATCH", "source": "history", "bars": changed})["status"] == "ERROR"
                assert exchange({"type": "HISTORY_END"})["status"] == "ERROR"
                assert coordinator.registry.list_series() == []
        finally:
            server.shutdown()
            thread.join(2)
