"""Execute completed-bar capture extracted verbatim from the real indicator."""

import subprocess

import pytest

from test_prediction_ninjascript_parser import SOURCE_PATH, _compile_harness, _section


@pytest.fixture(scope="module")
def capture_executable(tmp_path_factory):
    source = SOURCE_PATH.read_text(encoding="utf-8-sig")
    production = "\n".join([
        _section(source, "private sealed class PredictionBar", "// Published once under predictionLock"),
        _section(source, "private void CapturePredictionBar(", "private void StartPredictionClient()"),
    ])
    harness = r'''
using System;
using System.Collections.Generic;
using System.Globalization;
using System.Linq;
public enum State { Historical, Realtime, Transition }
public enum Calculate { OnBarClose, OnPriceChange, OnEachTick }
public sealed class FakeBars { public bool IsTickReplay; }
public sealed class CaptureHarness {
    private const int PredictionSeriesIndex = 2;
    private const int PredictionHistoryLimit = 20000;
    private bool EnablePredictions = true;
    private readonly object predictionLock = new object();
    private readonly SortedDictionary<DateTime, PredictionBar> predictionHistory = new SortedDictionary<DateTime, PredictionBar>();
    private readonly Queue<PredictionBar> predictionLiveQueue = new Queue<PredictionBar>();
    private DateTime predictionLastLiveEnd = DateTime.MinValue;
    private bool predictionHistoryPolicyValid = true;
    private string predictionHistoryPolicy = "MergeBackAdjusted";
    private TimeZoneInfo predictionTimeZone = TimeZoneInfo.Utc;
    private int[] CurrentBars = { 10, 10, 10 };
    private FakeBars[] BarsArray = { new FakeBars(), new FakeBars(), new FakeBars() };
    private DateTime[][] Times = { null, null, new DateTime[2] };
    private double[][] Closes = { null, null, new double[] { 99999, 30000 } };
    private State State;
    private Calculate Calculate;
    private bool IsFirstTickOfBar = false;
    private bool playback;
    private string GetPredictionHistoryPolicy() { return "MergeBackAdjusted"; }
    private bool PredictionPlaybackActive() { return playback; }
    public static int Main(string[] args) {
        var capture = new CaptureHarness();
        capture.State = (State)Enum.Parse(typeof(State), args[0]);
        capture.Calculate = (Calculate)Enum.Parse(typeof(Calculate), args[1]);
        capture.BarsArray[2].IsTickReplay = args[2] == "true";
        DateTime completed = new DateTime(2026, 9, 30, 18, 35, 0, DateTimeKind.Utc);
        DateTime now = completed.AddSeconds(int.Parse(args[3], CultureInfo.InvariantCulture));
        capture.Times[2][1] = completed;
        capture.Times[2][0] = completed.AddMinutes(5);
        if (capture.Calculate == Calculate.OnBarClose || capture.State == State.Historical && !capture.BarsArray[2].IsTickReplay)
        {
            capture.Times[2][0] = completed;
            capture.Times[2][1] = completed.AddMinutes(-5);
        }
        capture.CurrentBars[2] = int.Parse(args[5], CultureInfo.InvariantCulture);
        capture.playback = args[6] == "true";
        capture.IsFirstTickOfBar = args[7] == "true";
        for (int i = 0; i < int.Parse(args[4], CultureInfo.InvariantCulture); i++)
            capture.CapturePredictionBar(now);
        Console.WriteLine(capture.predictionHistory.Count);
        Console.WriteLine(capture.predictionLiveQueue.Count);
        Console.WriteLine(capture.predictionLiveQueue.Count == 0 ? "none" : capture.predictionLiveQueue.Peek().Close.ToString(CultureInfo.InvariantCulture));
        Console.WriteLine(capture.predictionLiveQueue.Count == 0 ? "none" : capture.predictionLiveQueue.Peek().EndUtc.ToString("O", CultureInfo.InvariantCulture));
        Console.WriteLine(capture.predictionHistory.Count == 0 ? "none" : capture.predictionHistory.Last().Value.Close.ToString(CultureInfo.InvariantCulture));
        return 0;
    }
'''
    return _compile_harness(tmp_path_factory, "CaptureHarness", harness + production + "\n}\n")


def _capture(executable, *, state="Realtime", calculate="OnPriceChange", replay=False,
             age=30, repetitions=1, current_bar=10, playback=False, first_tick=False):
    args = [state, calculate, str(replay).lower(), str(age), str(repetitions), str(current_bar),
            str(playback).lower(), str(first_tick).lower()]
    result = subprocess.run([str(executable), *args], capture_output=True, text=True, check=True)
    history, queued, close, end, history_close = result.stdout.splitlines()
    return dict(history=int(history), queued=int(queued), close=close, end=end, history_close=history_close)


@pytest.mark.parametrize("calculate", ["OnPriceChange", "OnEachTick"])
@pytest.mark.parametrize("first_tick", [False, True])
def test_intrabar_callback_captures_only_the_completed_prior_bar(capture_executable, calculate, first_tick):
    result = _capture(capture_executable, calculate=calculate, first_tick=first_tick)
    assert result["history"] == result["queued"] == 1
    assert result["close"] == "30000"  # The incomplete [0] price is deliberately different.
    assert result["end"] == "2026-09-30T18:35:00.0000000Z"


def test_repeated_intrabar_callbacks_do_not_duplicate_the_live_bar(capture_executable):
    result = _capture(capture_executable, repetitions=5)
    assert result["history"] == result["queued"] == 1
    assert result["close"] == "30000"


def test_realtime_on_bar_close_uses_completed_current_bar(capture_executable):
    result = _capture(capture_executable, calculate="OnBarClose")
    assert result["history"] == result["queued"] == 1
    assert result["close"] == "99999"
    assert result["end"] == "2026-09-30T18:35:00.0000000Z"


@pytest.mark.parametrize("calculate,replay,expected", [
    ("OnPriceChange", False, "99999"),
    ("OnEachTick", False, "99999"),
    ("OnBarClose", False, "99999"),
    ("OnBarClose", True, "99999"),
    ("OnPriceChange", True, "30000"),
    ("OnEachTick", True, "30000"),
])
def test_historical_capture_never_masquerades_as_live(capture_executable, calculate, replay, expected):
    result = _capture(capture_executable, state="Historical", calculate=calculate, replay=replay)
    assert result["history"] == 1 and result["queued"] == 0
    assert result["history_close"] == expected


@pytest.mark.parametrize("age,history,queued", [(-1, 0, 0), (0, 1, 1), (90, 1, 1), (91, 1, 0), (330, 1, 0)])
def test_future_and_stale_bars_cannot_enter_the_live_queue(capture_executable, age, history, queued):
    result = _capture(capture_executable, age=age)
    assert result["history"] == history and result["queued"] == queued


@pytest.mark.parametrize("options", [{"current_bar": 0}, {"current_bar": -1}, {"playback": True}, {"state": "Transition"}])
def test_no_live_bar_when_capture_preconditions_are_missing(capture_executable, options):
    result = _capture(capture_executable, **options)
    assert result["history"] == result["queued"] == 0
