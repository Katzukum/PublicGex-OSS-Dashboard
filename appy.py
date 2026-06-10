import eel
import pandas as pd
import json
import socket
import threading
from datetime import datetime
from sqlalchemy import text

from models import initialize_database, schema_is_current

# --- Configuration ---
eel.init('web')

DEFAULT_SETTINGS = {
    "refresh_interval": 180,
    "theme": "dark",
    "symbols": ["SPY"],
    "backend_update_delay": 180,
    "raw_retention_days": 30,
    "weights": {"SPY": 1.0},
    "weights_whale": {"SPX": 0.45, "NDX": 0.35, "IWM": 0.20},
}

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

def clamp_score(value, min_value=-1.0, max_value=1.0):
    return max(min_value, min(max_value, value))

def parse_timestamp(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None

def estimate_flip_from_profile(profile_data, spot=0):
    """
    Estimate a usable flip reference from the current signed GEX profile.

    The high-confidence case is an interpolated zero crossing of cumulative
    signed GEX. If there is no crossing in the collected strike range, return a
    lower-confidence proxy from the strongest opposite-sign strike cluster.
    """
    strikes_gex = {}
    for row in profile_data or []:
        strike = row.get('strike_price')
        gex = row.get('gex_value', 0) or 0
        if strike is not None:
            strikes_gex[strike] = strikes_gex.get(strike, 0) + gex

    strikes = sorted(strikes_gex.keys())
    if not strikes:
        return {
            "strike": 0,
            "quality": "missing",
            "confidence": 0.0,
            "note": "No profile data"
        }

    running_total = 0
    prev_total = 0
    prev_strike = strikes[0]
    closest_balance = {"strike": strikes[0], "abs_cum": None}

    for i, strike in enumerate(strikes):
        running_total += strikes_gex[strike]
        abs_cum = abs(running_total)
        if closest_balance["abs_cum"] is None or abs_cum < closest_balance["abs_cum"]:
            closest_balance = {"strike": strike, "abs_cum": abs_cum}

        if i == 0:
            prev_total = running_total
            prev_strike = strike
            continue

        if (prev_total < 0 <= running_total) or (prev_total > 0 >= running_total):
            span = running_total - prev_total
            if span == 0:
                flip = strike
            else:
                ratio = abs(prev_total) / abs(span)
                flip = prev_strike + ((strike - prev_strike) * ratio)
            return {
                "strike": flip,
                "quality": "crossing",
                "confidence": 1.0,
                "note": "Interpolated cumulative GEX zero crossing"
            }

        prev_total = running_total
        prev_strike = strike

    total_gex = sum(strikes_gex.values())
    opposite_sign = -1 if total_gex > 0 else 1
    candidates = [
        (strike, gex) for strike, gex in strikes_gex.items()
        if gex != 0 and (1 if gex > 0 else -1) == opposite_sign
    ]

    if candidates:
        def candidate_score(item):
            strike, gex = item
            distance_penalty = 1
            if spot:
                distance_penalty += abs(strike - spot) / max(abs(spot), 1)
            return abs(gex) / distance_penalty

        strike, _ = max(candidates, key=candidate_score)
        return {
            "strike": strike,
            "quality": "proxy",
            "confidence": 0.55,
            "note": "No zero crossing; using strongest opposing GEX cluster"
        }

    return {
        "strike": closest_balance["strike"],
        "quality": "edge",
        "confidence": 0.35,
        "note": "No zero crossing or opposing cluster in scanned strikes"
    }

def calculate_gex_imbalance_score(net_gex, call_gex, put_gex):
    import math

    gross_gex = abs(call_gex or 0) + abs(put_gex or 0)
    if gross_gex == 0:
        return 0, 0

    imbalance = clamp_score((net_gex or 0) / gross_gex)
    return math.tanh(2.0 * imbalance), imbalance

def calculate_component_confidence(row, profile_count, flip_state, gross_gex):
    score = 1.0
    warnings = []

    if profile_count < 20:
        score -= 0.20
        warnings.append("thin option profile")

    if gross_gex <= 0:
        score -= 0.35
        warnings.append("missing gross GEX")

    flip_quality = flip_state.get("quality")
    if flip_quality == "proxy":
        score -= 0.20
        warnings.append("flip is proxy")
    elif flip_quality == "edge":
        score -= 0.35
        warnings.append("flip outside observed range")
    elif flip_quality == "missing":
        score -= 0.45
        warnings.append("missing flip")

    ts = parse_timestamp(getattr(row, 'timestamp', None))
    age_seconds = None
    if ts:
        age_seconds = max(0, (datetime.now() - ts).total_seconds())
        if age_seconds > 15 * 60:
            score -= 0.25
            warnings.append("stale snapshot")
        elif age_seconds > 7 * 60:
            score -= 0.10
            warnings.append("aging snapshot")
    else:
        score -= 0.15
        warnings.append("unknown snapshot age")

    return {
        "score": clamp_score(score, 0.0, 1.0),
        "warnings": warnings,
        "age_seconds": age_seconds
    }

def calculate_gex_slope(spot, profile_data):
    """
    Calculates the GEX Gradient (Slope) at the current spot price.
    Tells us how fast hedging requirements change as price moves.
    """
    if not profile_data or spot == 0:
        return 0

    # 1. Aggregate GEX by strike
    strikes_gex = {}
    for row in profile_data:
        s = row.get('strike_price')
        if s is not None:
            strikes_gex[s] = strikes_gex.get(s, 0) + row.get('gex_value', 0)

    sorted_strikes = sorted(strikes_gex.keys())
    if len(sorted_strikes) < 2:
        return 0

    # 2. Find strikes surrounding spot
    import bisect
    idx = bisect.bisect_left(sorted_strikes, spot)

    # Get two nearest strikes
    if idx == 0:
        s1, s2 = sorted_strikes[0], sorted_strikes[1]
    elif idx >= len(sorted_strikes):
        s1, s2 = sorted_strikes[-2], sorted_strikes[-1]
    else:
        s1, s2 = sorted_strikes[idx-1], sorted_strikes[idx]

    g1, g2 = strikes_gex[s1], strikes_gex[s2]

    # Slope = Rate of change of GEX per dollar
    return (g2 - g1) / (s2 - s1) if s2 != s1 else 0

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
engine = initialize_database(allow_legacy_on_lock=True)
DB_SCHEMA_CURRENT = schema_is_current()
if not DB_SCHEMA_CURRENT:
    print("Legacy database schema is still active. Close other DB users and run: python publicData.py --reset-db")

def _load_settings() -> dict:
    try:
        with open('settings.json', encoding='utf-8') as f:
            settings = json.load(f)
    except FileNotFoundError:
        settings = {}
    merged = DEFAULT_SETTINGS.copy()
    merged.update(settings)
    return merged

def _normalized_composition(target_weights: dict) -> str:
    total = sum(float(w or 0) for w in target_weights.values())
    if total <= 0:
        return "No active weights"
    return ", ".join(f"{s}: {round((float(w) / total) * 100)}%" for s, w in target_weights.items())

@eel.expose
def get_symbols() -> list[str]:
    """Returns a list of unique symbols available in the database.

    Queries the `raw_option_greeks` table for distinct symbols.

    Returns:
        A list of symbol strings (e.g., ["SPY", "QQQ"]).
    """
    with engine.connect() as conn:
        result = conn.execute(text("SELECT DISTINCT symbol FROM gex_snapshots ORDER BY symbol ASC"))
        return [r[0] for r in result]

@eel.expose
def get_settings() -> dict:
    """Reads and returns the current application settings.

    Returns:
        A dictionary containing settings from `settings.json`.
    """
    return _load_settings()

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
        with open('settings.json', 'r', encoding='utf-8') as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {}

    # Merge new settings into existing
    existing.update(new_settings)

    with open('settings.json', 'w', encoding='utf-8') as f:
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
            # 1. Get Latest Snapshot
            query_snap = text("""
                SELECT *
                FROM gex_snapshots
                WHERE symbol = :symbol
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            snap_row = conn.execute(query_snap, {"symbol": symbol}).fetchone()

            if not snap_row:
                return {
                    "error": f"No data found for {symbol}. Run publicData.py for strict target-day 0DTE collection.",
                    "snapshot": None,
                    "profile": [],
                    "history": []
                }

            latest_time = snap_row.timestamp

            # 2. Fetch Profile Data (For the Bar Chart & Table)
            # We need raw rows to separate Calls vs Puts in JS
            if DB_SCHEMA_CURRENT:
                query_profile = text("""
                    SELECT strike_price, option_type, gex_value, open_interest
                    FROM raw_option_greeks
                    WHERE snapshot_id = :snapshot_id
                    ORDER BY strike_price ASC
                """)
                df_profile = pd.read_sql(query_profile, conn, params={"snapshot_id": snap_row.id})
            else:
                query_profile = text("""
                    SELECT strike_price, option_type, gex_value, open_interest
                    FROM raw_option_greeks
                    WHERE symbol = :symbol AND timestamp = :ts
                    ORDER BY strike_price ASC
                """)
                df_profile = pd.read_sql(query_profile, conn, params={"symbol": symbol, "ts": latest_time})

            # Convert Row to Dict safely
            spot = snap_row.spot_price or 0
            snapshot = {
                "symbol": symbol,
                "timestamp": str(latest_time),
                "spot_price": spot,
                "total_net_gex": snap_row.total_net_gex or 0,
                "max_call_gex_strike": snap_row.max_call_gex_strike or 0,
                "max_put_gex_strike": snap_row.max_put_gex_strike or 0,
                "gex_slope": calculate_gex_slope(spot, df_profile.to_dict(orient='records'))
            }

            # 4. Fetch History (For the Line Chart)
            query_history = text("""
                SELECT timestamp, total_net_gex, spot_price
                FROM (
                    SELECT timestamp, total_net_gex, spot_price
                    FROM gex_snapshots
                    WHERE symbol = :symbol
                    ORDER BY timestamp DESC
                    LIMIT 100
                )
                ORDER BY timestamp ASC
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
        import math

        settings = _load_settings()

        # Defaults if keys missing in settings
        weights_traders = settings.get('weights', {"SPY": 0.5, "QQQ": 0.3, "IWM": 0.2})
        weights_whale = settings.get('weights_whale', {"SPX": 0.45, "NDX": 0.35, "IWM": 0.20})

        overview_data = {
            "compass_traders": {},
            "compass_whale": {},
            "components": [],
            "tilt": [],
            "gamma_levels": {"NDX": [], "SPX": []}
        }

        def _gamma_levels_for_symbol(symbol, conn, per_side=5):
            snap_row = conn.execute(
                text("SELECT * FROM gex_snapshots WHERE symbol = :symbol ORDER BY timestamp DESC LIMIT 1"),
                {"symbol": symbol}
            ).fetchone()
            if not snap_row:
                return []

            if DB_SCHEMA_CURRENT:
                query_levels = text("""
                    SELECT strike_price, SUM(gex_value) AS net_gex
                    FROM raw_option_greeks
                    WHERE snapshot_id = :snapshot_id
                    GROUP BY strike_price
                """)
                level_rows = conn.execute(query_levels, {"snapshot_id": snap_row.id}).fetchall()
            else:
                query_levels = text("""
                    SELECT strike_price, SUM(gex_value) AS net_gex
                    FROM raw_option_greeks
                    WHERE symbol = :symbol AND timestamp = :ts
                    GROUP BY strike_price
                """)
                level_rows = conn.execute(query_levels, {"symbol": symbol, "ts": snap_row.timestamp}).fetchall()

            spot = getattr(snap_row, 'spot_price', 0) or 0
            below = []
            above = []
            for row in level_rows:
                gex = getattr(row, 'net_gex', 0) or 0
                strike = getattr(row, 'strike_price', 0) or 0
                if strike <= 0 or gex == 0:
                    continue

                item = {
                    "strike": strike,
                    "gex": gex,
                    "type": "resistance" if gex > 0 else "support",
                }
                if strike < spot:
                    below.append(item)
                else:
                    above.append(item)

            selected = (
                sorted(below, key=lambda item: abs(item["gex"]), reverse=True)[:per_side] +
                sorted(above, key=lambda item: abs(item["gex"]), reverse=True)[:per_side]
            )
            return sorted(selected, key=lambda item: item["strike"])

        def _calculate_compass_state(target_weights, conn):
            x_score_sum = 0
            y_score_sum = 0
            total_weight = 0
            components = []

            # Formatting composition string
            composition_str = _normalized_composition(target_weights)

            for symbol, weight in target_weights.items():
                # Fetch latest snapshot
                query = text("SELECT * FROM gex_snapshots WHERE symbol = :symbol ORDER BY timestamp DESC LIMIT 1")
                row = conn.execute(query, {"symbol": symbol}).fetchone()

                if row:
                    # Safe Extraction
                    net_gex = getattr(row, 'total_net_gex', 0)
                    call_gex = getattr(row, 'total_call_gex', 0) or 0
                    put_gex = getattr(row, 'total_put_gex', 0) or 0
                    spot = getattr(row, 'spot_price', 0)
                    stored_flip = getattr(row, 'flip_strike', 0) or 0
                    eff_gex = getattr(row, 'effective_gex', 0)
                    # Fetch Profile for slope calculation
                    if DB_SCHEMA_CURRENT:
                        query_profile = text("""
                            SELECT strike_price, gex_value
                            FROM raw_option_greeks
                            WHERE snapshot_id = :snapshot_id
                        """)
                        profile_rows = conn.execute(query_profile, {"snapshot_id": row.id}).fetchall()
                    else:
                        query_profile = text("""
                            SELECT strike_price, gex_value
                            FROM raw_option_greeks
                            WHERE symbol = :symbol AND timestamp = :ts
                        """)
                        profile_rows = conn.execute(query_profile, {"symbol": symbol, "ts": row.timestamp}).fetchall()
                    profile_data = [{"strike_price": r.strike_price, "gex_value": r.gex_value} for r in profile_rows]
                    acceleration = calculate_gex_slope(spot, profile_data)

                    flip_state = estimate_flip_from_profile(profile_data, spot)
                    if flip_state["strike"] == 0 and stored_flip > 0:
                        flip_state = {
                            "strike": stored_flip,
                            "quality": "stored",
                            "confidence": 0.70,
                            "note": "Stored collector flip"
                        }
                    flip = flip_state["strike"]
                    gross_gex = abs(call_gex) + abs(put_gex)

                    # --- 1. TREND SCORE (Y-AXIS) ---
                    # Uses the 0DTE sensitivity logic, damped when the flip is approximate.
                    if flip and flip > 0:
                        dist_pct = ((spot - flip) / flip) * 100
                        trend_score = calculate_0dte_trend_score(spot, flip, symbol) * flip_state["confidence"]
                    else:
                        dist_pct = 0
                        trend_score = 0

                    # --- 2. VOL SCORE (X-AXIS) ---
                    # Net-vs-gross imbalance keeps tiny and massive one-sided
                    # profiles from receiving the same score.
                    vol_score, gex_imbalance = calculate_gex_imbalance_score(net_gex, call_gex, put_gex)
                    quality = calculate_component_confidence(row, len(profile_data), flip_state, gross_gex)

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
                        "acceleration": acceleration,
                        "vol_score": vol_score,
                        "trend_score": trend_score,
                        "gex_imbalance": gex_imbalance,
                        "gross_gex": gross_gex,
                        "confidence": quality["score"],
                        "warnings": quality["warnings"],
                        "age_seconds": quality["age_seconds"],
                        "flip_quality": flip_state["quality"],
                        "flip_note": flip_state["note"]
                    })

            if not components:
                return {
                    "x_score": 0,
                    "y_score": 0,
                    "label": "NO DATA",
                    "strategy": "Run the strict target-day 0DTE collector to populate this view.",
                    "confidence": 0,
                    "confidence_label": "NO DATA",
                    "warnings": ["no active components"],
                    "composition": composition_str,
                    "raw_components": []
                }

            # --- FINAL COMPASS CALCULATION ---
            if total_weight > 0:
                final_vol = x_score_sum / total_weight
                final_trend = y_score_sum / total_weight
                weighted_confidence = sum([c['confidence'] * target_weights.get(c['symbol'], 0) for c in components])
                confidence = weighted_confidence / total_weight
            else:
                final_vol, final_trend, confidence = 0, 0, 0

            # Magnitude
            magnitude = math.sqrt(final_vol**2 + final_trend**2)

            # Determine Quadrant
            is_pos_gex = final_vol > 0
            is_bull_trend = final_trend > 0

            # --- REGIME CONTEXT LOGIC ---
            base_lbl, base_strat, base_icon = "", "", ""

            if is_pos_gex:
                if is_bull_trend:
                    base_lbl = "GRIND UP"
                    base_strat = "Positive gamma with spot above flip. Favor controlled upside and mean-reversion on pullbacks."
                    base_icon = "🟢"
                else:
                    base_lbl = "SUPPORT / CHOP"
                    base_strat = "Positive gamma below flip. Favor range discipline and mean-reversion; watch for failed breakdowns."
                    base_icon = "⚪"
            else:
                # Negative Gamma
                if is_bull_trend:
                    base_lbl = "MELT UP"
                    base_strat = "Negative gamma with spot above flip. Favor momentum and upside range expansion; avoid early fades."
                    base_icon = "🟡"
                else:
                    base_lbl = "CRASH / FLUSH"
                    base_strat = "Negative gamma below flip. Watch for downside range expansion; momentum can persist."
                    base_icon = "🔴"

            # Inner Ring Check
            inner_ring_threshold = 0.25
            base_icon = ""
            warnings = sorted({warning for c in components for warning in c.get("warnings", [])})
            if magnitude < inner_ring_threshold:
                warnings.append("low regime separation")

            if confidence < 0.45:
                confidence_label = "LOW"
            elif confidence < 0.70:
                confidence_label = "MEDIUM"
            else:
                confidence_label = "HIGH"

            if magnitude < inner_ring_threshold or confidence < 0.60:
                label = f"LOW CONFIDENCE {base_lbl}"
                strategy = f"{base_strat} Confirm with price action; data quality is reduced."
            else:
                label = base_lbl
                strategy = base_strat

            return {
                "x_score": final_vol,
                "y_score": final_trend,
                "label": label,
                "strategy": strategy,
                "confidence": confidence,
                "confidence_label": confidence_label,
                "warnings": warnings,
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
                    "regime": data['regime'],
                    "acceleration": data.get('acceleration', 0),
                    "vol_score": data.get('vol_score', 0),
                    "trend_score": data.get('trend_score', 0),
                    "gex_imbalance": data.get('gex_imbalance', 0),
                    "gross_gex": data.get('gross_gex', 0),
                    "confidence": data.get('confidence', 0),
                    "warnings": data.get('warnings', []),
                    "age_seconds": data.get('age_seconds'),
                    "flip_quality": data.get('flip_quality', 'missing'),
                    "flip_note": data.get('flip_note', '')
                })
                # Add Tilt Data
                overview_data["tilt"].append({
                    "symbol": data['symbol'],
                    "net_gex": data.get('effective_gex', 0)
                })

            for idx_symbol in ["NDX", "SPX"]:
                overview_data["gamma_levels"][idx_symbol] = _gamma_levels_for_symbol(idx_symbol, conn)

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
def trigger_data_refresh() -> dict:
    """Invokes the data collector script (publicData.py) immediately.

    Spawns a subprocess using the current Python interpreter.

    Returns:
        A structured result from the collector.
    """
    import subprocess
    import sys
    try:
        print("Triggering data refresh...")
        # Run publicData.py using the same python interpreter
        proc = subprocess.run([sys.executable, "publicData.py", "--once"], capture_output=True, text=True)
        output = (proc.stdout or "").strip().splitlines()
        if output:
            try:
                result = json.loads(output[-1])
                print(f"Data refresh complete: {result.get('message')}")
                return result
            except json.JSONDecodeError:
                pass
        message = (proc.stderr or proc.stdout or "Collector finished without a structured result.").strip()
        return {"ok": proc.returncode == 0, "message": message, "run_id": None}
    except Exception as e:
        print(f"Failed to refresh data: {e}")
        return {"ok": False, "message": str(e), "run_id": None}

# --- Run App ---
if __name__ == '__main__':
    try:
        eel.start('index.html', size=(1500, 900), port=8080)
    except OSError:
        eel.start('index.html', mode='edge', size=(1500, 900), port=8080)
