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
def run_event_server(port=5005):
    """Background thread to listen for events from data collector.

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
    """Calculates the Market Regime Compass (Trend vs. Volatility).

    Aggregates weighted GEX and Trend scores from multiple symbols (defined in
    settings.json) to produce two unified market sentiment vectors:
    1. Traders Market (SPY/QQQ/IWM)
    2. Whale Market (SPX/NDX/IWM)

    Returns:
        A dictionary containing:
            - compass_traders (dict): Compass state for Traders Market.
            - compass_whale (dict): Compass state for Whale Market.
            - components (list): Per-symbol contribution details (Union of all).
            - tilt (list): Effective GEX data.
    """
    try:
        # Load settings
        import json
        with open('settings.json') as f:
            settings = json.load(f)
        
        weights_traders = settings.get('weights', {})
        weights_whale = settings.get('weights_whale', {"SPX": 0.45, "NDX": 0.35, "IWM": 0.20})
        
        overview_data = {
            "compass_traders": {},
            "compass_whale": {},
            "components": [], # Will be populated with unique symbols from both
            "tilt": []
        }
        
        def _calculate_compass_state(target_weights, conn):
            """Helper to calculate compass state for a given set of weights."""
            x_score_sum = 0
            y_score_sum = 0
            total_weight = 0
            components = []
            
            # Format composition string for tooltip (e.g. "SPY: 50%, QQQ: 30%")
            comp_strs = []
            for s, w in target_weights.items():
                comp_strs.append(f"{s}: {int(w*100)}%")
            composition_str = ", ".join(comp_strs)
            
            for symbol, weight in target_weights.items():
                query = text("""
                    SELECT * FROM gex_snapshots 
                    WHERE symbol = :symbol 
                    ORDER BY timestamp DESC 
                    LIMIT 1
                """)
                row = conn.execute(query, {"symbol": symbol}).fetchone()
                
                if row:
                    net_gex = row.total_net_gex
                    spot = row.spot_price
                    flip = getattr(row, 'flip_strike', 0)
                    eff_gex = getattr(row, 'effective_gex', 0)
                    
                    # Logic
                    if flip and flip > 0:
                        dist_pct = ((spot - flip) / flip) * 100
                        trend_score = max(-1, min(1, dist_pct / 0.5))
                        vol_score = 1 if net_gex > 0 else -1
                        
                        x_score_sum += vol_score * weight
                        y_score_sum += trend_score * weight
                        total_weight += weight
                        
                        regime_label = "Bullish" if trend_score > 0 else "Bearish"
                        if abs(trend_score) < 0.2: regime_label = "Neutral"
                    else:
                        dist_pct = 0
                        regime_label = "No Flip Data"
                        eff_gex = 0 # Default if row missing attributes
                        net_gex = 0

                    components.append({
                        "symbol": symbol,
                        "spot": spot,
                        "flip_strike": flip,
                        "distance_pct": dist_pct,
                        "net_gex": net_gex,
                        "effective_gex": eff_gex, # Pass effective gex too
                        "regime": regime_label
                    })
            
            final_vol = x_score_sum / total_weight if total_weight > 0 else 0
            final_trend = y_score_sum / total_weight if total_weight > 0 else 0
            
            # Label Logic
            import math
            magnitude = math.sqrt(final_vol**2 + final_trend**2)
            inner_ring_threshold = 0.25
            is_pos_gex = final_vol > 0
            is_bull_trend = final_trend > 0
            
            if is_pos_gex and is_bull_trend:
                base_lbl, base_strat, base_icon = "GRIND UP", "Buy Calls / Sell Put Spreads.", "🟢"
            elif is_pos_gex and not is_bull_trend:
                base_lbl, base_strat, base_icon = "SUPPORT / CHOP", "'Bear Trap.' Buy dips.", "⚪"
            elif not is_pos_gex and is_bull_trend:
                base_lbl, base_strat, base_icon = "MELT UP", "Buy Calls, tighten stops.", "🟡"
            else:
                base_lbl, base_strat, base_icon = "CRASH / FLUSH", "Buy Puts / Sell Rips.", "🔴"

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
            
            # 3. Merge Unique Components for Pillars/Tilt
            # Use a dictionary keyed by symbol to ensure uniqueness
            merged_comps = {}
            
            # Helper to add to merged dict
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
                
                overview_data["tilt"].append({
                    "symbol": data['symbol'], 
                    "net_gex": data.get('effective_gex', 0)
                })

        # --- Broadcast to NinjaTrader (Focus on Traders/Settings Compass for now or send both?) ---
        # For backward compatibility, we can send the traders data as the primary
        try:
            from ninjatrader_broadcaster import send_regime_update
            # We construct a backward-compatible payload if needed, or just send the new one
            # NinjaTrader likely expects 'compass' key. Let's send Traders as default.
            broadcast_payload = overview_data.copy()
            broadcast_payload['compass'] = overview_data['compass_traders'] 
            send_regime_update(broadcast_payload)
        except Exception as e:
            print(f"NinjaTrader broadcast error (non-blocking): {e}")

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
