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
        private double spread = 0;
        private string indexSymbol = "";
        
        // Use OnBarUpdate to capture price safely
        private double lastClosePrice = 0;
        
        // Gamma S/R levels (adjusted for futures)
        private List<GammaLevel> gammaLevels = new List<GammaLevel>();
        
        private readonly object lockObj = new object();
        
        private struct GammaLevel
        {
            public double Strike;
            public double Gex;
            public double FuturesPrice; // Strike adjusted by spread
            public bool IsResistance;
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
            }
            else if (State == State.Configure)
            {
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
                    
                    int.TryParse(ExtractJsonValue(json, "regime_code"), out int code);
                    regimeCode = code;
                    
                    lastUpdate = DateTime.Now.ToString("HH:mm:ss");
                    
                    // Get index price from broadcast payload (not from NinjaTrader)
                    if (indexSymbol == "NDX")
                    {
                        double.TryParse(ExtractJsonValue(json, "spot_ndx"), out double ndx);
                        indexPrice = ndx;
                    }
                    else if (indexSymbol == "SPX")
                    {
                        double.TryParse(ExtractJsonValue(json, "spot_spx"), out double spx);
                        indexPrice = spx;
                    }
                }
                
                // Capture futures price and calc spread on UI thread
                if (ChartControl != null)
                {
                    // Cache json for use in dispatcher
                    string jsonCopy = json;
                    
                    ChartControl.Dispatcher.InvokeAsync(() =>
                    {
                        try
                        {
                            lock (lockObj)
                            {
                                // Use cached close price from OnBarUpdate
                                if (lastClosePrice > 0)
                                {
                                    // Cache futures price for logging
                                    futuresPrice = lastClosePrice;
                                    
                                    if (indexPrice > 0)
                                    {
                                        // Use integer spread (Index - Futures)
                                        // Round to nearest integer before subtracting
                                        spread = Math.Round(indexPrice) - Math.Round(futuresPrice);
                                    }
                                    
                                    // Parse and adjust gamma levels
                                    ParseGammaLevels(jsonCopy);
                                    
                                    Print($"OpenGamma: {currentRegime} | {indexSymbol}: {Math.Round(indexPrice)} | Futures: {Math.Round(futuresPrice)} | Spread: {spread}");
                                    ForceRefresh();
                                }
                            }
                        }
                        catch (Exception ex)
                        {
                            Print("OpenGamma: Failed to process update - " + ex.Message);
                        }
                    });
                }
            }
            catch (Exception ex)
            {
                Print("OpenGamma: Parse error - " + ex.Message);
            }
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
        
        private void ParseGammaLevels(string json)
        {
            string levelsKey = indexSymbol == "NDX" ? "gamma_levels_ndx" : "gamma_levels_spx";
            
            gammaLevels.Clear();
            
            // Find key manually but robustly
            // Find key manually but robustly
            int keyIdx = json.IndexOf("\"" + levelsKey + "\"");
            if (keyIdx < 0) 
            {
               Print($"OpenGamma: Key '{levelsKey}' not found in JSON.");
               return; 
            }
            
            // Find start of array value [
            int arrStart = json.IndexOf('[', keyIdx);
            if (arrStart < 0) return;
            
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
            
            if (arrEnd < 0) return;
            
            string arrContent = json.Substring(arrStart + 1, arrEnd - arrStart - 1);
            if (string.IsNullOrWhiteSpace(arrContent)) return;
            
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
            
            Print($"OpenGamma: Parsed {gammaLevels.Count} levels for {levelsKey}");
        }
        
        private void ParseGammaLevelObject(string objJson)
        {
            double strike = 0, gex = 0;
            
            try 
            {
                // Robust value extraction
                strike = ExtractNum(objJson, "strike");
                gex = ExtractNum(objJson, "gex");
                
                if (strike > 0)
                {
                    double futuresLevel = strike - spread;
                    gammaLevels.Add(new GammaLevel
                    {
                        Strike = strike,
                        Gex = gex,
                        FuturesPrice = futuresLevel,
                        IsResistance = gex > 0
                    });
                }
            }
            catch (Exception ex) { Print("OpenGamma: Error parsing level obj: " + ex.Message); }
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
                
            if (double.TryParse(json.Substring(valStart, valEnd - valStart), out double result))
                return result;
                
            return 0;
        }

        protected override void OnBarUpdate()
        {
            // Capture latest price for use in async updates
            if (CurrentBar > 0)
                lastClosePrice = Close[0];
        }
        
        protected override void OnRender(ChartControl chartControl, ChartScale chartScale)
        {
            base.OnRender(chartControl, chartScale);
            
            if (chartControl == null) return;
            
            // Get current state thread-safely
            string regime, prevRegime, update, idxSym;
            int code;
            double idx, fut, sprd;
            
            lock (lockObj)
            {
                regime = currentRegime;
                prevRegime = previousRegime;
                code = regimeCode;
                update = lastUpdate;
                idx = indexPrice;
                fut = futuresPrice;
                sprd = spread;
                idxSym = indexSymbol;
            }
            
            // Panel dimensions
            float panelWidth = 180;
            float panelHeight = 85;
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
                // Find Max GEX for scaling
                double maxGex = 0;
                foreach (var level in levelsCopy)
                    maxGex = Math.Max(maxGex, Math.Abs(level.Gex));
                
                if (maxGex == 0) maxGex = 1; // Prevent divide by zero

                using (SharpDX.DirectWrite.TextFormat labelFormat = new SharpDX.DirectWrite.TextFormat(
                    Core.Globals.DirectWriteFactory, "Arial", 10))
                {
                    foreach (var level in levelsCopy)
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
                            
                        // Text color (fully opaque)
                        SharpDX.Color textColor = level.IsResistance
                            ? new SharpDX.Color(255, 100, 100, 255)
                            : new SharpDX.Color(100, 255, 100, 255);
                        
                        using (SharpDX.Direct2D1.SolidColorBrush barBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, barColor))
                        using (SharpDX.Direct2D1.SolidColorBrush textBrush = new SharpDX.Direct2D1.SolidColorBrush(RenderTarget, textColor))
                        {
                            // Draw filled bar from Left side
                            SharpDX.RectangleF rect = new SharpDX.RectangleF(ChartPanel.X, yTop, width, height);
                            RenderTarget.FillRectangle(rect, barBrush);
                            
                            // Draw label with strike price
                            string label = $"{level.Strike:F0}";
                            RenderTarget.DrawText(label, labelFormat,
                                new SharpDX.RectangleF(ChartPanel.X + 5, yTop + 3, 80, 14), textBrush);
                        }
                    }
                }
            }
        }

        #region Properties
        [NinjaScriptProperty]
        [Range(1024, 65535)]
        [Display(Name = "Listen Port", Order = 1, GroupName = "Connection")]
        public int ListenPort { get; set; }
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
			return OpenGamma(Input, listenPort);
		}

		public OpenGamma OpenGamma(ISeries<double> input, int listenPort)
		{
			if (cacheOpenGamma != null)
				for (int idx = 0; idx < cacheOpenGamma.Length; idx++)
					if (cacheOpenGamma[idx] != null && cacheOpenGamma[idx].ListenPort == listenPort && cacheOpenGamma[idx].EqualsInput(input))
						return cacheOpenGamma[idx];
			return CacheIndicator<OpenGamma>(new OpenGamma(){ ListenPort = listenPort }, input, ref cacheOpenGamma);
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

		public Indicators.OpenGamma OpenGamma(ISeries<double> input , int listenPort)
		{
			return indicator.OpenGamma(input, listenPort);
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

		public Indicators.OpenGamma OpenGamma(ISeries<double> input , int listenPort)
		{
			return indicator.OpenGamma(input, listenPort);
		}
	}
}

#endregion
