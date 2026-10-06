"""Execute the indicator's real C# response parser without a running chart.

The harness extracts the production methods verbatim, stubbing only the chart
refresh callback. It targets installed .NET Framework, matching NinjaTrader.
"""

from datetime import datetime, timedelta, timezone
import base64
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


SOURCE_PATH = Path(__file__).with_name("OpenGamma.cs")


def _section(source, start, end):
    return source[source.index(start):source.index(end, source.index(start))]


def _compile_harness(tmp_path_factory, name, source_text):
    if os.name != "nt":
        pytest.skip("Native .NET Framework parser harness requires Windows")
    dotnet = shutil.which("dotnet")
    framework = Path(os.environ["WINDIR"]) / "Microsoft.NET/Framework64/v4.0.30319"
    if not dotnet or not (framework / "mscorlib.dll").is_file():
        pytest.skip("Installed .NET SDK and .NET Framework are required")
    sdks = subprocess.run([dotnet, "--list-sdks"], check=True, capture_output=True, text=True).stdout
    compilers = [Path(root) / version / "Roslyn/bincore/csc.dll"
                 for version, root in re.findall(r"^([^ ]+) \[(.+)\]$", sdks, re.MULTILINE)]
    compiler = next((path for path in reversed(compilers) if path.is_file()), None)
    if compiler is None:
        pytest.skip("Installed Roslyn C# compiler is required")

    build_dir = tmp_path_factory.mktemp("prediction-csharp-" + name)
    cs_path = build_dir / (name + ".cs")
    exe_path = build_dir / (name + ".exe")
    cs_path.write_text(source_text, encoding="utf-8")
    command = [dotnet, str(compiler), "/nologo", "/target:exe", "/nostdlib+", "/langversion:latest",
               "/out:" + str(exe_path)]
    command += ["/reference:" + str(framework / name) for name in ("mscorlib.dll", "System.dll", "System.Core.dll")]
    command.append(str(cs_path))
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return exe_path


@pytest.fixture(scope="module")
def parser_executable(tmp_path_factory):
    source = SOURCE_PATH.read_text(encoding="utf-8-sig")
    production = "\n".join([
        _section(source, "private sealed class PredictionFrame", "private struct GammaLevel"),
        _section(source, "private void SetPredictionStatus(", "private void RefreshPredictionChart()"),
        _section(source, "private bool PredictionDouble(", "private void RenderPredictions("),
        _section(source, "private string ExtractJsonValue(", "private bool ParseGammaLevels("),
    ])
    harness = r'''
using System;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Text;
public sealed class ParserHarness {
    private readonly object predictionLock = new object();
    private string predictionInstrument = "NQ DEC26";
    private string predictionHistoryPolicy = "MergeBackAdjusted";
    private string predictionFeedLabel = "ChartFeed";
    private string predictionSeriesId = "";
    private string predictionStatus = "INITIAL";
    private string predictionMessage = "";
    private PredictionFrame predictionFrame;
    private void RefreshPredictionChart() {}
    private static void Emit(string value) {
        Console.WriteLine(Convert.ToBase64String(Encoding.UTF8.GetBytes(value ?? "")));
    }
    public static int Main(string[] args) {
        var parser = new ParserHarness();
        parser.predictionSeriesId = args[2];
        if (args[3] == "true") parser.predictionFrame = new PredictionFrame { ModelId = "existing" };
        PredictionFrame before = parser.predictionFrame;
        string error = "";
        try { parser.ParsePredictionReply(Console.ReadLine(), args[1] == "true", args[0] == "true"); }
        catch (IOException ex) { error = ex.Message; }
        Emit(error);
        Emit(parser.predictionStatus);
        Emit(parser.predictionMessage);
        Emit(parser.predictionSeriesId);
        Emit(parser.predictionFrame == null ? "" : parser.predictionFrame.ModelId);
        Emit(ReferenceEquals(before, parser.predictionFrame) ? "unchanged" : "changed");
        return 0;
    }
'''
    return _compile_harness(tmp_path_factory, "ParserHarness", harness + production + "\n}\n")


def _parse(executable, payload, *, hello=False, establish=False, series="", existing=False):
    result = subprocess.run([str(executable), str(hello).lower(), str(establish).lower(), series,
                             str(existing).lower()], input=json.dumps(payload, separators=(",", ":")) + "\n",
                            text=True, capture_output=True, check=True)
    values = [base64.b64decode(line).decode("utf-8") for line in result.stdout.splitlines()]
    return dict(zip(("error", "status", "message", "series", "model", "frame"), values, strict=True))


def _status(**changes):
    payload = dict(schema_version=2, type="STATUS", status="WAITING_FOR_HISTORY", instrument="NQ DEC26",
                   history_policy="MergeBackAdjusted", feed_label="ChartFeed", series_id="",
                   server_time_utc=datetime.now(timezone.utc).isoformat(), message="Connected")
    payload.update(changes)
    return payload


def _forecast(**changes):
    now = datetime.now(timezone.utc)
    payload = _status(type="FORECAST", status="SHADOW", series_id="series-1", model_id="ewma-test",
                      session_kind="EXTENDED", forecast_mode="PROVISIONAL", model_family="EWMA",
                      available_models=["ewma"], training_returns=0, training_sessions=0,
                      origin_utc=(now - timedelta(seconds=30)).isoformat(), generated_at_utc=now.isoformat(),
                      valid_until_utc=(now + timedelta(seconds=300)).isoformat(),
                      origin_price=30000, nominal_coverage=0.8, high_vol_probability=None)
    for horizon in (15, 30):
        payload.update({f"ewma_{horizon}_lower": 29990, f"ewma_{horizon}_center": 30000,
                        f"ewma_{horizon}_upper": 30010})
    payload.update(changes)
    return payload


def test_unscoped_hello_rejection_preserves_the_server_reason(parser_executable):
    payload = _status(status="ERROR", instrument=None, history_policy="", feed_label="",
                      message="Unsupported contract label: NQ DEC26")
    reply = _parse(parser_executable, payload, hello=True)
    assert reply["error"] == "Forecast server rejected HELLO: Unsupported contract label: NQ DEC26"
    assert reply["series"] == reply["model"] == ""
    assert reply["frame"] == "unchanged"
    assert reply["status"] == "INITIAL"


@pytest.mark.parametrize("overrides,context", [
    ({"status": "WAITING_FOR_HISTORY"}, {"hello": True}),
    ({"type": "FORECAST", "status": "SHADOW"}, {"hello": True}),
    ({}, {"hello": False}),
    ({}, {"hello": True, "establish": True}),
    ({}, {"hello": True, "series": "series-1"}),
    ({"series_id": "unexpected-series"}, {"hello": True}),
    ({"instrument": "ES DEC26"}, {"hello": True}),
])
def test_unscoped_error_exception_never_weakens_identity_checks(parser_executable, overrides, context):
    payload = _status(status="ERROR", instrument=None, history_policy="", feed_label="", message="rejected")
    payload.update(overrides)
    reply = _parse(parser_executable, payload, **context)
    assert reply["error"].startswith("Prediction contract mismatch: chart expects 'NQ DEC26', server returned '")
    assert reply["series"] == context.get("series", "")
    assert reply["model"] == ""
    assert reply["frame"] == "unchanged"


def test_hello_rejection_still_requires_supported_protocol(parser_executable):
    reply = _parse(parser_executable, _status(status="ERROR", instrument=None, schema_version=1), hello=True)
    assert reply["error"] == "Unsupported prediction protocol version."
    assert reply["series"] == reply["model"] == ""


def test_true_contract_mismatch_names_expected_and_received_labels(parser_executable):
    reply = _parse(parser_executable, _forecast(instrument="ES DEC26"), series="series-1", existing=True)
    assert reply["error"] == "Prediction contract mismatch: chart expects 'NQ DEC26', server returned 'ES DEC26'."
    assert reply["model"] == "existing" and reply["frame"] == "unchanged"


@pytest.mark.parametrize("changes,error", [
    ({"history_policy": "DoNotMerge"}, "Prediction history policy or feed label does not match this chart."),
    ({"feed_label": "AnotherFeed"}, "Prediction history policy or feed label does not match this chart."),
    ({"series_id": "series-2"}, "Prediction history series changed; reconnect and reload history."),
])
def test_forecast_provenance_remains_strict(parser_executable, changes, error):
    reply = _parse(parser_executable, _forecast(**changes), series="series-1", existing=True)
    assert reply["error"] == error
    assert reply["model"] == "existing" and reply["frame"] == "unchanged"


def test_accepted_hello_cannot_install_a_series_or_forecast(parser_executable):
    reply = _parse(parser_executable, _status(), hello=True)
    assert reply["error"] == ""
    assert reply["status"] == "WAITING_FOR_HISTORY"
    assert reply["series"] == reply["model"] == ""


def test_history_commit_and_valid_forecast_are_still_accepted(parser_executable):
    reply = _parse(parser_executable, _status(series_id="series-1"), establish=True)
    assert reply["error"] == "" and reply["series"] == "series-1" and reply["model"] == ""
    reply = _parse(parser_executable, _forecast(), series="series-1")
    assert reply["error"] == "" and reply["status"] == "SHADOW"
    assert reply["series"] == "series-1" and reply["model"] == "ewma-test"


def test_even_a_valid_forecast_cannot_be_published_as_hello_reply(parser_executable):
    reply = _parse(parser_executable, _forecast(), hello=True, series="series-1", existing=True)
    assert reply["error"] == "Invalid prediction message type/status."
    assert reply["model"] == "existing" and reply["frame"] == "unchanged"
