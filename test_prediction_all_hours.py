"""All-hours forecasts still require genuine fresh bars and causal history."""
from datetime import timedelta
import json
import threading

import pytest

pytest.importorskip("scipy")

from prediction.data import iso_utc, parse_utc
from prediction.models import fit_bundle
from prediction.service import TRAINING_POLICY
from test_prediction_cold_start import _assert_provisional, _live
from test_prediction_integration import INSTRUMENT, _hello, _regime_bars, _upload, _wire_client, _wire_server


def bars_ending(end, count=6):
    origin = parse_utc(end)
    return [{"end_utc": iso_utc(origin - timedelta(minutes=5 * (count - 1 - index))),
             "close": 6000 + (index % 3) * .5 + index * .1}
            for index in range(count)]


@pytest.mark.parametrize("origin,kind", [
    ("2026-09-30T23:30:00Z", "EXTENDED"),  # evening
    ("2026-09-30T12:00:00Z", "EXTENDED"),  # premarket
    ("2026-09-30T19:35:00Z", "EXTENDED"),  # horizon crosses cash close
    ("2026-09-30T20:00:00Z", "EXTENDED"),  # cash close
    ("2026-10-01T04:00:00Z", "EXTENDED"),  # New York midnight
    ("2026-10-04T22:30:00Z", "EXTENDED"),  # Sunday
    ("2026-11-26T14:30:00Z", "EXTENDED"),  # cash holiday
    ("2028-01-04T23:00:00Z", "EXTENDED"),  # beyond cash-label calendar
    ("2026-09-30T14:30:00Z", "CASH"),
])
def test_real_tcp_accepts_all_hours_and_scores_only_live_endpoints(tmp_path, origin, kind):
    bars = bars_ending(origin)
    clock = [parse_utc(origin) + timedelta(seconds=1)]

    def unexpected_fit(*args):
        pytest.fail("Six closes must not trigger fitting")

    with _wire_server(tmp_path, lambda: clock[0], unexpected_fit) as (coordinator, address):
        with _wire_client(address) as exchange:
            _hello(exchange)
            _upload(exchange, bars[:-1])
            committed = exchange({"type": "HISTORY_END"})
            child = coordinator.services[committed["series_id"]]
            assert child.store.get_forecasts(INSTRUMENT) == []
            forecast = _live(exchange, bars[-1])
            _assert_provisional(forecast)
            assert forecast["session_scope"] == "ALL_HOURS"
            assert forecast["session_kind"] == kind
            assert forecast["training_policy"] == TRAINING_POLICY
            assert forecast["observed_closes"] == 6
            assert child.store.get_forecasts(INSTRUMENT) == [forecast]
            assert exchange({"type": "PING"}) == forecast

            clock[0] = parse_utc(origin) + timedelta(seconds=391)
            assert exchange({"type": "PING"})["status"] == "STALE"
            endpoint = {"end_utc": iso_utc(parse_utc(origin) + timedelta(minutes=15)), "close": 6002.0}
            clock[0] = parse_utc(endpoint["end_utc"]) + timedelta(seconds=1)
            # A market/feed gap requires another six observations to forecast.
            assert _live(exchange, endpoint)["status"] == "WARMING_UP"
            metrics = child.store.report_metrics(INSTRUMENT)
            assert len(metrics) == 1
            assert metrics[0]["session_kind"] == kind
            assert (metrics[0]["model"], metrics[0]["horizon_minutes"], metrics[0]["count"]) == ("ewma", 15, 1)


def test_midnight_keeps_current_forecast_and_contiguous_filter(tmp_path):
    bars = bars_ending("2026-10-01T03:55:00Z")  # 23:55 New York
    clock = [parse_utc(bars[-1]["end_utc"]) + timedelta(seconds=1)]
    with _wire_server(tmp_path, lambda: clock[0]) as (_, address):
        with _wire_client(address) as exchange:
            _hello(exchange)
            _upload(exchange, bars[:-1])
            exchange({"type": "HISTORY_END"})
            first = _live(exchange, bars[-1])
            _assert_provisional(first)
            clock[0] = parse_utc("2026-10-01T04:00:01Z")
            assert exchange({"type": "PING"}) == first
            second = _live(exchange, {"end_utc": "2026-10-01T04:00:00Z", "close": 6001.5})
            _assert_provisional(second)
            assert second["observed_closes"] == 7
            assert second["session_kind"] == "EXTENDED"
            assert second["origin_utc"] != first["origin_utc"]


def test_real_overnight_fit_promotes_without_rewriting_provisional_origin(tmp_path):
    _, cash_bars = _regime_bars()
    bars = [{**bar, "end_utc": iso_utc(parse_utc(bar["end_utc"]) + timedelta(hours=8))}
            for bar in cash_bars[:24]]
    clock = [parse_utc(bars[22]["end_utc"]) + timedelta(seconds=1)]
    release = threading.Event()
    captured = []

    def gated_fit(instrument, blocks, trained_through):
        captured.append(sum(len(block["y"]) - 1 for block in blocks))
        if not release.wait(timeout=10):
            raise RuntimeError("Test did not release fitting")
        return fit_bundle(instrument, blocks, trained_through)

    with _wire_server(tmp_path, lambda: clock[0], gated_fit) as (coordinator, address):
        with _wire_client(address) as exchange:
            _hello(exchange)
            _upload(exchange, bars[:22])
            try:
                committed = exchange({"type": "HISTORY_END"})
                child = coordinator.services[committed["series_id"]]
                future = next(iter(child.jobs.values()))
                first = _live(exchange, bars[22])
                _assert_provisional(first)
                assert first["session_kind"] == "EXTENDED"
            finally:
                release.set()
            future.result(timeout=30)
            assert captured == [21]
            assert exchange({"type": "PING"}) == first
            clock[0] = parse_utc(bars[23]["end_utc"]) + timedelta(seconds=1)
            fitted = _live(exchange, bars[23])
            assert fitted["forecast_mode"] == "FITTED"
            assert fitted["session_kind"] == "EXTENDED"
            assert fitted["training_end_utc"] == bars[21]["end_utc"]
            assert parse_utc(fitted["training_end_utc"]) < parse_utc(fitted["origin_utc"])
            artifact = next((child.data_dir / "models").glob("*.json"))
            assert TRAINING_POLICY in artifact.name
            saved = json.loads(artifact.read_text())
            assert saved["training_policy"] == TRAINING_POLICY
            assert child.store.get_forecasts(INSTRUMENT)[-1] == first
