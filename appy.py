import eel
import pandas as pd
import json
import socket
import threading
from datetime import datetime, timedelta
from sqlalchemy import create_engine, text

# --- Configuration ---
DB_CONNECTION_STR = "sqlite:///gex_data.db"
eel.init('web')

# --- Event/Notification Server ---

# --- 0DTE Optimization Helpers ---

SENSITIVITY_MAP = {
    "SPY": 0.0020,  # 0.20%
    "SPX": 0.0020,  # 0.20%
    "QQQ": 0.0035,  # 0.35% (Tech is noisier)
    "NDX": 0.0030,  # 0.30%
    "IWM": 0.0015,  # 0.15%
    "DEFAULT": 0.0025
}

def calculate_0dte_trend_score(spot, flip, symbol):
    """
    Calculates a score between -1 and 1 based on distance from flip.
    Uses symbol-specific sensitivity from SENSITIVITY_MAP.
    """
    if not flip or flip == 0:
        return 0
    
    sensitivity = SENSITIVITY_MAP.get(symbol, SENSITIVITY_MAP["DEFAULT"])
    
    # Calculate raw percentage distance
    dist_pct = (spot - flip) / flip
    
    # Scale score: distance / sensitivity
    # Example: If dist is 0.2% and sensitivity is 0.2%, score is 1.0
    score = dist_pct / sensitivity
    
    # Clamp between -1 and 1
    return max(-1.0, min(1.0, score))

def get_decay_multiplier(total_gamma, total_theta):
    """
    Returns a multiplier (1.0 to 1.25) to boost 'Grind Up' confidence
    if Time Decay (Theta) is the dominant force.
    """
    if total_gamma == 0: return 1.0
    
    # Calculate Theta/Gamma Ratio
    # High Ratio = Time is the main driver (Mid-day drift)
    # Low Ratio = Price Action is the main driver (Open/Close)
    # Note: Theta is usually negative, take abs
    ratio = abs(total_theta / total_gamma)
    
    # Thresholds: If Theta is > 2x Gamma, we are in "Charm Zone"
    if ratio > 2.0: 
        return 1.25 # Boost confidence by 25%
    elif ratio > 1.5:
        return 1.10
        
    return 1.0

def run_event_server(port=5005):
    """
    Listens on a local TCP socket for JSON messages from external scripts
    (like publicData.py) and forwards them to the frontend via Eel.

    Args:
        port: The local port to bind to (default: 5005).
    """
    print(f"Starting Event Server on port {port}...")
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        server.bind(('127.0.0.1', port))
        server.listen(5)
        
        while True:
            client_sock, addr = server.accept()
            try:
                data = client_sock.recv(4096)
                if data:
                    # Decode and parse
                    msg = json.loads(data.decode('utf-8'))
                    print(f"Event received: {msg.get('type', 'UNKNOWN')}")
                    
                    # 1. Handle Market Updates (Forward to NinjaTrader)
                    if msg.get('type') == 'MARKET_UPDATE' and 'data' in msg:
                        try:
                            from ninjatrader_broadcaster import send_regime_update
                            send_regime_update(msg['data'])
                            print(f"[Bridge] Forwarded market update to NinjaTrader")
                        except Exception as e:
                            print(f"[Bridge] Failed to forward to NinjaTrader: {e}")

                    # 2. Forward to Frontend
                    # eel.handle_backend_event(msg) # Need to ensuring this function exists in JS
                    # Eel functions are called as eel.Function()(callback)
                    # When calling FROM Python TO JS, we just do eel.JSFunctionName(args)
                    eel.handle_backend_event(msg)
                    
            except Exception as e:
                print(f"Error processing event: {e}")
            finally:
                client_sock.close()
                
    except Exception as e:
        print(f"Event Server Failed to Start: {e}")
    finally:
        server.close()

# Start Server in Background Thread
event_thread = threading.Thread(target=run_event_server, daemon=True)
event_thread.start()

# Start NinjaTrader Broadcast Server (Port 5010)
try:
    from ninjatrader_broadcaster import start_server as start_nt_server
    start_nt_server(5010)
except ImportError:
    print("Could not import ninjatrader_broadcaster")

# --- Database Connection ---
engine = create_engine(DB_CONNECTION_STR)

@eel.expose
def get_symbols() -> list[str]:
    """Returns a list of unique symbols available in the database.

    Queries the `raw_option_greeks` table for distinct symbols.

    Returns:
        A list of symbol strings (e.g., ["SPY", "QQQ"]).
    """
    with engine.connect() as conn:
        result = conn.execute(text("SELECT DISTINCT symbol FROM raw_option_greeks"))
        return [r[0] for r in result]

@eel.expose
def get_settings() -> dict:
    """Reads and returns the current application settings.

    Returns:
        A dictionary containing settings from `settings.json`.
    """
    import json
    with open('settings.json') as f:
        return json.load(f)

@eel.expose
def save_settings(new_settings: dict) -> bool:
    """Updates the settings.json file with new values.

    Merges the provided settings into the existing file to preserve
    keys that are not present in `new_settings`.

    Args:
        new_settings: A dictionary of settings to update.

    Returns:
        True if successful.
    """
    import json
    try:
        # Load existing manually to preserve hidden keys (like 'weights')
        with open('settings.json', 'r') as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}
        
    # Merge new settings into existing
    existing.update(new_settings)
    
    with open('settings.json', 'w') as f:
        json.dump(existing, f, indent=2)
    print(f"Settings merged and saved: {existing.keys()}")
    return True

@eel.expose
def get_dashboard_data(symbol: str = "SPY") -> dict:
    """Fetches comprehensive dashboard data for a specific symbol.

    Retrieves the latest snapshot, option profile (strike vs GEX), and 
    historical net GEX/price history for charting.

    Args:
        symbol: The ticker symbol to fetch (default: "SPY").

    Returns:
        A dictionary containing:
            - snapshot (dict): Key metrics (Spot, Net GEX, Max Pain).
            - profile (list): List of dicts for the bar chart (Strike, GEX).
            - history (list): List of dicts for the time-series chart.
            - error (str): If data is missing or query fails.
    """
    try:
        with engine.connect() as conn:
            # 1. Get Latest Timestamp
            query_time = text("SELECT MAX(timestamp) FROM raw_option_greeks WHERE symbol = :symbol")
            result = conn.execute(query_time, {"symbol": symbol}).fetchone()
            
            if not result or not result[0]:
                return {"error": f"No data found for {symbol}. Run publicData.py."}
            
            latest_time = result[0]
            if isinstance(latest_time, str):
                latest_time = datetime.strptime(latest_time, "%Y-%m-%d %H:%M:%S.%f")

            # 2. Fetch Profile Data (For the Bar Chart & Table)
            # We need raw rows to separate Calls vs Puts in JS
            query_profile = text("""
                SELECT strike_price, option_type, gex_value, open_interest
                FROM raw_option_greeks 
                WHERE symbol = :symbol AND timestamp = :ts
                ORDER BY strike_price ASC
            """)
            df_profile = pd.read_sql(query_profile, conn, params={"symbol": symbol, "ts": latest_time})

            # 3. Fetch Snapshot (For KPIs)
            query_snap = text("""
                SELECT * FROM gex_snapshots 
                WHERE symbol = :symbol AND timestamp = :ts
                LIMIT 1
            """)
            snap_row = conn.execute(query_snap, {"symbol": symbol, "ts": latest_time}).fetchone()
            
            # Convert Row to Dict safely
            snapshot = {
                "symbol": symbol,
                "timestamp": str(latest_time),
                "spot_price": snap_row.spot_price if snap_row else 0,
                "total_net_gex": snap_row.total_net_gex if snap_row else 0,
                "max_call_gex_strike": snap_row.max_call_gex_strike if snap_row else 0,
                "max_put_gex_strike": snap_row.max_put_gex_strike if snap_row else 0
            }

            # 4. Fetch History (For the Line Chart)
            query_history = text("""
                SELECT timestamp, total_net_gex, spot_price
                FROM gex_snapshots
                WHERE symbol = :symbol
                ORDER BY timestamp ASC
                LIMIT 100
            """)
            df_hist = pd.read_sql(query_history, conn, params={"symbol": symbol})
            
            # Convert timestamps to string for JSON
            df_hist['timestamp'] = df_hist['timestamp'].apply(lambda x: str(x))

            # Structure for Frontend
            return {
                "snapshot": snapshot,
                "profile": df_profile.to_dict(orient='records'),
                "history": df_hist.to_dict(orient='records')
            }

    except Exception as e:
        print(f"Error: {e}")
        return {"error": str(e)}

@eel.expose
def get_market_overview() -> dict:
    try:
        import json
        import math
        
        with open('settings.json') as f:
            settings = json.load(f)
        
        # Defaults if keys missing in settings
        weights_traders = settings.get('weights', {"SPY": 0.5, "QQQ": 0.3, "IWM": 0.2})
        weights_whale = settings.get('weights_whale', {"SPX": 0.45, "NDX": 0.35, "IWM": 0.20})
        
        overview_data = {
            "compass_traders": {},
            "compass_whale": {},
            "components": [],
            "tilt": []
        }
        
        def _calculate_compass_state(target_weights, conn):
            x_score_sum = 0
            y_score_sum = 0
            total_weight = 0
            components = []
            
            # Formatting composition string
            comp_strs = [f"{s}: {int(w*100)}%" for s, w in target_weights.items()]
            composition_str = ", ".join(comp_strs)
            
            for symbol, weight in target_weights.items():
                # Fetch latest snapshot
                query = text("SELECT * FROM gex_snapshots WHERE symbol = :symbol ORDER BY timestamp DESC LIMIT 1")
                row = conn.execute(query, {"symbol": symbol}).fetchone()
                
                if row:
                    # Safe Extraction
                    net_gex = getattr(row, 'total_net_gex', 0)
                    spot = getattr(row, 'spot_price', 0)
                    flip = getattr(row, 'flip_strike', 0)
                    eff_gex = getattr(row, 'effective_gex', 0)
                    
                    # Greeks (Default to 0 if column missing/null)
                    total_gamma = getattr(row, 'total_gamma', 0) or 0
                    total_theta = getattr(row, 'total_theta', 0) or 0
                    
                    # --- 1. TREND SCORE (Y-AXIS) ---
                    # Uses the 0DTE sensitivity logic
                    if flip and flip > 0:
                        dist_pct = ((spot - flip) / flip) * 100
                        trend_score = calculate_0dte_trend_score(spot, flip, symbol)
                    else:
                        dist_pct = 0
                        trend_score = 0 # Neutral if no flip found
                    
                    # --- 2. VOL SCORE (X-AXIS) ---
                    # IMPROVEMENT: Add slight scaling so it's not purely binary
                    # but keep it simple: Positive = Stability, Negative = Velocity
                    vol_score = 1.0 if net_gex > 0 else -1.0
                    
                    # --- 3. CHARM / THETA BOOST ---
                    decay_boost = get_decay_multiplier(total_gamma, total_theta)
                    
                    # Add to aggregates
                    x_score_sum += vol_score * weight
                    y_score_sum += trend_score * weight
                    total_weight += weight
                    
                    # Regime Label for individual component
                    regime_label = "Bullish" if trend_score > 0 else "Bearish"
                    if abs(trend_score) < 0.2: regime_label = "Neutral"

                    components.append({
                        "symbol": symbol,
                        "spot": spot,
                        "flip_strike": flip,
                        "distance_pct": dist_pct,
                        "net_gex": net_gex,
                        "effective_gex": eff_gex, 
                        "regime": regime_label,
                        "decay_boost": decay_boost
                    })
            
            # --- FINAL COMPASS CALCULATION ---
            if total_weight > 0:
                final_vol = x_score_sum / total_weight
                final_trend = y_score_sum / total_weight
                
                # Weighted average of the boost
                weighted_boost = sum([c['decay_boost'] * target_weights.get(c['symbol'], 0) for c in components])
                avg_decay_boost = weighted_boost / total_weight
            else:
                final_vol, final_trend, avg_decay_boost = 0, 0, 1.0

            # Magnitude
            magnitude = math.sqrt(final_vol**2 + final_trend**2)
            
            # Determine Quadrant
            is_pos_gex = final_vol > 0
            is_bull_trend = final_trend > 0
            
            # --- STRATEGY LOGIC ---
            base_lbl, base_strat, base_icon = "", "", ""
            
            # IMPROVEMENT: Apply Theta Boost to ANY Positive Gamma regime
            # Theta burn helps "Grind Up" AND "Support/Chop" (Pinned markets)
            if is_pos_gex and avg_decay_boost > 1.1:
                magnitude *= avg_decay_boost
            
            # Cap magnitude at 1.1 (allow slight overflow for visual effect, but prevent breakage)
            magnitude = min(magnitude, 1.1)

            if is_pos_gex:
                if is_bull_trend:
                    base_lbl = "GRIND UP"
                    base_strat = "Buy Calls / Sell Put Spreads."
                    base_icon = "🟢"
                    if avg_decay_boost > 1.1: base_lbl += " (THETA BURN)"
                else:
                    base_lbl = "SUPPORT / CHOP"
                    base_strat = "'Bear Trap.' Iron Condors / Buy Dips."
                    base_icon = "⚪"
                    if avg_decay_boost > 1.1: base_lbl += " (PINNED)"
            else:
                # Negative Gamma
                if is_bull_trend:
                    base_lbl = "MELT UP"
                    base_strat = "Buy Calls. Unanchored upside."
                    base_icon = "🟡"
                else:
                    base_lbl = "CRASH / FLUSH"
                    base_strat = "Buy Puts / Sell Rips."
                    base_icon = "🔴"

            # Inner Ring Check
            inner_ring_threshold = 0.25
            if magnitude < inner_ring_threshold:
                label = f"{base_icon} WEAK {base_lbl}"
                strategy = f"{base_strat} (Low Confidence)"
            else:
                label = f"{base_icon} {base_lbl}"
                strategy = base_strat
                
            return {
                "x_score": final_vol,
                "y_score": final_trend,
                "label": label,
                "strategy": strategy,
                "composition": composition_str,
                "raw_components": components
            }

        with engine.connect() as conn:
            # 1. Calculate Traders Compass
            traders_state = _calculate_compass_state(weights_traders, conn)
            overview_data["compass_traders"] = traders_state
            
            # 2. Calculate Whale Compass
            whale_state = _calculate_compass_state(weights_whale, conn)
            overview_data["compass_whale"] = whale_state
            
            # 3. Merge Unique Components for Table/Tilt Chart
            merged_comps = {}
            def add_comps(comp_list):
                for c in comp_list:
                    merged_comps[c['symbol']] = c
            
            add_comps(traders_state['raw_components'])
            add_comps(whale_state['raw_components'])
            
            for sym, data in merged_comps.items():
                overview_data["components"].append({
                    "symbol": data['symbol'],
                    "spot": data['spot'],
                    "flip_strike": data['flip_strike'],
                    "distance_pct": data.get('distance_pct', 0),
                    "net_gex": data['net_gex'],
                    "regime": data['regime']
                })
                # Add Tilt Data
                overview_data["tilt"].append({
                    "symbol": data['symbol'], 
                    "net_gex": data.get('effective_gex', 0)
                })

        # Broadcast
        try:
            from ninjatrader_broadcaster import send_regime_update
            broadcast_payload = overview_data.copy()
            # Default to Traders for simple clients
            broadcast_payload['compass'] = overview_data['compass_traders'] 
            send_regime_update(broadcast_payload)
        except Exception as e:
            print(f"NinjaTrader broadcast error: {e}")

        return overview_data

    except Exception as e:
        print(f"Error in market overview: {e}")
        return {"error": str(e)}

@eel.expose
def trigger_data_refresh() -> bool:
    """Invokes the data collector script (publicData.py) immediately.

    Spawns a subprocess using the current Python interpreter.

    Returns:
        True if the subprocess completed successfully.
        False if an error occurred.
    """
    import subprocess
    import sys
    try:
        print("Triggering data refresh...")
        # Run publicData.py using the same python interpreter
        subprocess.run([sys.executable, "publicData.py"], check=True)
        print("Data refresh complete.")
        return True
    except Exception as e:
        print(f"Failed to refresh data: {e}")
        return False

# --- Run App ---
if __name__ == '__main__':
    try:
        eel.start('index.html', size=(1500, 900), port=8080)
    except OSError:
        eel.start('index.html', mode='edge', size=(1500, 900), port=8080)
