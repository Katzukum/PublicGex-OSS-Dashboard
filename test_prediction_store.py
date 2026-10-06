from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from prediction.store import PredictionStore


UTC = timezone.utc
ORIGIN = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)


def _payload(**changes):
    result = {"instrument": "ES 12-26", "origin_utc": ORIGIN.isoformat(),
              "generated_at_utc": (ORIGIN + timedelta(seconds=2)).isoformat(),
              "valid_until_utc": (ORIGIN + timedelta(minutes=5)).isoformat(),
              "origin_price": 6000., "model_id": "model-v1", "status": "SHADOW", "nominal_coverage": .8}
    for model in ("garch", "markov"):
        for horizon in (15, 30):
            result.update({f"{model}_{horizon}_lower": 5990., f"{model}_{horizon}_center": 6000., f"{model}_{horizon}_upper": 6010.})
    result.update(changes)
    return result


def _bar(minutes, close=6005.):
    return {"end_utc": ORIGIN + timedelta(minutes=minutes), "close": close}


def _provisional(**changes):
    payload = {key: value for key, value in _payload().items()
               if not key.startswith(("garch_", "markov_"))}
    payload.update(forecast_mode="PROVISIONAL", model_family="EWMA",
                   available_models=["ewma"], high_vol_probability=None, model_id="ewma-v1")
    for horizon in (15, 30):
        payload.update({f"ewma_{horizon}_lower": 5990., f"ewma_{horizon}_center": 6000.,
                        f"ewma_{horizon}_upper": 6010.})
    payload.update(changes)
    return payload


def _store(tmp_path, created=None):
    return PredictionStore(tmp_path / "predictions.db", clock=lambda: created or ORIGIN + timedelta(seconds=3))


def test_bar_idempotency_whole_batch_conflict_rollback_and_expiry_separation(tmp_path):
    with _store(tmp_path) as store:
        received = ORIGIN + timedelta(hours=1)
        assert store.upsert_bars("ES 12-26", [_bar(0)], "history", received)["inserted"] == 1
        assert store.upsert_bars("ES 12-26", [_bar(0), _bar(0)], "history", received)["duplicates"] == 2
        with pytest.raises(ValueError, match="Conflicting stored"):
            store.upsert_bars("ES 12-26", [_bar(-5), _bar(0, 1.)], "history", received)
        assert len(store.get_bars("ES 12-26")) == 1
        with pytest.raises(ValueError, match="in batch"):
            store.upsert_bars("ES 12-26", [_bar(5), _bar(5, 1.)], "history", received)
        assert len(store.get_bars("ES 12-26")) == 1
        store.upsert_bars("ES 03-27", [_bar(0, 6100.)], "history", received)
        assert store.get_bars("ES 03-27")[0]["close"] == 6100.
        assert store.get_bars("ES 12-26")[0]["close"] == 6005.


def test_forecast_exact_replay_and_database_level_immutability(tmp_path):
    with _store(tmp_path) as store:
        first = store.log_forecast(_payload())
        assert store.log_forecast(_payload()) == first
        with pytest.raises(ValueError, match="immutable"):
            store.log_forecast(_payload(garch_15_upper=6020.))
        with sqlite3.connect(store.path) as con:
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                con.execute("UPDATE forecasts SET model_id='revised'")
            with pytest.raises(sqlite3.IntegrityError, match="immutable"):
                con.execute("DELETE FROM forecasts")
        assert store.status()["forecasts"] == 1


def test_exact_horizon_scoring_no_substitute_or_overnight_and_repeat_idempotency(tmp_path):
    with _store(tmp_path) as store:
        store.log_forecast(_payload())
        store.upsert_bars("ES 12-26", [_bar(16), _bar(24 * 60)], "live", ORIGIN + timedelta(days=1, seconds=1))
        assert store.evaluate() == []
        store.upsert_bars("ES 03-27", [_bar(15)], "live", ORIGIN + timedelta(minutes=15, seconds=1))
        assert store.evaluate() == []
        store.upsert_bars("ES 12-26", [_bar(15), _bar(30, 6020.)], "live", ORIGIN + timedelta(minutes=30, seconds=1))
        rows = store.evaluate()
        assert len(rows) == 4
        assert {r["horizon_minutes"] for r in rows} == {15, 30}
        assert [r["actual_close"] for r in rows if r["horizon_minutes"] == 15] == [6005., 6005.]
        thirty = [r for r in rows if r["horizon_minutes"] == 30]
        assert all(r["covered"] == 0 and r["error"] == 20 for r in thirty)
        assert all(r["interval_score"] == pytest.approx(120) for r in thirty)
        assert store.evaluate() == []
        metrics = store.report_metrics("ES 12-26")
        assert len(metrics) == 4
        assert all(r["count"] == 1 for r in metrics)
        assert all(r["mean_width"] == 20 for r in metrics)
        assert {r["coverage"] for r in metrics} == {0., 1.}
        assert {r["session_kind"] for r in metrics} == {"LEGACY"}


def test_history_does_not_score_and_live_upgrade_preserves_first_live_receipt(tmp_path):
    with _store(tmp_path) as store:
        store.log_forecast(_payload())
        store.upsert_bars("ES 12-26", [_bar(15)], "history", ORIGIN + timedelta(minutes=15, seconds=1))
        assert store.evaluate() == []
        first_receipt = ORIGIN + timedelta(minutes=15, seconds=2)
        assert store.upsert_bars("ES 12-26", [_bar(15)], "live", first_receipt)["upgraded"] == 1
        first = store.get_bars("ES 12-26")[0]
        store.upsert_bars("ES 12-26", [_bar(15)], "live", ORIGIN + timedelta(minutes=40))
        store.upsert_bars("ES 12-26", [_bar(15)], "history", ORIGIN + timedelta(minutes=50))
        assert store.get_bars("ES 12-26")[0] == first
        assert len(store.evaluate()) == 2


def test_backdated_forecast_cannot_score_already_elapsed_market_endpoints(tmp_path):
    with _store(tmp_path, created=ORIGIN + timedelta(hours=1)) as store:
        store.log_forecast(_payload())
        # Even first live receipt after logging cannot legitimize a retroactive origin.
        store.upsert_bars("ES 12-26", [_bar(15), _bar(30)], "live", ORIGIN + timedelta(hours=2))
        assert store.evaluate() == []
        assert store.status()["realized_evaluations"] == 0


def test_future_and_naive_timestamps_rejected_without_partial_writes(tmp_path):
    with _store(tmp_path) as store:
        with pytest.raises(ValueError, match="market end"):
            store.upsert_bars("ES 12-26", [_bar(0), _bar(15)], "live", ORIGIN)
        with pytest.raises(ValueError, match="timezone"):
            store.upsert_bars("ES 12-26", [{"end_utc": "2026-09-30 14:00", "close": 6000}], "history", ORIGIN)
        assert store.get_bars("ES 12-26") == []


def test_threaded_writes_are_idempotent_and_survive_reopen(tmp_path):
    with _store(tmp_path) as store:
        def insert(_):
            return store.upsert_bars("ES 12-26", [_bar(0)], "live", ORIGIN + timedelta(seconds=1))
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(insert, range(20)))
        assert sum(r["inserted"] for r in results) == 1
        assert sum(r["duplicates"] for r in results) == 19
    with _store(tmp_path) as reopened:
        assert len(reopened.get_bars("ES 12-26")) == 1


def test_refuses_unrelated_application_database(tmp_path):
    path = tmp_path / "application.db"
    with sqlite3.connect(path) as con:
        con.execute("CREATE TABLE gex_snapshots (id INTEGER)")
    original = path.read_bytes()
    with pytest.raises(ValueError, match="own database"):
        PredictionStore(path)
    assert path.read_bytes() == original


def test_provisional_forecast_persists_replays_and_remains_immutable(tmp_path):
    with _store(tmp_path) as store:
        original = store.log_forecast(_provisional())
        assert original["status"] == "SHADOW"
        assert original["forecast_mode"] == "PROVISIONAL"
        assert original["model_family"] == "EWMA"
        assert original["available_models"] == ["ewma"]
        assert original["high_vol_probability"] is None
        assert not any(key.startswith(("garch_", "markov_")) for key in original)
        assert store.log_forecast(_provisional()) == original
        with pytest.raises(ValueError, match="immutable"):
            store.log_forecast(_provisional(ewma_15_upper=6020.))
    with _store(tmp_path) as reopened:
        assert reopened.get_forecasts() == [original]
        assert reopened.log_forecast(_provisional()) == original


def test_provisional_scores_only_ewma_at_exact_live_endpoints_separate_from_fitted(tmp_path):
    with _store(tmp_path) as store:
        provisional = store.log_forecast(_provisional())
        fitted = store.log_forecast(_payload(forecast_mode="FITTED", model_family="GARCH_MARKOV",
                                             available_models=["garch", "markov"], high_vol_probability=.25))
        store.upsert_bars("ES 12-26", [_bar(16)], "live", ORIGIN + timedelta(minutes=16, seconds=1))
        assert store.evaluate() == []
        store.upsert_bars("ES 12-26", [_bar(15)], "history", ORIGIN + timedelta(minutes=16, seconds=1))
        assert store.evaluate() == []
        store.upsert_bars("ES 12-26", [_bar(15), _bar(30, 6020.)], "live", ORIGIN + timedelta(minutes=30, seconds=1))
        rows = store.evaluate()
        ewma = [row for row in rows if row["forecast_id"] == provisional["id"]]
        assert len(ewma) == 2
        assert {(row["model"], row["horizon_minutes"]) for row in ewma} == {("ewma", 15), ("ewma", 30)}
        assert next(row for row in ewma if row["horizon_minutes"] == 30)["interval_score"] == pytest.approx(120.)
        assert len([row for row in rows if row["forecast_id"] == fitted["id"]]) == 4
        assert store.evaluate() == []
        metrics = store.report_metrics()
        assert len(metrics) == 6
        assert {row["model"] for row in metrics} == {"ewma", "garch", "markov"}
        assert all(row["count"] == 1 for row in metrics)
        assert {row["model_id"] for row in metrics if row["model"] == "ewma"} == {"ewma-v1"}


def test_provisional_retroactive_logging_still_cannot_score_live_backfill(tmp_path):
    with _store(tmp_path, created=ORIGIN + timedelta(hours=1)) as store:
        store.log_forecast(_provisional())
        store.upsert_bars("ES 12-26", [_bar(15), _bar(30)], "live", ORIGIN + timedelta(hours=2))
        assert store.evaluate() == []


@pytest.mark.parametrize("changes", [
    {"forecast_mode": "FITTED"},
    {"model_family": "GARCH_MARKOV"},
    {"available_models": ["garch", "markov"]},
    {"available_models": ["ewma", "ewma"]},
    {"available_models": "ewma"},
    {"forecast_mode": "OTHER"},
    {"high_vol_probability": .5},
    {"garch_15_lower": 5990.},
    {"markov_30_center": 6000.},
])
def test_provisional_cannot_masquerade_as_fitted_models_or_markov_state(tmp_path, changes):
    with _store(tmp_path) as store:
        with pytest.raises(ValueError):
            store.log_forecast(_provisional(**changes))
        assert store.get_forecasts() == []


def test_missing_or_partial_metadata_rejected_and_legacy_payload_unchanged(tmp_path):
    with _store(tmp_path) as store:
        incomplete = _provisional()
        del incomplete["high_vol_probability"]
        with pytest.raises(ValueError, match="no Markov state"):
            store.log_forecast(incomplete)
        for metadata in ({"forecast_mode": "FITTED"}, {"available_models": ["garch", "markov"]},
                         {"model_family": "GARCH_MARKOV"}):
            with pytest.raises(ValueError, match="declared together"):
                store.log_forecast(_payload(**metadata))
        with pytest.raises(ValueError, match="unavailable ewma"):
            store.log_forecast(_payload(ewma_15_lower=5990.))
        legacy = store.log_forecast(_payload())
        assert all(key not in legacy for key in ("forecast_mode", "model_family", "available_models"))
        assert store.log_forecast(_payload()) == legacy


@pytest.mark.parametrize("payload_factory,models", [
    (_payload, {"garch", "markov"}),
    (_provisional, {"ewma"}),
])
def test_reports_separate_cash_extended_and_legacy_for_same_model_version(tmp_path, payload_factory, models):
    clock = [ORIGIN]
    path = tmp_path / "session-metrics.db"
    with PredictionStore(path, clock=lambda: clock[0]) as store:
        for offset_hours, session_kind, actual_close in (
            (0, "CASH", 6005.),
            (1, "CASH", 6020.),
            (2, None, 6001.),
            (9, "EXTENDED", 6030.),
        ):
            origin = ORIGIN + timedelta(hours=offset_hours)
            clock[0] = origin + timedelta(seconds=3)
            payload = payload_factory(
                origin_utc=origin.isoformat(),
                generated_at_utc=(origin + timedelta(seconds=2)).isoformat(),
                valid_until_utc=(origin + timedelta(minutes=5)).isoformat(),
            )
            if session_kind is not None:
                payload.update(session_scope="ALL_HOURS", session_kind=session_kind)
            store.log_forecast(payload)
            clock[0] = origin + timedelta(minutes=30, seconds=1)
            store.upsert_bars("ES 12-26", [
                {"end_utc": origin + timedelta(minutes=minutes), "close": actual_close}
                for minutes in (15, 30)
            ], "live", clock[0])
            assert len(store.evaluate()) == len(models) * 2

        original_forecasts = store.get_forecasts()
        original_evaluations = store.get_evaluations()
        metrics = store.report_metrics("ES 12-26")
        assert len(metrics) == len(models) * 2 * 3
        assert {row["model"] for row in metrics} == models
        assert len({row["model_id"] for row in metrics}) == 1
        for row in metrics:
            if row["session_kind"] == "CASH":
                assert row["count"] == 2
                assert row["coverage"] == .5
                assert row["mae"] == 12.5
                assert row["rmse"] == pytest.approx((212.5) ** .5)
            elif row["session_kind"] == "EXTENDED":
                assert row["count"] == 1
                assert row["coverage"] == 0.
                assert row["mae"] == 30.
            else:
                assert row["session_kind"] == "LEGACY"
                assert row["count"] == 1
                assert row["coverage"] == 1.
                assert row["mae"] == 1.
        assert store.report_metrics("NQ 12-26") == []
        assert store.get_forecasts() == original_forecasts
        assert store.get_evaluations() == original_evaluations
        assert store.evaluate() == []

    with PredictionStore(path, clock=lambda: clock[0]) as reopened:
        assert reopened.report_metrics() == metrics
        assert reopened.get_forecasts() == original_forecasts
        legacy = [row for row in reopened.get_forecasts() if "session_kind" not in row]
        assert len(legacy) == 1
        assert "session_scope" not in legacy[0]


@pytest.mark.parametrize("metadata", [
    {"session_scope": "ALL_HOURS"},
    {"session_kind": "CASH"},
    {"session_scope": "CASH_ONLY", "session_kind": "CASH"},
    {"session_scope": "ALL_HOURS", "session_kind": "LEGACY"},
    {"session_scope": "ALL_HOURS", "session_kind": "cash"},
    {"session_scope": "ALL_HOURS", "session_kind": None},
    {"session_scope": None, "session_kind": "EXTENDED"},
    {"session_scope": "ALL_HOURS", "session_kind": ["CASH", "EXTENDED"]},
])
def test_invalid_explicit_session_metadata_is_rejected_without_writes(tmp_path, metadata):
    with _store(tmp_path) as store:
        with pytest.raises(ValueError, match="session_"):
            store.log_forecast(_payload(**metadata))
        with pytest.raises(ValueError, match="session_"):
            store.log_forecast(_provisional(**metadata))
        assert store.get_forecasts() == []
