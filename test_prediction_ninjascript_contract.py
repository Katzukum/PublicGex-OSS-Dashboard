"""Structural guards at the NinjaScript/Python live-forecast boundary.

The installed-assembly compiler check is scripts/Test-PredictionIndicator.ps1;
these tests specifically protect series indexes, lifecycle, chart-price units,
and the request sequence against accidental integration regressions.
"""

from pathlib import Path
import re


SOURCE = Path(__file__).with_name("OpenGamma.cs").read_text(encoding="utf-8-sig")


def _between(start, end):
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


def test_forecast_series_is_dedicated_and_completed_bars_are_captured():
    assert "private const int PredictionSeriesIndex = 2;" in SOURCE
    configure = _between("else if (State == State.Configure)", "else if (State == State.DataLoaded)")
    assert configure.index("AddDataSeries(BarsPeriodType.Minute, JmaTimeSeriesMinutes)") < configure.index("AddDataSeries(BarsPeriodType.Minute, 5)")
    capture = _between("private void CapturePredictionBar(DateTime now)", "private void StartPredictionClient()")
    assert "State == State.Historical" in capture and "barsAgo = 0" in capture
    assert "IsFirstTickOfBar" not in capture and "barsAgo = 1" in capture
    assert "Calculate == Calculate.OnBarClose" in capture
    assert "CapturePredictionBar(DateTime.UtcNow);" in SOURCE
    assert "TimeZoneInfo.ConvertTimeToUtc" in capture
    assert "endUtc > now" in capture and "TotalSeconds <= 90" in capture
    assert "GetPredictionHistoryPolicy(), predictionHistoryPolicy" in capture


def test_network_start_and_shutdown_are_tied_to_realtime_lifecycle():
    loaded = _between("else if (State == State.DataLoaded)", "else if (State == State.Realtime)")
    realtime = _between("else if (State == State.Realtime)", "else if (State == State.Terminated)")
    terminated = _between("else if (State == State.Terminated)", "#region Chart-feed prediction client")
    assert "StartPredictionClient()" not in loaded
    assert "StartPredictionClient()" in realtime
    assert "StopPredictionClient()" in terminated
    assert "predictionRealtime = false" in terminated
    callbacks = _between("protected override void OnBarUpdate()", "#region Properties")
    assert "PredictionRequest(" not in callbacks
    assert "PredictionClientLoop(" not in callbacks
    assert "TcpClient(" not in callbacks


def test_client_protocol_has_upload_commit_and_chart_feed_metadata():
    client = _between("private void PredictionClientLoop()", "private bool PredictionDouble(")
    assert client.index('\\"HELLO\\"') < client.index('\\"HISTORY_END\\"') < client.index('\\"PING\\"')
    assert 'PredictionJsonString(predictionHistoryPolicy)' in client
    assert 'PredictionJsonString(predictionFeedLabel)' in client
    hello = client.split('PredictionJsonString(predictionInstrument)', 1)[1].split("List<PredictionBar> history;", 1)[0]
    assert "helloReply: true" in hello
    request = _between("private void PredictionRequest(", "private void PredictionClientLoop()")
    assert "ParsePredictionReply(reply, establishSeries, helloReply)" in request
    assert '\\"schema_version\\":2' in client
    assert '\\"schema_version\\":1' not in SOURCE
    assert '\\"HISTORY_END\\"}\", true)' in client
    assert "Math.Min(256, history.Count - offset)" in client
    assert "IPAddress.Loopback, PredictionPort" in client
    assert "predictionStop.WaitOne(2000)" in client
    assert "predictionStop.WaitOne(5000)" in client
    policy = _between("private string GetPredictionHistoryPolicy()", "private void CapturePredictionBar(DateTime now)")
    assert "MergePolicy.UseGlobalSettings" in policy
    assert "Core.Globals.MarketDataOptions.GlobalMergePolicy" in policy
    assert "return policy.ToString()" in policy
    assert "MergePolicy.DoNotMerge" not in policy
    loaded = _between("else if (State == State.DataLoaded)", "else if (State == State.Realtime)")
    assert "predictionHistoryPolicy = GetPredictionHistoryPolicy()" in loaded
    assert "predictionHistoryPolicyValid = true" in loaded
    assert "predictionHistory.Clear()" in loaded
    assert "Merge policy changed; reload historical data." in SOURCE
    assert "Set this instrument merge policy to DoNotMerge" not in SOURCE


def test_frames_are_series_bound_fresh_and_drawn_in_chart_units():
    parser = _between("private void ParsePredictionReply(string json, bool establishSeries, bool helloReply)", "private void RenderPredictions(")
    assert "string.Equals(instrument, predictionInstrument, StringComparison.Ordinal)" in parser
    assert "frame.OriginUtc > now" in parser
    assert "frame.ValidUntilUtc <= now" in parser
    assert "frame.OriginUtc.AddSeconds(390)" in parser
    assert 'status == "ERROR"' in parser and "throw new IOException" in parser
    assert 'ExtractJsonValue(json, "schema_version") != "2"' in parser
    assert "string.Equals(policy, predictionHistoryPolicy, StringComparison.Ordinal)" in parser
    assert "string.Equals(feedLabel, predictionFeedLabel, StringComparison.Ordinal)" in parser
    assert "string.Equals(seriesId, predictionSeriesId, StringComparison.Ordinal)" in parser
    establish = parser.split("if (establishSeries)", 1)[1]
    assert establish.index("string.IsNullOrWhiteSpace(seriesId)") < establish.index("predictionSeriesId = seriesId")
    assert "string.IsNullOrWhiteSpace(seriesId) || predictionSeriesId.Length == 0" in parser
    render = _between("private void RenderPredictions(", "#region TCP Client")
    assert "State == State.Realtime" in render
    assert "!PredictionPlaybackActive()" in render
    for field in ("Lower", "Center", "Upper"):
        assert f"chartScale.GetYByValue(frame.{field}[i])" in render
    assert not re.search(r"frame\.(Lower|Center|Upper).*[-+]\s*(sprd|spread)", render)
    assert "Next 5m high-vol" in render
    assert "nominal 80% endpoints" in render
    assert "frame.SeriesId == currentSeriesId" in render
    assert "frame.HistoryPolicy == predictionHistoryPolicy" in render
    assert "frame.FeedLabel == predictionFeedLabel" in render
    assert "Merged chart history" in render


def test_prediction_properties_do_not_change_generated_api_signatures():
    assert "EnablePredictions = true;" in SOURCE
    assert "PredictionPort = 5011;" in SOURCE
    assert 'PredictionFeedLabel = "ChartFeed";' in SOURCE
    properties = _between("#region Properties", "#region NinjaScript generated code")
    for name, type_name in (("EnablePredictions", "bool"), ("PredictionPort", "int"), ("PredictionFeedLabel", "string")):
        preceding = properties.split(f"public {type_name} {name}", 1)[0]
        attributes = preceding.rsplit("}", 1)[-1]
        assert "[NinjaScriptProperty]" not in attributes
    generated = SOURCE.split("#region NinjaScript generated code", 1)[1]
    assert "PredictionPort" not in generated and "EnablePredictions" not in generated and "PredictionFeedLabel" not in generated


def test_provisional_mode_uses_only_ewma_ranges_and_no_markov_probability():
    parser = _between("private void ParsePredictionReply(string json, bool establishSeries, bool helloReply)", "private void RenderPredictions(")
    assert 'if (legacyFitted) forecastMode = "FITTED";' in parser
    assert 'frame.Provisional = forecastMode == "PROVISIONAL";' in parser
    assert 'frame.Provisional ? "EWMA" : "GARCH_MARKOV"' in parser
    assert "PredictionModelsMatch(json, frame.Provisional)" in parser
    assert "frame.TrainingReturns < 0 || frame.TrainingSessions < 0" in parser
    assert 'new string[] { "ewma_15", "ewma_30" }' in parser
    assert 'new string[] { "garch_15", "garch_30", "markov_15", "markov_30" }' in parser
    assert "frame.HighVolProbability = double.NaN;" in parser
    assert 'if (!frame.Provisional && (!PredictionDouble(json, "high_vol_probability"' in parser
    assert "frame.Lower = new double[prefixes.Length]" in parser
    render = _between("private void RenderPredictions(", "#region TCP Client")
    assert '"PROVISIONAL | limited history"' in render
    assert 'new string[] { "EWMA 15m", "EWMA 30m" }' in render
    assert 'new string[] { "E15", "E30" }' in render
    assert "i < laneNames.Length" in render
    footer = render.split('if (frame.Provisional)', 1)[1].split("// Fixed-price endpoint lanes", 1)[0]
    provisional_footer, fitted_footer = footer.split("else", 1)
    assert "HighVolProbability" not in provisional_footer
    assert "frame.ModeReason" in provisional_footer
    assert "HighVolProbability" in fitted_footer
    assert "30 days" not in SOURCE
    assert "Load available chart history; waiting for a current five-minute bar." in SOURCE


def test_completed_fit_cannot_repaint_same_origin_but_can_upgrade_next_origin():
    publish = _between("// Completing a fit cannot repaint", "private void RenderPredictions(")
    assert "predictionFrame.SeriesId == frame.SeriesId" in publish
    assert "frame.OriginUtc <= predictionFrame.OriginUtc" in publish
    assert publish.index("return;") < publish.index("predictionFrame = frame;")


def test_session_kind_is_optional_for_legacy_frames_and_explicitly_validated():
    parser = _between("private void ParsePredictionReply(string json, bool establishSeries, bool helloReply)", "private void RenderPredictions(")
    assert 'frame.SessionKind = PredictionString(json, "session_kind");' in parser
    assert 'frame.SessionKind == null && json.IndexOf("\\\"session_kind\\\":", StringComparison.Ordinal) < 0' in parser
    assert 'frame.SessionKind = "CASH";' in parser
    assert 'frame.SessionKind != "CASH" && frame.SessionKind != "EXTENDED"' in parser
    assert 'throw new IOException("Unsupported forecast session kind.")' in parser
    assert parser.index("Unsupported forecast session kind.") < parser.index("predictionFrame = frame;")


def test_extended_hours_label_is_visible_for_both_forecast_modes():
    render = _between("private void RenderPredictions(", "#region TCP Client")
    assert 'string sessionLabel = frame.SessionKind == "EXTENDED"' in render
    assert '? "EXTENDED HOURS | experimental" : "CASH SESSION";' in render
    session_row = render.split('string sessionLabel', 1)[1].split('string historyLabel', 1)[0]
    assert 'frame.Provisional' not in session_row
    assert 'RenderTarget.DrawText(sessionLabel' in session_row
    assert 'y += 18;' in session_row
    assert 'float panelHeight = usable ? (frame.Provisional ? 204 : 190) : 78;' in render
    assert 'new SharpDX.RectangleF(x - 5, y - 5, width, panelHeight)' in render
    assert 'new SharpDX.RectangleF(x, y + 20, width - 10, 48)' in render
