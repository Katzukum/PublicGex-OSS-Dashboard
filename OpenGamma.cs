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
        private volatile bool isRunning;

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
            }
            else if (State == State.Terminated)
            {
                StopListener();
            }
        }

        #region TCP Client
        private void StartListener()
        {
            if (listenerThread != null && listenerThread.IsAlive)
                return;

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

            if (listenerThread != null && listenerThread.IsAlive)
                listenerThread.Join(1000);

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
                }

                // Retry delay
                if (isRunning)
                    Thread.Sleep(5000);
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
                    if (TryExtractDashboardDouble(json, "dashboard_confidence", out double confidenceScore))
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

                    RenderTarget.DrawText("Conf", dashboardLabelFormat,
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
                    string whaleValue = (string.IsNullOrEmpty(dashWhale) ? $"Updated: {update}" : dashWhale).Replace("Whale: ", "");
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

                    RenderTarget.DrawText("Whale Flow", dashboardLabelFormat,
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
        }

        #region Properties
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
