"""All-hours ingestion, chronology and cash/extended labeling boundaries."""
from datetime import date, timedelta

import numpy as np
import pytest

from prediction.data import (forecast_session_kind, iso_utc, live_closes,
                             normalize_bars, parse_utc, training_data)


def sequence(first, count=6):
    start = parse_utc(first)
    return [{"end_utc": iso_utc(start + timedelta(minutes=5 * index)),
             "close": 6000.0 + index * .25} for index in range(count)]


@pytest.mark.parametrize("first", [
    "2026-09-30T22:05:00Z",  # Evening futures trading.
    "2026-10-01T03:45:00Z",  # Cross New York midnight.
    "2026-10-03T18:00:00Z",  # Saturday: no fabricated bars, accept actual input.
    "2026-11-26T15:00:00Z",  # Cash holiday.
    "2028-01-03T15:00:00Z",  # Outside the verified cash-calendar years.
    "2026-09-30T13:15:00Z",  # Cross the cash open.
    "2026-09-30T19:45:00Z",  # Cross the cash close.
])
def test_actual_completed_bars_are_retained_and_used_at_every_hour(first):
    bars = sequence(first)
    origin = bars[-1]["end_utc"]
    accepted = normalize_bars(list(reversed(bars)), parse_utc(origin) + timedelta(seconds=1))
    assert accepted == bars
    assert live_closes(accepted, origin) == [row["close"] for row in bars]


def test_extended_bars_keep_future_alignment_and_price_validation():
    origin = parse_utc("2026-09-30T23:00:00Z")
    for invalid in (
        {"end_utc": iso_utc(origin + timedelta(minutes=5)), "close": 6000},
        {"end_utc": "2026-09-30T22:59:00Z", "close": 6000},
        {"end_utc": iso_utc(origin), "close": float("inf")},
        {"end_utc": iso_utc(origin), "close": 0},
    ):
        with pytest.raises(ValueError):
            normalize_bars([invalid], origin)


def test_live_sequence_resets_at_actual_gap_and_ignores_future_rows():
    bars = sequence("2026-10-01T03:30:00Z", 15)
    # Only five contiguous closes follow the omitted midnight bar.
    assert live_closes(bars[:6] + bars[7:], bars[11]["end_utc"]) == []
    assert live_closes(bars[:6] + bars[7:], bars[12]["end_utc"]) == [row["close"] for row in bars[7:13]]
    assert live_closes(list(reversed(bars)), bars[5]["end_utc"]) == [row["close"] for row in bars[:6]]
    assert live_closes(bars[:5] + bars[6:], bars[5]["end_utc"]) == []


def test_training_returns_cross_midnight_but_never_cross_missing_bar():
    bars = sequence("2026-10-01T03:45:00Z", 12)
    blocks, evidence = training_data(bars[:5] + bars[6:], date(2026, 10, 2))
    assert [len(block["y"]) for block in blocks] == [5, 6]
    assert evidence["sessions"] == 2
    assert evidence["training_points"] == 11
    assert evidence["training_returns"] == 9
    assert evidence["trained_through"] == "2026-10-01"
    assert evidence["training_end_utc"] == bars[-1]["end_utc"]
    np.testing.assert_allclose(blocks[0]["y"], np.log([row["close"] for row in bars[:5]]) * 10000)


def test_training_cold_start_cutoff_excludes_origin_and_future_and_preserves_prefix():
    bars = sequence("2026-10-01T03:45:00Z", 12)
    day, origin = date(2026, 10, 1), bars[7]["end_utc"]
    blocks, evidence = training_data(bars, day, before=origin)
    assert [len(block["y"]) for block in blocks] == [7]
    assert evidence["training_returns"] == 6
    assert evidence["sessions"] == 2
    assert evidence["training_end_utc"] == bars[6]["end_utc"]
    _, prefix_evidence = training_data(bars[:7], day, before=origin)
    assert evidence == prefix_evidence
    # Default training still excludes all observations on the forecast date.
    prior_blocks, prior_evidence = training_data(bars, day)
    assert [len(block["y"]) for block in prior_blocks] == [3]
    assert prior_evidence["trained_through"] == "2026-09-30"
    assert prior_evidence["training_end_utc"] == bars[2]["end_utc"]


def test_training_includes_holidays_weekends_and_unknown_calendar_years():
    bars = (sequence("2027-12-24T15:00:00Z")
            + sequence("2027-12-25T15:00:00Z")
            + sequence("2028-01-02T15:00:00Z"))
    blocks, evidence = training_data(bars, date(2028, 1, 3))
    assert [len(block["y"]) for block in blocks] == [6, 6, 6]
    assert evidence["sessions"] == 3
    assert evidence["training_returns"] == 15


def test_training_is_bounded_to_latest_60_observed_new_york_dates():
    start = parse_utc("2026-07-01T22:00:00Z")
    bars = [row for day_index in range(62)
            for row in sequence(iso_utc(start + timedelta(days=day_index)), 2)]
    blocks, evidence = training_data(bars, date(2026, 9, 2))
    assert len(blocks) == evidence["sessions"] == 60
    assert evidence["training_points"] == 120
    assert evidence["training_returns"] == 60
    np.testing.assert_allclose(blocks[0]["y"], np.log([row["close"] for row in bars[4:6]]) * 10000)
    # Old excluded data cannot change the fitted sample fingerprint.
    bars[0]["close"] += 123
    assert training_data(bars, date(2026, 9, 2))[1]["fingerprint"] == evidence["fingerprint"]


@pytest.mark.parametrize(("origin", "kind"), [
    ("2026-09-30T13:30:00Z", "EXTENDED"),  # Cash open itself is not a cash close.
    ("2026-09-30T13:35:00Z", "CASH"),
    ("2026-09-30T19:30:00Z", "CASH"),      # Exact horizon at cash close.
    ("2026-09-30T19:35:00Z", "EXTENDED"),  # Full horizon extends past close.
    ("2026-09-30T23:00:00Z", "EXTENDED"),
    ("2026-10-03T15:00:00Z", "EXTENDED"),
    ("2026-11-26T15:00:00Z", "EXTENDED"),
    ("2026-11-27T17:30:00Z", "CASH"),      # Thanksgiving early close.
    ("2026-11-27T17:35:00Z", "EXTENDED"),
    ("2028-01-03T15:00:00Z", "EXTENDED"),
])
def test_cash_label_requires_entire_horizon_within_known_session(origin, kind):
    assert forecast_session_kind(origin) == kind


def test_cash_label_respects_requested_horizon():
    assert forecast_session_kind("2026-09-30T19:45:00Z", 15) == "CASH"
    assert forecast_session_kind("2026-09-30T19:45:00Z", 30) == "EXTENDED"
