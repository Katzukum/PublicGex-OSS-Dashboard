#region Using declarations
using System;
using System.Collections.Generic;
using System.ComponentModel;
using System.ComponentModel.DataAnnotations;
using System.Linq;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Input;
using System.Windows.Media;
using System.Xml.Serialization;
using System.Net;
using System.Net.Sockets;
using System.IO;
using System.Globalization;
using NinjaTrader.Cbi;
using NinjaTrader.Gui;
using NinjaTrader.Gui.Chart;
using NinjaTrader.Gui.SuperDom;
using NinjaTrader.Gui.Tools;
using NinjaTrader.Data;
using NinjaTrader.NinjaScript;
using NinjaTrader.Core.FloatingPoint;
using NinjaTrader.NinjaScript.DrawingTools;
#endregion

namespace NinjaTrader.NinjaScript.Indicators
{
    public class OpenGamma : Indicator
    {
        #region Variables
        private TcpListener tcpListener;
        private Thread listenerThread;
        private TcpClient activeClient;
        private volatile bool isRunning;
        private readonly ManualResetEvent stopRequested = new ManualResetEvent(false);
        private readonly object clientLock = new object();

        // Regime state (protected by lockObj)
        private volatile string currentRegime = "WAITING";
        private volatile string previousRegime = "---";
        private volatile int regimeCode = 0;
        private volatile string lastUpdate = "";

        // Index price captured on update (not on tick)
        private double indexPrice = 0;
        private double futuresPrice = 0;
        private double rawSpread = 0;
        private double spread = 0;
        private bool jmaSpreadInitialized = false;
        private double jmaSpreadValue = 0;
        private double jmaSpreadE0 = 0;
        private double jmaSpreadE1 = 0;
        private double jmaSpreadE2 = 0;
        private string indexSymbol = "";
        private double acceleration = 0;
        private string dashboardSymbol = "";
        private string dashboardBias = "";
        private double dashboardBiasScore = double.NaN;
        private double dashboardConfidence = double.NaN;
        private double dashboardTarget = double.NaN;
        private double dashboardInvalidation = double.NaN;
        private double dashboardFlip = double.NaN;
        private double modeledZeroGex = double.NaN;
        private string dashboardContext = "";
        private string dashboardMarket = "";
        private string dashboardDealer = "";
        private string dashboardLiquidity = "";
        private string dashboardWhale = "";
        private string dashboardEdgeSummary = "";
        private string dashboardEdgeSource = "";
        private double dashboardEdgeWinRate = double.NaN;
        private double dashboardEdgeMedianMove = double.NaN;
        private double dashboardEdgeSample = double.NaN;

        // Use OnBarUpdate to capture price safely
        private double lastClosePrice = 0;
        private double jmaClosePrice = 0;

        // Gamma S/R levels (adjusted for futures)
        private List<GammaLevel> gammaLevels = new List<GammaLevel>();
        private const string CacheFolderName = "OpenGammaCache";
        private const string CacheVersion = "OpenGammaCacheV1";

        private readonly object lockObj = new object();

        // Independent chart-feed forecast channel. No forecasting or socket
        // work runs on the NinjaScript data/UI callbacks; only completed bars
        // are copied to these bounded in-memory buffers.
        private const int PredictionSeriesIndex = 2;
        private const int PredictionHistoryLimit = 20000;
        private readonly object predictionLock = new object();
        private readonly SortedDictionary<DateTime, PredictionBar> predictionHistory = new SortedDictionary<DateTime, PredictionBar>();
        private readonly Queue<PredictionBar> predictionLiveQueue = new Queue<PredictionBar>();
        private readonly ManualResetEvent predictionStop = new ManualResetEvent(false);
        private Thread predictionThread;
        private TcpClient predictionClient;
        private volatile bool predictionRunning;
        private volatile bool predictionRealtime;
        private volatile bool predictionHistoryPolicyValid;
        private string predictionHistoryPolicy = "";
        private string predictionFeedLabel = "ChartFeed";
        private string predictionSeriesId = "";
        private string predictionInstrument = "";
        private string predictionSymbol = "";
        private TimeZoneInfo predictionTimeZone;
        private DateTime predictionLastLiveEnd = DateTime.MinValue;
        private string predictionStatus = "WAITING_FOR_HISTORY";
        private string predictionMessage = "Load available chart history; waiting for a current five-minute bar.";
        private PredictionFrame predictionFrame;

        private sealed class PredictionBar
        {
            public DateTime EndUtc;
            public double Close;
        }

        // Published once under predictionLock and never mutated afterwards.
        private sealed class PredictionFrame
        {
            public string Instrument;
            public string ModelId;
            public string SeriesId;
            public string HistoryPolicy;
            public string FeedLabel;
            public string SessionKind;
            public bool Provisional;
            public string ModeReason;
            public int TrainingReturns = -1;
            public int TrainingSessions = -1;
            public DateTime OriginUtc;
            public DateTime GeneratedUtc;
            public DateTime ValidUntilUtc;
            public double OriginPrice;
            public double Coverage;
            public double HighVolProbability;
            public double[] Lower = new double[4];
            public double[] Center = new double[4];
            public double[] Upper = new double[4];
        }

        private struct GammaLevel
        {
            public double Strike;
            public double Gex;
            public double FuturesPrice; // Strike adjusted by spread
            public bool IsResistance;
            public bool IsKeyLevel;
        }
        #endregion

        protected override void OnStateChange()
        {
            if (State == State.SetDefaults)
            {
                Description = @"Displays market regime data from PublicGex Dashboard";
                Name = "OpenGamma";
                Calculate = Calculate.OnPriceChange; // Update on every tick to capture Close[0] properly
                IsOverlay = true;
                DisplayInDataBox = true;
                DrawOnPricePanel = true;
                IsSuspendedWhileInactive = false;

                ListenPort = 5010;
                EnablePredictions = true;
                PredictionPort = 5011;
                PredictionFeedLabel = "ChartFeed";
                GammaBarsOnRight = false;
                JmaTimeSeriesMinutes = 2;
                JmaLength = 13;
                JmaPhase = 78;
                JmaPower = 2;
                JmaResetThreshold = 25;
            }
            else if (State == State.Configure)
            {
                AddDataSeries(BarsPeriodType.Minute, JmaTimeSeriesMinutes);
                AddDataSeries(BarsPeriodType.Minute, 5); // Dedicated forecast input, index 2.
            }
            else if (State == State.DataLoaded)
            {
                // Determine index symbol based on chart instrument
                string instr = Instrument.MasterInstrument.Name.ToUpper();
                if (instr.Contains("NQ") || instr.Contains("MNQ"))
                    indexSymbol = "NDX";
                else if (instr.Contains("ES") || instr.Contains("MES"))
                    indexSymbol = "SPX";
                else
                    indexSymbol = "";

                LoadCachedGammaLevels();
                StartListener();
                predictionInstrument = Instrument.FullName;
                predictionSymbol = Instrument.MasterInstrument.Name;
                predictionTimeZone = Core.Globals.GeneralOptions.TimeZoneInfo;
                predictionHistoryPolicy = GetPredictionHistoryPolicy();
                predictionHistoryPolicyValid = true;
                predictionFeedLabel = string.IsNullOrWhiteSpace(PredictionFeedLabel) ? "ChartFeed" : PredictionFeedLabel.Trim();
                lock (predictionLock)
                {
                    predictionHistory.Clear();
                    predictionLiveQueue.Clear();
                    predictionLastLiveEnd = DateTime.MinValue;
                    predictionSeriesId = "";
                    predictionFrame = null;
                }
            }
            else if (State == State.Realtime)
            {
                predictionRealtime = true;
                if (EnablePredictions)
                    StartPredictionClient();
            }
            else if (State == State.Terminated)
            {
                predictionRealtime = false;
                StopPredictionClient();
                StopListener();
            }
        }

        #region Chart-feed prediction client
        private bool PredictionPlaybackActive()
        {
            // Playback also enters State.Realtime: State alone is insufficient.
            return NinjaTrader.Cbi.Connection.PlaybackConnection != null;
        }

        private string GetPredictionHistoryPolicy()
        {
            MergePolicy policy = Instrument.MasterInstrument.MergePolicy;
            if (policy == MergePolicy.UseGlobalSettings || policy == MergePolicy.UseDefault)
                policy = Core.Globals.MarketDataOptions.GlobalMergePolicy;
            return policy.ToString();
        }

        private void CapturePredictionBar(DateTime now)
        {
            if (!EnablePredictions || CurrentBars[PredictionSeriesIndex] < 0)
                return;
            if (!predictionHistoryPolicyValid || !string.Equals(GetPredictionHistoryPolicy(), predictionHistoryPolicy, StringComparison.Ordinal))
            {
                // This latch can only reset on a fresh data load. Changing the
                // setting alone must not relabel history from another policy.
                predictionHistoryPolicyValid = false;
                return;
            }
            if (PredictionPlaybackActive())
                return;
            int barsAgo;
            bool live = State == State.Realtime;
            if ((live || State == State.Historical) && Calculate == Calculate.OnBarClose
                || State == State.Historical && !BarsArray[PredictionSeriesIndex].IsTickReplay)
                barsAgo = 0; // Bar-close callbacks expose the completed current bar.
            else if ((live || State == State.Historical) && CurrentBars[PredictionSeriesIndex] >= 1)
                // Intrabar callbacks always expose a completed prior bar. A
                // later tick can recover a missed first-tick callback; the
                // timestamp and last-live-end checks below prevent duplicates.
                barsAgo = 1;
            else
                return;

            DateTime endUtc;
            try
            {
                DateTime applicationTime = DateTime.SpecifyKind(Times[PredictionSeriesIndex][barsAgo], DateTimeKind.Unspecified);
                endUtc = TimeZoneInfo.ConvertTimeToUtc(applicationTime, predictionTimeZone);
            }
            catch (ArgumentException)
            {
                return; // Invalid local wall-clock time must not become a guessed timestamp.
            }
            double close = Closes[PredictionSeriesIndex][barsAgo];
            if (endUtc > now || endUtc < now.AddDays(-60) || double.IsNaN(close)
                || double.IsInfinity(close) || close <= 0 || endUtc.Minute % 5 != 0
                || endUtc.Second != 0 || endUtc.Millisecond != 0)
                return;
            var bar = new PredictionBar { EndUtc = endUtc, Close = close };
            lock (predictionLock)
            {
                predictionHistory[endUtc] = bar;
                while (predictionHistory.Count > PredictionHistoryLimit
                    || (predictionHistory.Count > 0 && predictionHistory.First().Key < now.AddDays(-60)))
                    predictionHistory.Remove(predictionHistory.First().Key);
                if (live && !PredictionPlaybackActive() && (now - endUtc).TotalSeconds <= 90
                    && endUtc > predictionLastLiveEnd)
                {
                    predictionLastLiveEnd = endUtc;
                    predictionLiveQueue.Enqueue(bar);
                    while (predictionLiveQueue.Count > 256)
                        predictionLiveQueue.Dequeue();
                }
            }
        }

        private void StartPredictionClient()
        {
            if (predictionThread != null && predictionThread.IsAlive)
                return;
            predictionStop.Reset();
            predictionRunning = true;
            predictionThread = new Thread(PredictionClientLoop)
            {
                IsBackground = true,
                Name = "OpenGamma_PredictionClient"
            };
            predictionThread.Start();
        }

        private void StopPredictionClient()
        {
            predictionRunning = false;
            predictionStop.Set();
            TcpClient client;
            lock (predictionLock)
            {
                client = predictionClient;
                predictionClient = null;
                predictionFrame = null;
                predictionLiveQueue.Clear();
            }
            if (client != null)
                client.Close();
            if (predictionThread != null && predictionThread.IsAlive)
                predictionThread.Join(1500);
            predictionThread = null;
        }

        private bool PredictionClockIsLive()
        {
            if (!predictionRealtime || !EnablePredictions || PredictionPlaybackActive())
                return false;
            if (!string.Equals(GetPredictionHistoryPolicy(), predictionHistoryPolicy, StringComparison.Ordinal))
                predictionHistoryPolicyValid = false;
            if (!predictionHistoryPolicyValid) return false;
            DateTime latest;
            lock (predictionLock)
                latest = predictionHistory.Count == 0 ? DateTime.MinValue : predictionHistory.Last().Key;
            double age = (DateTime.UtcNow - latest).TotalSeconds;
            return age >= 0 && age <= 390;
        }

        private void SetPredictionStatus(string status, string message, bool hide)
        {
            lock (predictionLock)
            {
                predictionStatus = status;
                predictionMessage = message ?? "";
                if (hide)
                    predictionFrame = null;
            }
            RefreshPredictionChart();
        }

        private void RefreshPredictionChart()
        {
            if (predictionRealtime && ChartControl != null)
                ChartControl.Dispatcher.InvokeAsync(() =>
                {
                    if (predictionRealtime && ChartControl != null)
                        ForceRefresh();
                });
        }

        private static string PredictionJsonString(string value)
        {
            return "\"" + (value ?? "").Replace("\\", "\\\\").Replace("\"", "\\\"")
                .Replace("\r", "\\r").Replace("\n", "\\n").Replace("\t", "\\t") + "\"";
        }

        private string PredictionBatchJson(List<PredictionBar> bars, string source)
        {
            var json = new StringBuilder("{\"schema_version\":2,\"type\":\"BAR_BATCH\",\"source\":");
            json.Append(PredictionJsonString(source)).Append(",\"bars\":[");
            for (int i = 0; i < bars.Count; i++)
            {
                if (i > 0) json.Append(',');
                json.Append("{\"end_utc\":").Append(PredictionJsonString(bars[i].EndUtc.ToString("yyyy-MM-dd'T'HH:mm:ss'Z'", CultureInfo.InvariantCulture)))
                    .Append(",\"close\":").Append(bars[i].Close.ToString("R", CultureInfo.InvariantCulture)).Append('}');
            }
            return json.Append("]}").ToString();
        }

        private void PredictionRequest(StreamWriter writer, StreamReader reader, string request, bool establishSeries = false, bool helloReply = false)
        {
            if (!predictionRunning || !PredictionClockIsLive())
                throw new IOException("Waiting for a live wall-clock-aligned five-minute bar.");
            writer.WriteLine(request);
            string reply = reader.ReadLine();
            if (reply == null || reply.Length > 32768)
                throw new IOException("Prediction server closed the connection or sent an invalid reply.");
            ParsePredictionReply(reply, establishSeries, helloReply);
            if (establishSeries && string.IsNullOrWhiteSpace(predictionSeriesId))
                throw new IOException("HISTORY_END did not establish a forecast history series.");
        }

        private void PredictionClientLoop()
        {
            while (predictionRunning)
            {
                if (!PredictionClockIsLive())
                {
                    SetPredictionStatus(!predictionHistoryPolicyValid ? "ERROR" : "STALE", !predictionHistoryPolicyValid
                        ? "Merge policy changed; reload historical data."
                        : PredictionPlaybackActive()
                        ? "Predictions are disabled during Playback."
                        : "Load available chart history; waiting for a current five-minute bar.", true);
                    if (predictionStop.WaitOne(2000)) break;
                    continue;
                }
                TcpClient client = null;
                try
                {
                    client = new TcpClient { ReceiveTimeout = 5000, SendTimeout = 3000, NoDelay = true };
                    lock (predictionLock)
                    {
                        predictionClient = client;
                        predictionSeriesId = "";
                        predictionFrame = null;
                    }
                    IAsyncResult connect = client.BeginConnect(IPAddress.Loopback, PredictionPort, null, null);
                    using (WaitHandle connected = connect.AsyncWaitHandle)
                    {
                        int result = WaitHandle.WaitAny(new WaitHandle[] { connected, predictionStop }, 3000);
                        if (result != 0) throw new IOException("Prediction connection timed out or was cancelled.");
                        client.EndConnect(connect);
                    }
                    using (NetworkStream stream = client.GetStream())
                    using (var reader = new StreamReader(stream, Encoding.UTF8))
                    using (var writer = new StreamWriter(stream, new UTF8Encoding(false)) { AutoFlush = true, NewLine = "\n" })
                    {
                        PredictionRequest(writer, reader, "{\"schema_version\":2,\"type\":\"HELLO\",\"instrument\":"
                            + PredictionJsonString(predictionInstrument) + ",\"symbol\":" + PredictionJsonString(predictionSymbol)
                            + ",\"bar_minutes\":5,\"mode\":\"live\",\"history_policy\":" + PredictionJsonString(predictionHistoryPolicy)
                            + ",\"feed_label\":" + PredictionJsonString(predictionFeedLabel) + "}", helloReply: true);
                        List<PredictionBar> history;
                        lock (predictionLock)
                            history = predictionHistory.Values.Where(bar => bar.EndUtc <= DateTime.UtcNow).ToList();
                        for (int offset = 0; offset < history.Count && predictionRunning; offset += 256)
                            PredictionRequest(writer, reader, PredictionBatchJson(history.GetRange(offset, Math.Min(256, history.Count - offset)), "history"));
                        PredictionRequest(writer, reader, "{\"schema_version\":2,\"type\":\"HISTORY_END\"}", true);
                        while (predictionRunning)
                        {
                            List<PredictionBar> live;
                            lock (predictionLock)
                            {
                                live = predictionLiveQueue.Where(bar => (DateTime.UtcNow - bar.EndUtc).TotalSeconds <= 90).ToList();
                                while (predictionLiveQueue.Count > 0 && (DateTime.UtcNow - predictionLiveQueue.Peek().EndUtc).TotalSeconds > 90)
                                    predictionLiveQueue.Dequeue();
                            }
                            PredictionRequest(writer, reader, live.Count > 0
                                ? PredictionBatchJson(live, "live")
                                : "{\"schema_version\":2,\"type\":\"PING\"}");
                            if (live.Count > 0)
                            {
                                // Keep an unacknowledged bar queued across a
                                // reconnect, provided it is still genuinely live.
                                DateTime acknowledged = live[live.Count - 1].EndUtc;
                                lock (predictionLock)
                                    while (predictionLiveQueue.Count > 0 && predictionLiveQueue.Peek().EndUtc <= acknowledged)
                                        predictionLiveQueue.Dequeue();
                            }
                            RefreshPredictionChart(); // Expiry also hides during an idle market.
                            if (predictionStop.WaitOne(2000)) break;
                        }
                    }
                }
                catch (Exception ex)
                {
                    if (predictionRunning)
                        SetPredictionStatus("ERROR", "Forecast service unavailable: " + ex.Message, true);
                }
                finally
                {
                    lock (predictionLock)
                    {
                        if (ReferenceEquals(predictionClient, client)) predictionClient = null;
                    }
                    if (client != null) client.Close();
                }
                if (predictionStop.WaitOne(5000)) break;
            }
        }

        private bool PredictionDouble(string json, string key, out double value)
        {
            return double.TryParse(ExtractJsonValue(json, key), NumberStyles.Float, CultureInfo.InvariantCulture, out value)
                && !double.IsNaN(value) && !double.IsInfinity(value);
        }

        private bool PredictionDate(string json, string key, out DateTime value)
        {
            return DateTime.TryParse(ExtractJsonValue(json, key), CultureInfo.InvariantCulture,
                DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal, out value);
        }

        private string PredictionString(string json, string key)
        {
            // Flat v2 metadata can contain JSON escapes (for example a feed
            // label with a quote). Decode it without adding a JSON dependency.
            string marker = "\"" + key + "\":";
            int index = json.IndexOf(marker, StringComparison.Ordinal);
            if (index < 0) return null;
            index += marker.Length;
            while (index < json.Length && char.IsWhiteSpace(json[index])) index++;
            if (index >= json.Length || json[index++] != '"') return null;
            var value = new StringBuilder();
            while (index < json.Length)
            {
                char character = json[index++];
                if (character == '"') return value.ToString();
                if (character != '\\') { value.Append(character); continue; }
                if (index >= json.Length) break;
                char escaped = json[index++];
                switch (escaped)
                {
                    case '"': value.Append('"'); break;
                    case '\\': value.Append('\\'); break;
                    case '/': value.Append('/'); break;
                    case 'b': value.Append('\b'); break;
                    case 'f': value.Append('\f'); break;
                    case 'n': value.Append('\n'); break;
                    case 'r': value.Append('\r'); break;
                    case 't': value.Append('\t'); break;
                    case 'u':
                        ushort code;
                        if (index + 4 > json.Length || !ushort.TryParse(json.Substring(index, 4), NumberStyles.HexNumber, CultureInfo.InvariantCulture, out code))
                            throw new IOException("Invalid escaped forecast metadata.");
                        value.Append((char)code);
                        index += 4;
                        break;
                    default: throw new IOException("Invalid escaped forecast metadata.");
                }
            }
            throw new IOException("Unterminated forecast metadata string.");
        }

        private bool PredictionModelsMatch(string json, bool provisional)
        {
            // Model identifiers are a small fixed protocol vocabulary. This
            // array must describe exactly the model-specific bounds we parse.
            int keyIndex = json.IndexOf("\"available_models\":", StringComparison.Ordinal);
            if (keyIndex < 0) return false;
            int start = keyIndex + "\"available_models\":".Length;
            while (start < json.Length && char.IsWhiteSpace(json[start])) start++;
            if (start >= json.Length || json[start] != '[') return false;
            int end = json.IndexOf(']', start + 1);
            if (end < 0) return false;
            string[] entries = json.Substring(start + 1, end - start - 1).Split(',')
                .Select(item => item.Trim()).ToArray();
            return provisional
                ? entries.Length == 1 && entries[0] == "\"ewma\""
                : entries.Length == 2 && entries.Contains("\"garch\"") && entries.Contains("\"markov\"");
        }

        private void ParsePredictionReply(string json, bool establishSeries, bool helloReply)
        {
            if (ExtractJsonValue(json, "schema_version") != "2")
                throw new IOException("Unsupported prediction protocol version.");
            string type = PredictionString(json, "type");
            string instrument = PredictionString(json, "instrument");
            // A rejected HELLO has no validated contract/series yet. Surface
            // that rejection only in the explicit HELLO phase, without ever
            // accepting its metadata or publishing a forecast.
            if (helloReply && !establishSeries && predictionSeriesId.Length == 0
                && type == "STATUS" && PredictionString(json, "status") == "ERROR"
                && ExtractJsonValue(json, "instrument") == "null"
                && string.IsNullOrEmpty(PredictionString(json, "series_id")))
                throw new IOException("Forecast server rejected HELLO: "
                    + (PredictionString(json, "message") ?? "Unknown validation error."));
            if (!string.Equals(instrument, predictionInstrument, StringComparison.Ordinal))
                throw new IOException("Prediction contract mismatch: chart expects '" + predictionInstrument
                    + "', server returned '" + (instrument ?? "<missing>") + "'.");
            string policy = PredictionString(json, "history_policy");
            string feedLabel = PredictionString(json, "feed_label");
            string seriesId = PredictionString(json, "series_id") ?? "";
            if (!string.Equals(policy, predictionHistoryPolicy, StringComparison.Ordinal)
                || !string.Equals(feedLabel, predictionFeedLabel, StringComparison.Ordinal))
                throw new IOException("Prediction history policy or feed label does not match this chart.");
            if (!establishSeries && predictionSeriesId.Length > 0
                && !string.Equals(seriesId, predictionSeriesId, StringComparison.Ordinal))
                throw new IOException("Prediction history series changed; reconnect and reload history.");
            if (type == "ACK" && !establishSeries) return;
            if (type == "STATUS")
            {
                string status = PredictionString(json, "status") ?? "ERROR";
                DateTime serverTime;
                if (Array.IndexOf(new string[] { "WAITING_FOR_HISTORY", "TRAINING", "WARMING_UP", "STALE", "OUTSIDE_SESSION", "ERROR" }, status) < 0)
                    throw new IOException("Unsupported forecast status.");
                if (!PredictionDate(json, "server_time_utc", out serverTime)
                    || Math.Abs((serverTime - DateTime.UtcNow).TotalSeconds) > 30)
                {
                    SetPredictionStatus("STALE", "Forecast server and chart wall clocks do not match.", true);
                    return;
                }
                SetPredictionStatus(status, PredictionString(json, "message"),
                    status == "STALE" || status == "OUTSIDE_SESSION" || status == "ERROR");
                if (status == "ERROR")
                    throw new IOException(PredictionString(json, "message") ?? "Forecast server rejected the request.");
                if (establishSeries)
                {
                    if (string.IsNullOrWhiteSpace(seriesId))
                        throw new IOException("HISTORY_END did not establish a forecast history series.");
                    lock (predictionLock)
                    {
                        predictionSeriesId = seriesId;
                        predictionFrame = null;
                    }
                }
                return; // TRAINING preserves an already-fresh frame until its own expiry.
            }
            if (helloReply || establishSeries || type != "FORECAST" || PredictionString(json, "status") != "SHADOW"
                || string.IsNullOrWhiteSpace(seriesId) || predictionSeriesId.Length == 0)
                throw new IOException("Invalid prediction message type/status.");
            var frame = new PredictionFrame
            {
                Instrument = instrument, ModelId = PredictionString(json, "model_id") ?? "",
                SeriesId = seriesId, HistoryPolicy = policy, FeedLabel = feedLabel
            };
            frame.SessionKind = PredictionString(json, "session_kind");
            if (frame.SessionKind == null && json.IndexOf("\"session_kind\":", StringComparison.Ordinal) < 0)
                frame.SessionKind = "CASH"; // Older schema-2 forecasts only covered cash hours.
            if (frame.SessionKind != "CASH" && frame.SessionKind != "EXTENDED")
                throw new IOException("Unsupported forecast session kind.");
            string forecastMode = PredictionString(json, "forecast_mode");
            bool legacyFitted = forecastMode == null;
            if (legacyFitted) forecastMode = "FITTED";
            if (forecastMode != "PROVISIONAL" && forecastMode != "FITTED")
                throw new IOException("Unsupported forecast mode.");
            frame.Provisional = forecastMode == "PROVISIONAL";
            frame.ModeReason = PredictionString(json, "mode_reason") ?? (frame.Provisional
                ? "GARCH/Markov fitting as more chart history arrives." : "");
            if (frame.Provisional && string.IsNullOrWhiteSpace(frame.ModeReason))
                frame.ModeReason = "GARCH/Markov fitting as more chart history arrives.";
            if (!legacyFitted)
            {
                string expectedFamily = frame.Provisional ? "EWMA" : "GARCH_MARKOV";
                if (PredictionString(json, "model_family") != expectedFamily
                    || !PredictionModelsMatch(json, frame.Provisional)
                    || !int.TryParse(ExtractJsonValue(json, "training_returns"), NumberStyles.Integer, CultureInfo.InvariantCulture, out frame.TrainingReturns)
                    || !int.TryParse(ExtractJsonValue(json, "training_sessions"), NumberStyles.Integer, CultureInfo.InvariantCulture, out frame.TrainingSessions)
                    || frame.TrainingReturns < 0 || frame.TrainingSessions < 0)
                    throw new IOException("Invalid forecast model or training-sample metadata.");
            }
            DateTime now = DateTime.UtcNow;
            if (!PredictionDate(json, "origin_utc", out frame.OriginUtc)
                || !PredictionDate(json, "generated_at_utc", out frame.GeneratedUtc)
                || !PredictionDate(json, "valid_until_utc", out frame.ValidUntilUtc)
                || frame.OriginUtc > now || frame.GeneratedUtc > now.AddSeconds(30)
                || frame.GeneratedUtc < frame.OriginUtc || frame.ValidUntilUtc <= now
                || frame.ValidUntilUtc > frame.OriginUtc.AddSeconds(390)
                || !PredictionDouble(json, "origin_price", out frame.OriginPrice) || frame.OriginPrice <= 0
                || !PredictionDouble(json, "nominal_coverage", out frame.Coverage) || Math.Abs(frame.Coverage - 0.8) > 0.0001
                || frame.ModelId.Length == 0)
            {
                SetPredictionStatus("STALE", "Forecast is expired or has invalid timing/values.", true);
                return;
            }
            frame.HighVolProbability = double.NaN;
            if (!frame.Provisional && (!PredictionDouble(json, "high_vol_probability", out frame.HighVolProbability)
                || frame.HighVolProbability < 0 || frame.HighVolProbability > 1))
                throw new IOException("Invalid fitted-model high-volatility probability.");
            string[] prefixes = frame.Provisional
                ? new string[] { "ewma_15", "ewma_30" }
                : new string[] { "garch_15", "garch_30", "markov_15", "markov_30" };
            frame.Lower = new double[prefixes.Length];
            frame.Center = new double[prefixes.Length];
            frame.Upper = new double[prefixes.Length];
            for (int i = 0; i < prefixes.Length; i++)
                if (!PredictionDouble(json, prefixes[i] + "_lower", out frame.Lower[i])
                    || !PredictionDouble(json, prefixes[i] + "_center", out frame.Center[i])
                    || !PredictionDouble(json, prefixes[i] + "_upper", out frame.Upper[i])
                    || frame.Lower[i] <= 0 || frame.Lower[i] > frame.Center[i] || frame.Center[i] > frame.Upper[i])
                    throw new IOException("Invalid forecast endpoint bounds.");
            lock (predictionLock)
            {
                // Completing a fit cannot repaint an already published origin.
                // A later origin may upgrade from provisional to fitted normally.
                if (predictionFrame != null && predictionFrame.SeriesId == frame.SeriesId
                    && frame.OriginUtc <= predictionFrame.OriginUtc)
                    return;
                predictionFrame = frame;
                predictionStatus = "SHADOW";
                predictionMessage = "";
            }
            RefreshPredictionChart();
        }

        private void RenderPredictions(ChartScale chartScale)
        {
            if (!EnablePredictions || ChartPanel == null || RenderTarget == null)
                return;
            PredictionFrame frame;
            string status, message, currentSeriesId;
            lock (predictionLock)
            {
                frame = predictionFrame;
                status = predictionStatus;
                message = predictionMessage;
                currentSeriesId = predictionSeriesId;
            }
            DateTime now = DateTime.UtcNow;
            bool usable = predictionRealtime && predictionHistoryPolicyValid && State == State.Realtime && !PredictionPlaybackActive()
                && frame != null && frame.Instrument == predictionInstrument
                && frame.HistoryPolicy == predictionHistoryPolicy && frame.FeedLabel == predictionFeedLabel
                && frame.SeriesId.Length > 0 && frame.SeriesId == currentSeriesId
                && frame.OriginUtc <= now && now < frame.ValidUntilUtc;
            if (!usable && frame != null) { status = "STALE"; message = "Waiting for the next live five-minute close."; }
            if (PredictionPlaybackActive()) { status = "PLAYBACK DISABLED"; message = "Forecasts require a live market feed."; usable = false; }
            if (!predictionRealtime) { status = "LIVE ONLY"; message = "Load available chart history; waiting for a current five-minute bar."; }
            if (!predictionHistoryPolicyValid)
            {
                status = "HISTORY POLICY";
                message = "Merge policy changed; reload historical data.";
                usable = false;
            }
            float x = ChartPanel.X + 10, y = ChartPanel.Y + 32;
            float width = Math.Min(380, ChartPanel.W - 20);
            if (width < 150) return;
            using (var format = new SharpDX.DirectWrite.TextFormat(Core.Globals.DirectWriteFactory, "Arial", 11))
            using (var heading = new SharpDX.DirectWrite.TextFormat(Core.Globals.DirectWriteFactory, "Arial", SharpDX.DirectWrite.FontWeight.Bold, SharpDX.DirectWrite.FontStyle.Normal, 12))
            using (var background = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, new SharpDX.Color(16, 23, 32, 226)))
            using (var text = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, new SharpDX.Color(226, 233, 243, 255)))
            using (var dim = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, new SharpDX.Color(170, 187, 203, 255)))
            {
                // Both modes have a session row; the provisional explanation
                // has three lines of space below its two endpoint rows.
                float panelHeight = usable ? (frame.Provisional ? 204 : 190) : 78;
                RenderTarget.FillRectangle(new SharpDX.RectangleF(x - 5, y - 5, width, panelHeight), background);
                string panelTitle = usable && frame.Provisional ? "PROVISIONAL | limited history"
                    : usable ? "FITTED SHADOW | nominal 80% endpoints" : "FORECAST SHADOW";
                RenderTarget.DrawText(panelTitle, heading, new SharpDX.RectangleF(x, y, width - 10, 18), text);
                y += 20;
                if (!usable)
                {
                    RenderTarget.DrawText(status, format, new SharpDX.RectangleF(x, y, width - 10, 16), text);
                    RenderTarget.DrawText(message, format, new SharpDX.RectangleF(x, y + 17, width - 10, 27), dim);
                    return;
                }
                string sessionLabel = frame.SessionKind == "EXTENDED"
                    ? "EXTENDED HOURS | experimental" : "CASH SESSION";
                RenderTarget.DrawText(sessionLabel, format, new SharpDX.RectangleF(x, y, width - 10, 16), text);
                y += 18;
                RenderTarget.DrawText(frame.Instrument + " | origin " + frame.OriginPrice.ToString("0.00", CultureInfo.InvariantCulture)
                    + " | age " + Math.Max(0, (int)(now - frame.OriginUtc).TotalSeconds) + "s | " + status,
                    format, new SharpDX.RectangleF(x, y, width - 10, 16), dim);
                y += 18;
                string historyLabel = predictionHistoryPolicy == "MergeBackAdjusted" ? "Merged chart history (back adjusted)"
                    : predictionHistoryPolicy == "MergeNonBackAdjusted" ? "Merged chart history (not adjusted)"
                    : "Chart history | " + predictionHistoryPolicy;
                RenderTarget.DrawText(predictionFeedLabel + " | " + historyLabel,
                    format, new SharpDX.RectangleF(x, y, width - 10, 16), dim);
                y += 18;
                string sampleText = frame.TrainingReturns >= 0
                    ? frame.TrainingReturns + " returns | " + frame.TrainingSessions + " sessions | " + (frame.Provisional ? "PROVISIONAL" : "FITTED")
                    : "FITTED | lower / center / upper";
                RenderTarget.DrawText(sampleText, format, new SharpDX.RectangleF(x, y, width - 10, 16), dim);
                y += 18;
                string[] names = frame.Provisional
                    ? new string[] { "EWMA 15m", "EWMA 30m" }
                    : new string[] { "GARCH 15m", "GARCH 30m", "Markov 15m", "Markov 30m" };
                for (int i = 0; i < names.Length; i++)
                {
                    string line = string.Format(CultureInfo.InvariantCulture, "{0,-11} {1:0.00} / {2:0.00} / {3:0.00}", names[i], frame.Lower[i], frame.Center[i], frame.Upper[i]);
                    RenderTarget.DrawText(line, format, new SharpDX.RectangleF(x, y, width - 10, 16), text);
                    y += 17;
                }
                if (frame.Provisional)
                {
                    RenderTarget.DrawText("SHADOW | nominal 80% endpoints | lower / center / upper",
                        format, new SharpDX.RectangleF(x, y + 1, width - 10, 18), dim);
                    RenderTarget.DrawText(frame.ModeReason, format, new SharpDX.RectangleF(x, y + 20, width - 10, 48), dim);
                }
                else
                    RenderTarget.DrawText("Next 5m high-vol: " + frame.HighVolProbability.ToString("P0", CultureInfo.InvariantCulture)
                        + " | lower / center / upper", format, new SharpDX.RectangleF(x, y + 1, width - 10, 18), dim);

                // Fixed-price endpoint lanes for only the available models.
                // These do not imply a continuous path or a calibrated path band.
                // In particular, never apply the GEX/index-to-futures spread here.
                string[] laneNames = frame.Provisional ? new string[] { "E15", "E30" }
                    : new string[] { "G15", "G30", "M15", "M30" };
                float laneWidth = Math.Min(69, (ChartPanel.W - 25) / laneNames.Length);
                float startX = ChartPanel.X + ChartPanel.W - laneNames.Length * (laneWidth + 4) - 6;
                for (int i = 0; i < laneNames.Length; i++)
                {
                    float upperY = chartScale.GetYByValue(frame.Upper[i]);
                    float lowerY = chartScale.GetYByValue(frame.Lower[i]);
                    float centerY = chartScale.GetYByValue(frame.Center[i]);
                    float top = Math.Max(ChartPanel.Y, upperY);
                    float bottom = Math.Min(ChartPanel.Y + ChartPanel.H, lowerY);
                    float laneX = startX + i * (laneWidth + 4);
                    if (bottom <= top) continue;
                    SharpDX.Color color = i < 2 ? new SharpDX.Color(99, 190, 246, 185) : new SharpDX.Color(237, 183, 87, 185);
                    SharpDX.Color fillColor = i < 2 ? new SharpDX.Color(99, 190, 246, 23) : new SharpDX.Color(237, 183, 87, 23);
                    using (var fill = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, fillColor))
                    using (var stroke = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, color))
                    {
                        RenderTarget.FillRectangle(new SharpDX.RectangleF(laneX, top, laneWidth, bottom - top), fill);
                        RenderTarget.DrawRectangle(new SharpDX.RectangleF(laneX, top, laneWidth, bottom - top), stroke, 1);
                        if (centerY >= ChartPanel.Y && centerY <= ChartPanel.Y + ChartPanel.H)
                            RenderTarget.DrawLine(new SharpDX.Vector2(laneX, centerY), new SharpDX.Vector2(laneX + laneWidth, centerY), stroke, 1.5f);
                        RenderTarget.DrawText(laneNames[i], format, new SharpDX.RectangleF(laneX + 2, top + 2, laneWidth - 4, 16), stroke);
                    }
                }
            }
        }
        #endregion

        #region TCP Client
        private void StartListener()
        {
            if (listenerThread != null && listenerThread.IsAlive)
                return;

            stopRequested.Reset();
            isRunning = true;
            listenerThread = new Thread(ClientLoop)
            {
                IsBackground = true,
                Name = "OpenGamma_TCPClient"
            };
            listenerThread.Start();
            Print("OpenGamma: Starting TCP Client...");
        }

        private void StopListener()
        {
            isRunning = false;
            stopRequested.Set();

            TcpClient clientToClose = null;
            lock (clientLock)
            {
                clientToClose = activeClient;
                activeClient = null;
            }
            if (clientToClose != null)
                clientToClose.Close();

            if (listenerThread != null && listenerThread.IsAlive)
                listenerThread.Join(3000);
            listenerThread = null;

            Print("OpenGamma: TCP Client stopped");
        }

        private void ClientLoop()
        {
            while (isRunning)
            {
                TcpClient client = null;
                try
                {
                    // Attempt to connect to Python Server
                    client = new TcpClient();
                    lock (clientLock)
                    {
                        activeClient = client;
                    }
                    client.Connect(IPAddress.Loopback, ListenPort);

                    Print($"OpenGamma: Connected to Server on port {ListenPort}");

                    using (NetworkStream stream = client.GetStream())
                    using (StreamReader reader = new StreamReader(stream, Encoding.UTF8))
                    {
                        while (isRunning)
                        {
                            try
                            {
                                string line = reader.ReadLine();
                                if (line == null) break; // End of stream

                                if (!string.IsNullOrEmpty(line))
                                {
                                    ParseRegimeUpdate(line);
                                }
                            }
                            catch (IOException)
                            {
                                break; // Stream error, reconnect
                            }
                        }
                    }
                }
                catch (Exception ex)
                {
                    // Connection failed or lost
                    if (isRunning)
                    {
                        // Print("OpenGamma: Connection failed/lost - " + ex.Message);
                        // Access denied or refuse usually means server not up.
                        // Wait before retry.
                    }
                }
                finally
                {
                    client?.Close();
                    lock (clientLock)
                    {
                        if (ReferenceEquals(activeClient, client))
                            activeClient = null;
                    }
                }

                // Retry delay
                if (isRunning && stopRequested.WaitOne(5000))
                    break;
            }
        }
        #endregion

        private void ParseRegimeUpdate(string json)
        {
            try
            {
                string newRegime = ExtractJsonValue(json, "regime") ?? "UNKNOWN";

                lock (lockObj)
                {
                    // Track previous regime
                    if (currentRegime != "WAITING" && currentRegime != newRegime)
                    {
                        previousRegime = currentRegime;
                    }

                    currentRegime = newRegime;

                    int.TryParse(ExtractJsonValue(json, "regime_code"), NumberStyles.Integer, CultureInfo.InvariantCulture, out int code);
                    regimeCode = code;

                    lastUpdate = DateTime.Now.ToString("HH:mm:ss");

                    // Get index price from broadcast payload (not from NinjaTrader)
                    if (indexSymbol == "NDX")
                    {
                        double.TryParse(ExtractJsonValue(json, "spot_ndx"), NumberStyles.Float, CultureInfo.InvariantCulture, out double ndx);
                        indexPrice = ndx;
                    }
                    else if (indexSymbol == "SPX")
                    {
                        double.TryParse(ExtractJsonValue(json, "spot_spx"), NumberStyles.Float, CultureInfo.InvariantCulture, out double spx);
                        indexPrice = spx;
                    }

                    if (indexSymbol == "NDX")
                    {
                        double.TryParse(ExtractJsonValue(json, "accel_ndx"), NumberStyles.Float, CultureInfo.InvariantCulture, out double accel);
                        acceleration = accel;
                    }
                    else if (indexSymbol == "SPX")
                    {
                        double.TryParse(ExtractJsonValue(json, "accel_spx"), NumberStyles.Float, CultureInfo.InvariantCulture, out double accel);
                        acceleration = accel;
                    }

                    string dashValue = ExtractDashboardValue(json, "dashboard_symbol");
                    if (dashValue != null)
                        dashboardSymbol = string.IsNullOrEmpty(dashValue) ? indexSymbol : dashValue;
                    dashValue = ExtractDashboardValue(json, "dashboard_bias");
                    if (dashValue != null)
                        dashboardBias = dashValue;
                    dashValue = ExtractDashboardValue(json, "dashboard_context");
                    if (dashValue != null)
                        dashboardContext = dashValue;
                    dashValue = ExtractDashboardValue(json, "dashboard_market");
                    if (dashValue != null)
                        dashboardMarket = dashValue;
                    dashValue = ExtractDashboardValue(json, "dashboard_dealer");
                    if (dashValue != null)
                        dashboardDealer = dashValue;
                    dashValue = ExtractDashboardValue(json, "dashboard_liquidity");
                    if (dashValue != null)
                        dashboardLiquidity = dashValue;
                    dashValue = ExtractDashboardValue(json, "dashboard_index_basket");
                    if (dashValue == null)
                        dashValue = ExtractDashboardValue(json, "dashboard_whale");
                    if (dashValue != null)
                        dashboardWhale = dashValue;
                    dashValue = ExtractDashboardValue(json, "dashboard_edge_summary");
                    if (dashValue != null)
                        dashboardEdgeSummary = dashValue;
                    dashValue = ExtractDashboardValue(json, "dashboard_edge_source");
                    if (dashValue != null)
                        dashboardEdgeSource = dashValue;

                    if (TryExtractDashboardDouble(json, "dashboard_bias_score", out double biasScore))
                        dashboardBiasScore = biasScore;
                    if (TryExtractDashboardDouble(json, "dashboard_data_quality", out double confidenceScore)
                        || TryExtractDashboardDouble(json, "dashboard_confidence", out confidenceScore))
                        dashboardConfidence = confidenceScore;
                    if (TryExtractDashboardDouble(json, "dashboard_target", out double target))
                        dashboardTarget = target;
                    if (TryExtractDashboardDouble(json, "dashboard_invalidation", out double invalidation))
                        dashboardInvalidation = invalidation;
                    if (TryExtractDashboardDouble(json, "dashboard_flip", out double flip))
                        dashboardFlip = flip;
                    if (TryExtractDashboardDouble(json, "dashboard_modeled_zero_gex", out double modeledZero))
                        modeledZeroGex = modeledZero;
                    if (TryExtractDashboardDouble(json, "dashboard_edge_win_rate", out double edgeWinRate))
                        dashboardEdgeWinRate = edgeWinRate;
                    if (TryExtractDashboardDouble(json, "dashboard_edge_median_move", out double edgeMedianMove))
                        dashboardEdgeMedianMove = edgeMedianMove;
                    if (TryExtractDashboardDouble(json, "dashboard_edge_sample", out double edgeSample))
                        dashboardEdgeSample = edgeSample;
                }

                // Capture futures price and calc spread on UI thread
                string jsonCopy = json;
                Action processChartUpdate = () =>
                {
                    try
                    {
                        bool levelsSeen;
                        int levelCount;
                        lock (lockObj)
                        {
                            // Use cached close price from OnBarUpdate when it is available.
                            // The incoming dashboard payload should still update levels/cache even
                            // if NinjaTrader has not delivered a chart price yet.
                            if (lastClosePrice > 0)
                                futuresPrice = lastClosePrice;

                            // Feed the Jurik average from the configured secondary
                            // minute series rather than the primary chart series.
                            if (jmaClosePrice > 0 && indexPrice > 0)
                            {
                                rawSpread = indexPrice - jmaClosePrice;
                                spread = UpdateJmaSpread(rawSpread);
                            }

                            // Parse and adjust gamma levels on every valid payload, using the
                            // latest known spread when the current chart price is not ready yet.
                            levelsSeen = ParseGammaLevels(jsonCopy);
                            levelCount = gammaLevels.Count;

                            Print($"OpenGamma: {currentRegime} | {indexSymbol}: {Math.Round(indexPrice)} | Futures: {Math.Round(futuresPrice)} | Raw Spread: {rawSpread:F2} | JMA Spread: {spread:F2} | Levels: {(levelsSeen ? levelCount.ToString() : "unchanged")}");
                        }

                        if (ChartControl != null)
                            ForceRefresh();
                    }
                    catch (Exception ex)
                    {
                        Print("OpenGamma: Failed to process update - " + ex.Message);
                    }
                };

                if (ChartControl != null)
                {
                    ChartControl.Dispatcher.InvokeAsync(processChartUpdate);
                }
                else
                {
                    processChartUpdate();
                }
            }
            catch (Exception ex)
            {
                Print("OpenGamma: Parse error - " + ex.Message);
            }
        }

        private string ExtractDashboardValue(string json, string key)
        {
            if (!string.IsNullOrEmpty(indexSymbol))
            {
                string symbolValue = ExtractJsonValue(json, key + "_" + indexSymbol.ToLowerInvariant());
                if (symbolValue != null)
                    return symbolValue;
            }

            return ExtractJsonValue(json, key);
        }

        private bool TryExtractDashboardDouble(string json, string key, out double result)
        {
            result = double.NaN;
            string value = ExtractDashboardValue(json, key);
            if (value == null)
                return false;

            if (string.Equals(value, "null", StringComparison.OrdinalIgnoreCase) || value.Length == 0)
                return true;

            if (double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out result))
                return true;

            result = double.NaN;
            return true;
        }

        private string ExtractJsonValue(string json, string key)
        {
            string pattern = $"\"{key}\":";
            int startIdx = json.IndexOf(pattern);
            if (startIdx < 0) return null;

            startIdx += pattern.Length;

            while (startIdx < json.Length && char.IsWhiteSpace(json[startIdx]))
                startIdx++;

            if (startIdx >= json.Length) return null;

            if (json[startIdx] == '"')
            {
                int endIdx = json.IndexOf('"', startIdx + 1);
                if (endIdx < 0) return null;
                return json.Substring(startIdx + 1, endIdx - startIdx - 1);
            }
            else
            {
                int endIdx = startIdx;
                while (endIdx < json.Length && json[endIdx] != ',' && json[endIdx] != '}')
                    endIdx++;
                return json.Substring(startIdx, endIdx - startIdx).Trim();
            }
        }

        private bool ParseGammaLevels(string json)
        {
            string levelsKey = indexSymbol == "NDX" ? "gamma_levels_ndx" : "gamma_levels_spx";

            // Find key manually but robustly
            int keyIdx = json.IndexOf("\"" + levelsKey + "\"");
            if (keyIdx < 0)
            {
               Print($"OpenGamma: Key '{levelsKey}' not found in JSON.");
               return false;
            }

            // Find start of array value [
            int arrStart = json.IndexOf('[', keyIdx);
            if (arrStart < 0) return false;

            // Find matching closing bracket ]
            int arrEnd = -1;
            int bracketCount = 0;
            for (int i = arrStart; i < json.Length; i++)
            {
                if (json[i] == '[') bracketCount++;
                else if (json[i] == ']')
                {
                    bracketCount--;
                    if (bracketCount == 0)
                    {
                        arrEnd = i;
                        break;
                    }
                }
            }

            if (arrEnd < 0) return false;

            string arrContent = json.Substring(arrStart + 1, arrEnd - arrStart - 1);
            gammaLevels.Clear();
            if (string.IsNullOrWhiteSpace(arrContent))
            {
                Print($"OpenGamma: Parsed 0 levels for {levelsKey}");
                SaveGammaCache();
                return true;
            }

            // Parse individual objects {...}
            int objStart = 0;
            int braceCount = 0;
            int currentStart = -1;

            for (int i = 0; i < arrContent.Length; i++)
            {
                if (arrContent[i] == '{')
                {
                    if (braceCount == 0) currentStart = i;
                    braceCount++;
                }
                else if (arrContent[i] == '}')
                {
                    braceCount--;
                    if (braceCount == 0 && currentStart >= 0)
                    {
                        string objJson = arrContent.Substring(currentStart, i - currentStart + 1);
                        ParseGammaLevelObject(objJson);
                        currentStart = -1;
                    }
                }
            }

            int keyCount = gammaLevels.Count(l => l.IsKeyLevel);
            Print($"OpenGamma: Parsed {gammaLevels.Count} levels for {levelsKey} ({keyCount} key, {gammaLevels.Count - keyCount} hollow)");
            SaveGammaCache();
            return true;
        }

        private void ParseGammaLevelObject(string objJson)
        {
            double strike = 0, gex = 0;

            try
            {
                // Robust value extraction
                strike = ExtractNum(objJson, "strike");
                gex = ExtractNum(objJson, "gex");
                bool isKeyLevel = ExtractBool(objJson, "is_key_level", true);

                if (strike > 0)
                {
                    double futuresLevel = strike - spread;

                    gammaLevels.Add(new GammaLevel
                    {
                        Strike = strike,
                        Gex = gex,
                        FuturesPrice = futuresLevel,
                        IsResistance = gex > 0,
                        IsKeyLevel = isKeyLevel
                    });
                }
            }
            catch (Exception ex) { Print("OpenGamma: Error parsing level obj: " + ex.Message); }
        }

        private double UpdateJmaSpread(double value)
        {
            if (double.IsNaN(value) || double.IsInfinity(value))
                return spread;

            if (!jmaSpreadInitialized)
            {
                ResetJmaSpread(value);
                return jmaSpreadValue;
            }

            double resetThreshold = Math.Max(0, JmaResetThreshold);
            if (resetThreshold > 0 && Math.Abs(value - jmaSpreadValue) > resetThreshold)
            {
                ResetJmaSpread(value);
                return jmaSpreadValue;
            }

            int length = Math.Max(1, JmaLength);
            int phase = Math.Max(-100, Math.Min(100, JmaPhase));
            double power = Math.Max(1, JmaPower);

            double phaseRatio = phase / 100.0 + 1.5;
            double beta = 0.45 * (length - 1) / (0.45 * (length - 1) + 2.0);
            double alpha = Math.Pow(beta, power);
            double oneMinusAlpha = 1.0 - alpha;

            jmaSpreadE0 = oneMinusAlpha * value + alpha * jmaSpreadE0;
            jmaSpreadE1 = (value - jmaSpreadE0) * (1.0 - beta) + beta * jmaSpreadE1;
            jmaSpreadE2 = (jmaSpreadE0 + phaseRatio * jmaSpreadE1 - jmaSpreadValue)
                * oneMinusAlpha * oneMinusAlpha
                + alpha * alpha * jmaSpreadE2;
            jmaSpreadValue += jmaSpreadE2;

            return jmaSpreadValue;
        }

        private void ResetJmaSpread(double value)
        {
            jmaSpreadInitialized = true;
            jmaSpreadValue = value;
            jmaSpreadE0 = value;
            jmaSpreadE1 = 0;
            jmaSpreadE2 = 0;
        }

        private double ExtractNum(string json, string key)
        {
            int keyIdx = json.IndexOf("\"" + key + "\"");
            if (keyIdx < 0) return 0;

            int valStart = keyIdx + key.Length + 3; // quote + key + quote + colon
            while (valStart < json.Length && !char.IsDigit(json[valStart]) && json[valStart] != '-' && json[valStart] != '.')
                valStart++;

            if (valStart >= json.Length) return 0;

            int valEnd = valStart;
            while (valEnd < json.Length && (char.IsDigit(json[valEnd]) || json[valEnd] == '.' || json[valEnd] == '-' || json[valEnd] == 'e' || json[valEnd] == 'E' || json[valEnd] == '+'))
                valEnd++;

            if (double.TryParse(json.Substring(valStart, valEnd - valStart), NumberStyles.Float, CultureInfo.InvariantCulture, out double result))
                return result;

            return 0;
        }

        private bool ExtractBool(string json, string key, bool defaultValue)
        {
            int keyIdx = json.IndexOf("\"" + key + "\"");
            if (keyIdx < 0) return defaultValue;

            int valStart = keyIdx + key.Length + 3; // quote + key + quote + colon
            while (valStart < json.Length && char.IsWhiteSpace(json[valStart]))
                valStart++;

            if (valStart >= json.Length) return defaultValue;

            string token;
            if (json[valStart] == '"')
            {
                int endIdx = json.IndexOf('"', valStart + 1);
                if (endIdx < 0) return defaultValue;
                token = json.Substring(valStart + 1, endIdx - valStart - 1);
            }
            else
            {
                int valEnd = valStart;
                while (valEnd < json.Length && json[valEnd] != ',' && json[valEnd] != '}')
                    valEnd++;
                token = json.Substring(valStart, valEnd - valStart).Trim();
            }

            if (string.Equals(token, "true", StringComparison.OrdinalIgnoreCase) || token == "1")
                return true;
            if (string.Equals(token, "false", StringComparison.OrdinalIgnoreCase) || token == "0")
                return false;

            return defaultValue;
        }

        private string GetCachePath()
        {
            string instrumentName = "unknown";
            if (Instrument != null && Instrument.MasterInstrument != null)
                instrumentName = Instrument.MasterInstrument.Name;

            string cacheKey = $"{Name}_{instrumentName}_{indexSymbol}_{ListenPort}".Replace(" ", "_");
            foreach (char invalidChar in Path.GetInvalidFileNameChars())
                cacheKey = cacheKey.Replace(invalidChar, '_');

            string cacheDir = Path.Combine(Core.Globals.UserDataDir, CacheFolderName);
            return Path.Combine(cacheDir, cacheKey + ".txt");
        }

        private void SaveGammaCache()
        {
            try
            {
                if (string.IsNullOrEmpty(indexSymbol))
                    return;

                string cachePath = GetCachePath();

                if (gammaLevels.Count == 0)
                {
                    if (File.Exists(cachePath))
                        File.Delete(cachePath);
                    return;
                }

                Directory.CreateDirectory(Path.GetDirectoryName(cachePath));

                using (StreamWriter writer = new StreamWriter(cachePath, false, Encoding.UTF8))
                {
                    writer.WriteLine(CacheVersion);
                    writer.WriteLine(string.Join("|",
                        indexSymbol,
                        DateTime.Now.ToString("O", CultureInfo.InvariantCulture),
                        indexPrice.ToString("R", CultureInfo.InvariantCulture),
                        futuresPrice.ToString("R", CultureInfo.InvariantCulture),
                        spread.ToString("R", CultureInfo.InvariantCulture),
                        acceleration.ToString("R", CultureInfo.InvariantCulture),
                        currentRegime,
                        previousRegime,
                        regimeCode.ToString(CultureInfo.InvariantCulture),
                        modeledZeroGex.ToString("R", CultureInfo.InvariantCulture)));

                    foreach (var level in gammaLevels)
                    {
                        writer.WriteLine(string.Join(",",
                            level.Strike.ToString("R", CultureInfo.InvariantCulture),
                            level.Gex.ToString("R", CultureInfo.InvariantCulture),
                            level.FuturesPrice.ToString("R", CultureInfo.InvariantCulture),
                            level.IsResistance ? "1" : "0",
                            level.IsKeyLevel ? "1" : "0"));
                    }
                }
            }
            catch (Exception ex)
            {
                Print("OpenGamma: Failed to save level cache - " + ex.Message);
            }
        }

        private void LoadCachedGammaLevels()
        {
            try
            {
                if (string.IsNullOrEmpty(indexSymbol))
                    return;

                string cachePath = GetCachePath();
                if (!File.Exists(cachePath))
                    return;

                string[] lines = File.ReadAllLines(cachePath, Encoding.UTF8);
                if (lines.Length < 3 || lines[0] != CacheVersion)
                    return;

                string[] header = lines[1].Split('|');
                if (header.Length < 9 || header[0] != indexSymbol)
                    return;

                var cachedLevels = new List<GammaLevel>();
                for (int i = 2; i < lines.Length; i++)
                {
                    if (string.IsNullOrWhiteSpace(lines[i]))
                        continue;

                    string[] parts = lines[i].Split(',');
                    if (parts.Length < 4)
                        continue;

                    if (!double.TryParse(parts[0], NumberStyles.Float, CultureInfo.InvariantCulture, out double strike))
                        continue;
                    if (!double.TryParse(parts[1], NumberStyles.Float, CultureInfo.InvariantCulture, out double gex))
                        continue;
                    if (!double.TryParse(parts[2], NumberStyles.Float, CultureInfo.InvariantCulture, out double futuresLevel))
                        continue;

                    cachedLevels.Add(new GammaLevel
                    {
                        Strike = strike,
                        Gex = gex,
                        FuturesPrice = futuresLevel,
                        IsResistance = parts[3] == "1",
                        IsKeyLevel = parts.Length < 5 || parts[4] == "1"
                    });
                }

                if (cachedLevels.Count == 0)
                    return;

                lock (lockObj)
                {
                    double.TryParse(header[2], NumberStyles.Float, CultureInfo.InvariantCulture, out indexPrice);
                    double.TryParse(header[3], NumberStyles.Float, CultureInfo.InvariantCulture, out futuresPrice);
                    double.TryParse(header[4], NumberStyles.Float, CultureInfo.InvariantCulture, out spread);
                    rawSpread = spread;
                    ResetJmaSpread(spread);
                    double.TryParse(header[5], NumberStyles.Float, CultureInfo.InvariantCulture, out acceleration);
                    currentRegime = string.IsNullOrEmpty(header[6]) ? "CACHED" : header[6];
                    previousRegime = string.IsNullOrEmpty(header[7]) ? previousRegime : header[7];
                    int.TryParse(header[8], NumberStyles.Integer, CultureInfo.InvariantCulture, out regimeCode);
                    if (header.Length >= 10)
                        double.TryParse(header[9], NumberStyles.Float, CultureInfo.InvariantCulture, out modeledZeroGex);
                    lastUpdate = "cached";
                    gammaLevels = cachedLevels
                        .OrderBy(l => l.FuturesPrice)
                        .ToList();
                }

                Print($"OpenGamma: Loaded {cachedLevels.Count} cached levels from {cachePath}");
            }
            catch (Exception ex)
            {
                Print("OpenGamma: Failed to load level cache - " + ex.Message);
            }
        }

        protected override void OnBarUpdate()
        {
            if (BarsInProgress == PredictionSeriesIndex)
            {
                CapturePredictionBar(DateTime.UtcNow);
                return;
            }
            lock (lockObj)
            {
                if (BarsInProgress == 0 && CurrentBars[0] >= 0)
                    lastClosePrice = Closes[0][0];
                else if (BarsInProgress == 1 && CurrentBars[1] >= 0)
                    jmaClosePrice = Closes[1][0];
            }
        }

        protected override void OnRender(ChartControl chartControl, ChartScale chartScale)
        {
            base.OnRender(chartControl, chartScale);

            if (chartControl == null) return;

            // Get current state thread-safely
            string regime, prevRegime, update, idxSym, dashSymbol, dashBias, dashContext, dashMarket, dashDealer, dashLiquidity, dashWhale, dashEdgeSummary, dashEdgeSource;
            int code;
            double idx, fut, sprd, accel, dashScore, dashConfidence, dashTarget, dashInvalidation, dashFlip, modeledZero, dashEdgeWinRate, dashEdgeMedianMove, dashEdgeSample;

            lock (lockObj)
            {
                regime = currentRegime;
                prevRegime = previousRegime;
                code = regimeCode;
                update = lastUpdate;
                idx = indexPrice;
                fut = futuresPrice;
                sprd = spread;
                accel = acceleration;
                idxSym = indexSymbol;
                dashSymbol = dashboardSymbol;
                dashBias = dashboardBias;
                dashScore = dashboardBiasScore;
                dashConfidence = dashboardConfidence;
                dashTarget = dashboardTarget;
                dashInvalidation = dashboardInvalidation;
                dashFlip = dashboardFlip;
                modeledZero = modeledZeroGex;
                dashContext = dashboardContext;
                dashMarket = dashboardMarket;
                dashDealer = dashboardDealer;
                dashLiquidity = dashboardLiquidity;
                dashWhale = dashboardWhale;
                dashEdgeSummary = dashboardEdgeSummary;
                dashEdgeSource = dashboardEdgeSource;
                dashEdgeWinRate = dashboardEdgeWinRate;
                dashEdgeMedianMove = dashboardEdgeMedianMove;
                dashEdgeSample = dashboardEdgeSample;
            }

            bool drawLegacyPanel = false;
            if (drawLegacyPanel)
            {
            // Panel dimensions
            float panelWidth = 180;
            float panelHeight = 100; // Increased height
            float panelX = ChartPanel.X + ChartPanel.W - panelWidth - 10;
            float panelY = ChartPanel.Y + ChartPanel.H - panelHeight - 10;
            float lineHeight = 16;
            float textY = panelY + 5;

            using (SharpDX.DirectWrite.TextFormat titleFormat = new SharpDX.DirectWrite.TextFormat(
                Core.Globals.DirectWriteFactory, "Arial", SharpDX.DirectWrite.FontWeight.Bold,
                SharpDX.DirectWrite.FontStyle.Normal, 14))
            using (SharpDX.DirectWrite.TextFormat textFormat = new SharpDX.DirectWrite.TextFormat(
                Core.Globals.DirectWriteFactory, "Arial", 11))
            {
                // Background panel
                SharpDX.RectangleF panelRect = new SharpDX.RectangleF(panelX - 5, panelY - 5, panelWidth, panelHeight);
                using (SharpDX.Direct2D1.SolidColorBrush panelBrush = new SharpDX.Direct2D1.SolidColorBrush(
                    RenderTarget, new SharpDX.Color(20, 20, 30, 220)))
                {
                    RenderTarget.FillRectangle(panelRect, panelBrush);
                }

                // Current Regime (colored)
                SharpDX.Color regimeColor = code switch
                {
                    1 => new SharpDX.Color(0, 255, 0, 255),     // Green
                    2 => new SharpDX.Color(255, 215, 0, 255),   // Gold
                    3 => new SharpDX.Color(180, 180, 180, 255), // Gray
                    4 => new SharpDX.Color(220, 20, 60, 255),   // Red
                    _ => new SharpDX.Color(180, 180, 180, 255)
                };

                using (SharpDX.Direct2D1.SolidColorBrush textBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, regimeColor))
                {
                    RenderTarget.DrawText($"◉ {regime}", titleFormat,
                        new SharpDX.RectangleF(panelX, textY, panelWidth - 10, 18), textBrush);
                }
                textY += lineHeight + 2;

                // Previous Regime (dimmed)
                using (SharpDX.Direct2D1.SolidColorBrush textBrush = new SharpDX.Direct2D1.SolidColorBrush(
                    RenderTarget, new SharpDX.Color(120, 120, 120, 255)))
                {
                    RenderTarget.DrawText($"Prev: {prevRegime}", textFormat,
                        new SharpDX.RectangleF(panelX, textY, panelWidth - 10, 16), textBrush);
                }
                textY += lineHeight;

                // Index price and spread
                if (!string.IsNullOrEmpty(idxSym) && idx > 0)
                {
                    using (SharpDX.Direct2D1.SolidColorBrush textBrush = new SharpDX.Direct2D1.SolidColorBrush(
                        RenderTarget, new SharpDX.Color(200, 200, 200, 255)))
                    {
                        RenderTarget.DrawText($"{idxSym}: {idx:F0}", textFormat,
                            new SharpDX.RectangleF(panelX, textY, panelWidth - 10, 16), textBrush);
                    }
                    textY += lineHeight;

                    // Spread with color
                    SharpDX.Color spreadColor = sprd >= 0
                        ? new SharpDX.Color(100, 200, 255, 255)  // Blue for positive
                        : new SharpDX.Color(255, 150, 100, 255); // Orange for negative

                    using (SharpDX.Direct2D1.SolidColorBrush textBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, spreadColor))
                    {
                        string spreadSign = sprd >= 0 ? "+" : "";
                        RenderTarget.DrawText($"Spread: {spreadSign}{sprd:F0}", textFormat,
                            new SharpDX.RectangleF(panelX, textY, panelWidth - 10, 16), textBrush);
                    }
                    textY += lineHeight;

                    // --- Gamma Bias (BULLISH/BEARISH) based on SLOPE ---
                    // Calculate the slope of the Net Gamma Curve above and below spot
                    // Slope = change in GEX / change in price

                    string gammaBias = "NEUTRAL";
                    SharpDX.Color biasColor = new SharpDX.Color(128, 128, 128, 255);

                    List<GammaLevel> levelsForBias;
                    lock (lockObj)
                    {
                        levelsForBias = gammaLevels.Where(l => l.IsKeyLevel).ToList();
                    }

                    if (levelsForBias.Count >= 4 && fut > 0)
                    {
                        // Sort by price ascending
                        var sortedLevels = levelsForBias.OrderBy(l => l.FuturesPrice).ToList();

                        // Find levels above and below spot
                        var levelsAbove = sortedLevels.Where(l => l.FuturesPrice > fut).Take(3).ToList();
                        var levelsBelow = sortedLevels.Where(l => l.FuturesPrice <= fut).Reverse().Take(3).ToList();

                        // Calculate slope ABOVE spot (how curve changes as price goes UP)
                        double slopeAbove = 0;
                        if (levelsAbove.Count >= 2)
                        {
                            double dGex = levelsAbove[1].Gex - levelsAbove[0].Gex;
                            double dPrice = levelsAbove[1].FuturesPrice - levelsAbove[0].FuturesPrice;
                            if (dPrice != 0) slopeAbove = dGex / dPrice;
                        }

                        // Calculate slope BELOW spot (how curve changes as price goes DOWN)
                        double slopeBelow = 0;
                        if (levelsBelow.Count >= 2)
                        {
                            // levelsBelow[0] is closest to spot, [1] is further down
                            double dGex = levelsBelow[0].Gex - levelsBelow[1].Gex;
                            double dPrice = levelsBelow[0].FuturesPrice - levelsBelow[1].FuturesPrice;
                            if (dPrice != 0) slopeBelow = dGex / dPrice;
                        }

                        // Interpretation:
                        // slopeAbove > 0: Curve rising as price goes up = MORE resistance above = BEARISH
                        // slopeAbove < 0: Curve falling as price goes up = LESS resistance above = BULLISH
                        // slopeBelow > 0: Curve rising as price goes down = MORE support below = BULLISH
                        // slopeBelow < 0: Curve falling as price goes down = LESS support below = BEARISH

                        // Net bias: positive = bullish, negative = bearish
                        double netBias = -slopeAbove + slopeBelow;

                        if (netBias > 0)
                        {
                            gammaBias = "▲ BULLISH";
                            biasColor = new SharpDX.Color(80, 255, 80, 255);
                        }
                        else if (netBias < 0)
                        {
                            gammaBias = "▼ BEARISH";
                            biasColor = new SharpDX.Color(255, 80, 80, 255);
                        }
                    }

                    using (SharpDX.Direct2D1.SolidColorBrush textBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, biasColor))
                    {
                        RenderTarget.DrawText($"Gamma: {gammaBias}", textFormat,
                            new SharpDX.RectangleF(panelX, textY, panelWidth - 10, 16), textBrush);
                    }
                }
            }

            }

            // Draw gamma S/R horizontal lines
            List<GammaLevel> levelsCopy;
            lock (lockObj)
            {
                levelsCopy = new List<GammaLevel>(gammaLevels);
            }

            if (levelsCopy.Count > 0)
            {
                bool drawOnRight = GammaBarsOnRight;

                // Find Max GEX for scaling
                double maxGex = 0;
                foreach (var level in levelsCopy)
                    maxGex = Math.Max(maxGex, Math.Abs(level.Gex));

                if (maxGex == 0) maxGex = 1; // Prevent divide by zero

                using (SharpDX.DirectWrite.TextFormat labelFormat = new SharpDX.DirectWrite.TextFormat(
                    Core.Globals.DirectWriteFactory, "Arial", 10))
                {
                    foreach (var level in levelsCopy.OrderBy(l => l.IsKeyLevel ? 1 : 0))
                    {
                        // Convert price to Y coordinate
                        float y = chartScale.GetYByValue(level.FuturesPrice);

                        // Height of the bar (fixed pixel height, e.g., 20px centered)
                        float height = 20;
                        float yTop = y - height / 2;

                        if (y < ChartPanel.Y || y > ChartPanel.Y + ChartPanel.H)
                        {
                            // Log only the first few to avoid spam
                            if (levelsCopy.IndexOf(level) < 3)
                                Print($"OpenGamma Render: Level {level.FuturesPrice} (Y={y}) is off-screen (Panel: {ChartPanel.Y}-{ChartPanel.Y+ChartPanel.H})");
                            continue;  // Skip if off-screen
                        }

                        // Calculate width based on GEX magnitude (max 40% of screen)
                        // Use Math.Max(10, ...) to ensure very small bars are at least visible
                        float maxWidth = (float)ChartPanel.W * 0.4f;
                        float width = (float)((Math.Abs(level.Gex) / maxGex) * maxWidth);
                        width = Math.Max(width, 10);

                        // Choose color based on S/R type
                        // Use lower opacity for the fill (e.g. 80 alpha out of 255 -> ~0.3)
                        SharpDX.Color barColor = level.IsResistance
                            ? new SharpDX.Color(255, 60, 60, 80)   // Red fill
                            : new SharpDX.Color(60, 255, 60, 80);  // Green fill

                        SharpDX.Color outlineColor = level.IsResistance
                            ? new SharpDX.Color(255, 60, 60, 190)
                            : new SharpDX.Color(60, 255, 60, 190);

                        // Text color (fully opaque)
                        SharpDX.Color textColor = level.IsResistance
                            ? new SharpDX.Color(255, 100, 100, 255)
                            : new SharpDX.Color(100, 255, 100, 255);

                        using (SharpDX.Direct2D1.SolidColorBrush barBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, barColor))
                        using (SharpDX.Direct2D1.SolidColorBrush outlineBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, outlineColor))
                        using (SharpDX.Direct2D1.SolidColorBrush textBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, textColor))
                        {
                            float barX = drawOnRight
                                ? ChartPanel.X + ChartPanel.W - width
                                : ChartPanel.X;
                            SharpDX.RectangleF rect = new SharpDX.RectangleF(barX, yTop, width, height);
                            if (level.IsKeyLevel)
                                RenderTarget.FillRectangle(rect, barBrush);
                            else
                                RenderTarget.DrawRectangle(rect, outlineBrush, 1.5f);

                            // Draw label with strike price
                            string label = $"{level.Strike:F0}";
                            float labelX = drawOnRight
                                ? ChartPanel.X + ChartPanel.W - 85
                                : ChartPanel.X + 5;
                            RenderTarget.DrawText(label, labelFormat,
                                new SharpDX.RectangleF(labelX, yTop + 3, 80, 14), textBrush);
                        }
                    }
                }

                // --- Draw the Net Gamma CURVE (Profile Line) ---
                // Sort levels by price (descending for top-to-bottom drawing)
                var sortedLevels = levelsCopy.OrderByDescending(l => l.FuturesPrice).ToList();

                if (sortedLevels.Count >= 2)
                {
                    // Build curve points
                    var curvePoints = new List<SharpDX.Vector2>();
                    float curveMaxWidth = (float)ChartPanel.W * 0.25f; // Max 25% of chart width
                    float curveBaseX = ChartPanel.X + 5; // Anchor at left edge

                    foreach (var level in sortedLevels)
                    {
                        float y = chartScale.GetYByValue(level.FuturesPrice);

                        // Skip off-screen points but still include to avoid gaps
                        if (y < ChartPanel.Y - 50 || y > ChartPanel.Y + ChartPanel.H + 50)
                            continue;

                        // X position: NetGEX determines how far RIGHT the curve goes
                        // Positive GEX = curve goes further right (resistance)
                        // Negative GEX = curve stays left (support dropping = easy path)
                        float normalizedGex = (float)(level.Gex / maxGex);
                        float xOffset = normalizedGex * curveMaxWidth;

                        curvePoints.Add(new SharpDX.Vector2(curveBaseX + curveMaxWidth + xOffset, y));
                    }

                    // Draw the curve
                    if (curvePoints.Count >= 2)
                    {
                        using (SharpDX.Direct2D1.SolidColorBrush curveBrush = new SharpDX.Direct2D1.SolidColorBrush(
                            RenderTarget, new SharpDX.Color(255, 0, 255, 200))) // Magenta/Pink
                        {
                            for (int i = 0; i < curvePoints.Count - 1; i++)
                            {
                                RenderTarget.DrawLine(curvePoints[i], curvePoints[i + 1], curveBrush, 3.0f);
                            }
                        }
                    }
                }
            }

            if (!double.IsNaN(modeledZero) && !double.IsInfinity(modeledZero) && modeledZero > 0)
            {
                double modeledZeroFutures = modeledZero - sprd;
                float y = chartScale.GetYByValue(modeledZeroFutures);

                if (y >= ChartPanel.Y && y <= ChartPanel.Y + ChartPanel.H)
                {
                    using (SharpDX.Direct2D1.SolidColorBrush zeroBrush = new SharpDX.Direct2D1.SolidColorBrush(
                        RenderTarget, new SharpDX.Color(255, 140, 0, 235)))
                    using (SharpDX.Direct2D1.StrokeStyle zeroStroke = new SharpDX.Direct2D1.StrokeStyle(
                        RenderTarget.Factory,
                        new SharpDX.Direct2D1.StrokeStyleProperties
                        {
                            DashStyle = SharpDX.Direct2D1.DashStyle.Dot,
                            DashCap = SharpDX.Direct2D1.CapStyle.Round,
                            StartCap = SharpDX.Direct2D1.CapStyle.Round,
                            EndCap = SharpDX.Direct2D1.CapStyle.Round
                        }))
                    using (SharpDX.DirectWrite.TextFormat zeroLabelFormat = new SharpDX.DirectWrite.TextFormat(
                        Core.Globals.DirectWriteFactory, "Arial", SharpDX.DirectWrite.FontWeight.Bold,
                        SharpDX.DirectWrite.FontStyle.Normal, 11))
                    {
                        RenderTarget.DrawLine(
                            new SharpDX.Vector2(ChartPanel.X, y),
                            new SharpDX.Vector2(ChartPanel.X + ChartPanel.W, y),
                            zeroBrush,
                            3.0f,
                            zeroStroke);

                        RenderTarget.DrawText(
                            $"Modeled 0 GEX {modeledZero:F0}",
                            zeroLabelFormat,
                            new SharpDX.RectangleF(ChartPanel.X + ChartPanel.W - 150, y - 17, 145, 16),
                            zeroBrush);
                    }
                }
            }

            if (string.IsNullOrEmpty(dashSymbol))
                dashSymbol = idxSym;

            if (string.IsNullOrEmpty(dashBias))
                dashBias = regime;
            if (double.IsNaN(dashConfidence))
                dashConfidence = 0;

            float dashboardWidth = Math.Max(390f, Math.Min((float)ChartPanel.W * 0.46f, 580f));
            float dashboardHeight = 122;
            float dashboardX = ChartPanel.X + 10;
            float dashboardY = ChartPanel.Y + ChartPanel.H - dashboardHeight - 12;

            using (SharpDX.DirectWrite.TextFormat dashboardTitleFormat = new SharpDX.DirectWrite.TextFormat(
                Core.Globals.DirectWriteFactory, "Arial", SharpDX.DirectWrite.FontWeight.Bold,
                SharpDX.DirectWrite.FontStyle.Normal, 17))
            using (SharpDX.DirectWrite.TextFormat dashboardMetricFormat = new SharpDX.DirectWrite.TextFormat(
                Core.Globals.DirectWriteFactory, "Arial", SharpDX.DirectWrite.FontWeight.Bold,
                SharpDX.DirectWrite.FontStyle.Normal, 13))
            using (SharpDX.DirectWrite.TextFormat dashboardLabelFormat = new SharpDX.DirectWrite.TextFormat(
                Core.Globals.DirectWriteFactory, "Arial", SharpDX.DirectWrite.FontWeight.Normal,
                SharpDX.DirectWrite.FontStyle.Normal, 8))
            using (SharpDX.DirectWrite.TextFormat dashboardTextFormat = new SharpDX.DirectWrite.TextFormat(
                Core.Globals.DirectWriteFactory, "Arial", 9))
            {
                SharpDX.RectangleF dashboardRect = new SharpDX.RectangleF(dashboardX - 5, dashboardY - 5, dashboardWidth, dashboardHeight);
                using (SharpDX.Direct2D1.SolidColorBrush dashboardBrush = new SharpDX.Direct2D1.SolidColorBrush(
                    RenderTarget, new SharpDX.Color(20, 20, 30, 235)))
                {
                    RenderTarget.FillRectangle(dashboardRect, dashboardBrush);
                }

                SharpDX.Color red = new SharpDX.Color(255, 66, 86, 255);
                SharpDX.Color green = new SharpDX.Color(55, 230, 120, 255);
                SharpDX.Color amber = new SharpDX.Color(255, 171, 37, 255);
                SharpDX.Color cyan = new SharpDX.Color(141, 199, 255, 255);
                SharpDX.Color white = new SharpDX.Color(245, 248, 255, 255);
                SharpDX.Color dim = new SharpDX.Color(176, 202, 224, 255);
                SharpDX.Color biasColor = dashScore < -0.05 ? red : dashScore > 0.05 ? green : amber;

                using (SharpDX.Direct2D1.SolidColorBrush whiteBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, white))
                using (SharpDX.Direct2D1.SolidColorBrush dimBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, dim))
                using (SharpDX.Direct2D1.SolidColorBrush cyanBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, cyan))
                using (SharpDX.Direct2D1.SolidColorBrush biasBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, biasColor))
                using (SharpDX.Direct2D1.SolidColorBrush amberBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, amber))
                {
                    float contentX = dashboardX + 10;
                    float contentY = dashboardY + 8;
                    float topY = contentY;
                    float metricY = contentY + 12;
                    float tileY = dashboardY + 58;
                    float symbolWidth = 138;
                    float metricWidth = Math.Max(48f, (dashboardWidth - symbolWidth - 22) / 5f);
                    float metricX = contentX + symbolWidth;

                    RenderTarget.DrawText(string.IsNullOrEmpty(dashSymbol) ? "NDX" : dashSymbol, dashboardTitleFormat,
                        new SharpDX.RectangleF(contentX, contentY, 50, 22), whiteBrush);

                    RenderTarget.DrawText(dashContext, dashboardTextFormat,
                        new SharpDX.RectangleF(contentX, contentY + 24, symbolWidth - 10, 30), dimBrush);

                    RenderTarget.DrawText(dashBias, dashboardLabelFormat,
                        new SharpDX.RectangleF(metricX, topY, metricWidth, 10), biasBrush);
                    string scoreText = double.IsNaN(dashScore) ? "--" : $"{Math.Round(dashScore * 100):+0;-0;0}%";
                    RenderTarget.DrawText(scoreText, dashboardMetricFormat,
                        new SharpDX.RectangleF(metricX, metricY, metricWidth, 18), biasBrush);

                    RenderTarget.DrawText("Data Q", dashboardLabelFormat,
                        new SharpDX.RectangleF(metricX + metricWidth, topY, metricWidth, 10), cyanBrush);
                    RenderTarget.DrawText($"{Math.Round(dashConfidence * 100):F0}%", dashboardMetricFormat,
                        new SharpDX.RectangleF(metricX + metricWidth, metricY, metricWidth, 18), whiteBrush);

                    RenderTarget.DrawText("Target", dashboardLabelFormat,
                        new SharpDX.RectangleF(metricX + (metricWidth * 2), topY, metricWidth, 10), cyanBrush);
                    RenderTarget.DrawText(double.IsNaN(dashTarget) ? "--" : dashTarget.ToString("F0", CultureInfo.InvariantCulture), dashboardMetricFormat,
                        new SharpDX.RectangleF(metricX + (metricWidth * 2), metricY, metricWidth, 18), whiteBrush);

                    RenderTarget.DrawText("Invalid", dashboardLabelFormat,
                        new SharpDX.RectangleF(metricX + (metricWidth * 3), topY, metricWidth, 10), cyanBrush);
                    RenderTarget.DrawText(double.IsNaN(dashInvalidation) ? "--" : dashInvalidation.ToString("F0", CultureInfo.InvariantCulture), dashboardMetricFormat,
                        new SharpDX.RectangleF(metricX + (metricWidth * 3), metricY, metricWidth, 18), amberBrush);

                    RenderTarget.DrawText("Flip", dashboardLabelFormat,
                        new SharpDX.RectangleF(metricX + (metricWidth * 4), topY, metricWidth, 10), cyanBrush);
                    RenderTarget.DrawText(double.IsNaN(dashFlip) ? "--" : dashFlip.ToString("F0", CultureInfo.InvariantCulture), dashboardMetricFormat,
                        new SharpDX.RectangleF(metricX + (metricWidth * 4), metricY, metricWidth, 18), whiteBrush);

                    float tileGap = 6;
                    float tileWidth = (dashboardWidth - 20 - (tileGap * 3)) / 4f;
                    string marketValue = (string.IsNullOrEmpty(dashMarket) ? $"Market: {regime}" : dashMarket).Replace("Market: ", "");
                    string dealerValue = (string.IsNullOrEmpty(dashDealer) ? $"Dealer: {prevRegime}" : dashDealer).Replace("Dealer: ", "");
                    string liquidityValue = (string.IsNullOrEmpty(dashLiquidity) ? $"{idxSym}: {idx:F0} | Spread: {sprd:+0;-0;0}" : dashLiquidity).Replace("Liquidity: ", "");
                    string whaleValue = (string.IsNullOrEmpty(dashWhale) ? $"Updated: {update}" : dashWhale).Replace("Index Basket: ", "").Replace("Whale: ", "");
                    string edgeValue = dashEdgeSummary;
                    if (string.IsNullOrEmpty(edgeValue))
                    {
                        string edgeWin = double.IsNaN(dashEdgeWinRate) ? "--" : $"{Math.Round(dashEdgeWinRate * 100):F0}%";
                        string edgeSample = double.IsNaN(dashEdgeSample) ? "0" : dashEdgeSample.ToString("F0", CultureInfo.InvariantCulture);
                        string edgeMove = double.IsNaN(dashEdgeMedianMove) ? "--" : $"{dashEdgeMedianMove:+0;-0;0}";
                        edgeValue = $"30m {edgeWin} n={edgeSample} med {edgeMove}";
                    }
                    string edgeSource = string.IsNullOrEmpty(dashEdgeSource) ? "Empirical Edge" : $"Empirical Edge ({dashEdgeSource})";

                    RenderTarget.DrawText("Market Signal", dashboardLabelFormat,
                        new SharpDX.RectangleF(contentX, tileY, tileWidth, 10), cyanBrush);
                    RenderTarget.DrawText(marketValue, dashboardTextFormat,
                        new SharpDX.RectangleF(contentX, tileY + 11, tileWidth, 16), biasBrush);

                    RenderTarget.DrawText("Dealer Positioning", dashboardLabelFormat,
                        new SharpDX.RectangleF(contentX + tileWidth + tileGap, tileY, tileWidth, 10), cyanBrush);
                    RenderTarget.DrawText(dealerValue, dashboardTextFormat,
                        new SharpDX.RectangleF(contentX + tileWidth + tileGap, tileY + 11, tileWidth, 16), biasBrush);

                    RenderTarget.DrawText("Gamma Liquidity", dashboardLabelFormat,
                        new SharpDX.RectangleF(contentX + ((tileWidth + tileGap) * 2), tileY, tileWidth, 10), cyanBrush);
                    RenderTarget.DrawText(liquidityValue, dashboardTextFormat,
                        new SharpDX.RectangleF(contentX + ((tileWidth + tileGap) * 2), tileY + 11, tileWidth, 16), biasBrush);

                    RenderTarget.DrawText("Index Gamma Basket", dashboardLabelFormat,
                        new SharpDX.RectangleF(contentX + ((tileWidth + tileGap) * 3), tileY, tileWidth, 10), cyanBrush);
                    RenderTarget.DrawText(whaleValue, dashboardTextFormat,
                        new SharpDX.RectangleF(contentX + ((tileWidth + tileGap) * 3), tileY + 11, tileWidth, 16), biasBrush);

                    float edgeY = tileY + 34;
                    RenderTarget.DrawText(edgeSource, dashboardLabelFormat,
                        new SharpDX.RectangleF(contentX, edgeY, dashboardWidth - 20, 10), cyanBrush);
                    RenderTarget.DrawText(edgeValue, dashboardTextFormat,
                        new SharpDX.RectangleF(contentX, edgeY + 12, dashboardWidth - 20, 18), whiteBrush);
                }
            }
            RenderPredictions(chartScale);
        }

        #region Properties
        [Display(Name = "Enable Predictions", Order = 1, GroupName = "Predictions")]
        public bool EnablePredictions { get; set; }

        [Range(1024, 65535)]
        [Display(Name = "Prediction Port", Order = 2, GroupName = "Predictions")]
        public int PredictionPort { get; set; }

        [Display(Name = "Prediction Feed Label", Description = "Label this chart's data source to keep separate providers' forecast history apart.", Order = 3, GroupName = "Predictions")]
        public string PredictionFeedLabel { get; set; }

        [NinjaScriptProperty]
        [Range(1024, 65535)]
        [Display(Name = "Listen Port", Order = 1, GroupName = "Connection")]
        public int ListenPort { get; set; }

        [NinjaScriptProperty]
        [Display(Name = "Gamma Bars On Right", Order = 1, GroupName = "Visual")]
        public bool GammaBarsOnRight { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Time Series (Minutes)", Order = 1, GroupName = "JMA Spread")]
        public int JmaTimeSeriesMinutes { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Length - JMA", Order = 2, GroupName = "JMA Spread")]
        public int JmaLength { get; set; }

        [NinjaScriptProperty]
        [Range(-100, 100)]
        [Display(Name = "Phase - JMA", Order = 3, GroupName = "JMA Spread")]
        public int JmaPhase { get; set; }

        [NinjaScriptProperty]
        [Range(1, int.MaxValue)]
        [Display(Name = "Power - JMA", Order = 4, GroupName = "JMA Spread")]
        public int JmaPower { get; set; }

        [NinjaScriptProperty]
        [Range(0, double.MaxValue)]
        [Display(Name = "Reset Threshold - JMA", Order = 5, GroupName = "JMA Spread")]
        public double JmaResetThreshold { get; set; }
        #endregion
    }
}

#region NinjaScript generated code. Neither change nor remove.

namespace NinjaTrader.NinjaScript.Indicators
{
	public partial class Indicator : NinjaTrader.Gui.NinjaScript.IndicatorRenderBase
	{
		private OpenGamma[] cacheOpenGamma;
		public OpenGamma OpenGamma(int listenPort)
		{
			return OpenGamma(Input, listenPort, false, 2, 13, 78, 2, 25);
		}

		public OpenGamma OpenGamma(int listenPort, bool gammaBarsOnRight)
		{
			return OpenGamma(Input, listenPort, gammaBarsOnRight, 2, 13, 78, 2, 25);
		}

		public OpenGamma OpenGamma(int listenPort, bool gammaBarsOnRight, int jmaTimeSeriesMinutes, int jmaLength, int jmaPhase, int jmaPower, double jmaResetThreshold)
		{
			return OpenGamma(Input, listenPort, gammaBarsOnRight, jmaTimeSeriesMinutes, jmaLength, jmaPhase, jmaPower, jmaResetThreshold);
		}

		public OpenGamma OpenGamma(ISeries<double> input, int listenPort)
		{
			return OpenGamma(input, listenPort, false, 2, 13, 78, 2, 25);
		}

		public OpenGamma OpenGamma(ISeries<double> input, int listenPort, bool gammaBarsOnRight)
		{
			return OpenGamma(input, listenPort, gammaBarsOnRight, 2, 13, 78, 2, 25);
		}

		public OpenGamma OpenGamma(ISeries<double> input, int listenPort, bool gammaBarsOnRight, int jmaTimeSeriesMinutes, int jmaLength, int jmaPhase, int jmaPower, double jmaResetThreshold)
		{
			if (cacheOpenGamma != null)
				for (int idx = 0; idx < cacheOpenGamma.Length; idx++)
					if (cacheOpenGamma[idx] != null && cacheOpenGamma[idx].ListenPort == listenPort && cacheOpenGamma[idx].GammaBarsOnRight == gammaBarsOnRight && cacheOpenGamma[idx].JmaTimeSeriesMinutes == jmaTimeSeriesMinutes && cacheOpenGamma[idx].JmaLength == jmaLength && cacheOpenGamma[idx].JmaPhase == jmaPhase && cacheOpenGamma[idx].JmaPower == jmaPower && cacheOpenGamma[idx].JmaResetThreshold == jmaResetThreshold && cacheOpenGamma[idx].EqualsInput(input))
						return cacheOpenGamma[idx];
			return CacheIndicator<OpenGamma>(new OpenGamma(){ ListenPort = listenPort, GammaBarsOnRight = gammaBarsOnRight, JmaTimeSeriesMinutes = jmaTimeSeriesMinutes, JmaLength = jmaLength, JmaPhase = jmaPhase, JmaPower = jmaPower, JmaResetThreshold = jmaResetThreshold }, input, ref cacheOpenGamma);
		}
	}
}

namespace NinjaTrader.NinjaScript.MarketAnalyzerColumns
{
	public partial class MarketAnalyzerColumn : MarketAnalyzerColumnBase
	{
		public Indicators.OpenGamma OpenGamma(int listenPort)
		{
			return indicator.OpenGamma(Input, listenPort);
		}

		public Indicators.OpenGamma OpenGamma(int listenPort, bool gammaBarsOnRight)
		{
			return indicator.OpenGamma(Input, listenPort, gammaBarsOnRight);
		}

		public Indicators.OpenGamma OpenGamma(int listenPort, bool gammaBarsOnRight, int jmaTimeSeriesMinutes, int jmaLength, int jmaPhase, int jmaPower, double jmaResetThreshold)
		{
			return indicator.OpenGamma(Input, listenPort, gammaBarsOnRight, jmaTimeSeriesMinutes, jmaLength, jmaPhase, jmaPower, jmaResetThreshold);
		}

		public Indicators.OpenGamma OpenGamma(ISeries<double> input , int listenPort)
		{
			return indicator.OpenGamma(input, listenPort);
		}

		public Indicators.OpenGamma OpenGamma(ISeries<double> input , int listenPort, bool gammaBarsOnRight)
		{
			return indicator.OpenGamma(input, listenPort, gammaBarsOnRight);
		}

		public Indicators.OpenGamma OpenGamma(ISeries<double> input , int listenPort, bool gammaBarsOnRight, int jmaTimeSeriesMinutes, int jmaLength, int jmaPhase, int jmaPower, double jmaResetThreshold)
		{
			return indicator.OpenGamma(input, listenPort, gammaBarsOnRight, jmaTimeSeriesMinutes, jmaLength, jmaPhase, jmaPower, jmaResetThreshold);
		}
	}
}

namespace NinjaTrader.NinjaScript.Strategies
{
	public partial class Strategy : NinjaTrader.Gui.NinjaScript.StrategyRenderBase
	{
		public Indicators.OpenGamma OpenGamma(int listenPort)
		{
			return indicator.OpenGamma(Input, listenPort);
		}

		public Indicators.OpenGamma OpenGamma(int listenPort, bool gammaBarsOnRight)
		{
			return indicator.OpenGamma(Input, listenPort, gammaBarsOnRight);
		}

		public Indicators.OpenGamma OpenGamma(int listenPort, bool gammaBarsOnRight, int jmaTimeSeriesMinutes, int jmaLength, int jmaPhase, int jmaPower, double jmaResetThreshold)
		{
			return indicator.OpenGamma(Input, listenPort, gammaBarsOnRight, jmaTimeSeriesMinutes, jmaLength, jmaPhase, jmaPower, jmaResetThreshold);
		}

		public Indicators.OpenGamma OpenGamma(ISeries<double> input , int listenPort)
		{
			return indicator.OpenGamma(input, listenPort);
		}

		public Indicators.OpenGamma OpenGamma(ISeries<double> input , int listenPort, bool gammaBarsOnRight)
		{
			return indicator.OpenGamma(input, listenPort, gammaBarsOnRight);
		}

		public Indicators.OpenGamma OpenGamma(ISeries<double> input , int listenPort, bool gammaBarsOnRight, int jmaTimeSeriesMinutes, int jmaLength, int jmaPhase, int jmaPower, double jmaResetThreshold)
		{
			return indicator.OpenGamma(input, listenPort, gammaBarsOnRight, jmaTimeSeriesMinutes, jmaLength, jmaPhase, jmaPower, jmaResetThreshold);
		}
	}
}

#endregion
