import eel
import pandas as pd
import atexit
import json
import re
import socket
import subprocess
import sys
import threading
from datetime import date, datetime, time as dt_time, timedelta
from pathlib import Path
from sqlalchemy import text

from backtest_gamma_butterflies import (
    DEFAULT_PIT_WALL_COUNT,
    _available_symmetric_width,
    _center_strike,
    _leg_price,
    _price_fly,
    _score_gamma_wall_pit_levels,
    _score_hybrid_levels,
    _strike_key,
    _strike_summary,
    _rows_by_strike_and_side,
)
from gamma_sweep import build_gamma_sweep
from gex_levels import aggregate_gamma_levels
from models import get_engine, initialize_database, schema_is_current

# --- Configuration ---
eel.init('web')
APP_ROOT = Path(__file__).resolve().parent
ONE_OFF_DB_PATH = APP_ROOT / "one_off_gex_data.db"
collector_process = None

DEFAULT_SETTINGS = {
    "refresh_interval": 10,
    "theme": "dark",
    "symbols": ["SPY"],
    "api_rate_limit_per_second": 10.0,
    "api_rate_limit_utilization": 0.6,
    "min_poll_interval_seconds": 15,
    "max_poll_interval_seconds": 120,
    "raw_retention_days": 30,
    "weights": {"SPY": 1.0},
    "weights_whale": {"SPX": 0.45, "NDX": 0.35, "IWM": 0.20},
}

# --- Event/Notification Server ---

def start_collector_process():
    """Start the Public.com collector as a child of the dashboard process."""
    global collector_process
    if collector_process and collector_process.poll() is None:
        return collector_process

    print("Starting Public.com collector backend...")
    collector_process = subprocess.Popen(
        [sys.executable, "publicData.py"],
        cwd=str(APP_ROOT),
    )
    return collector_process


def stop_collector_process():
    """Stop the dashboard-owned collector process, if it is still running."""
    global collector_process
    if not collector_process or collector_process.poll() is not None:
        return

    print("Stopping Public.com collector backend...")
    collector_process.terminate()
    try:
        collector_process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        print("Collector did not stop quickly; forcing shutdown.")
        collector_process.kill()
        collector_process.wait(timeout=5)


atexit.register(stop_collector_process)

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
                client_sock.settimeout(5)
                chunks = []
                while True:
                    try:
                        chunk = client_sock.recv(65536)
                    except socket.timeout:
                        break
                    if not chunk:
                        break
                    chunks.append(chunk)

                data = b"".join(chunks)
                if data:
                    # Decode and parse
                    msg = json.loads(data.decode('utf-8'))
                    print(f"Event received: {msg.get('type', 'UNKNOWN')}")

                    # 1. Handle Market Updates (Forward canonical dashboard state to NinjaTrader)
                    if msg.get('type') == 'MARKET_UPDATE' and 'data' in msg:
                        try:
                            from ninjatrader_broadcaster import send_regime_update
                            overview_data = get_market_overview()
                            if overview_data.get("error"):
                                raise RuntimeError(overview_data["error"])
                            msg['data'] = overview_data
                            send_regime_update(overview_data)
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

def _validate_settings(settings: dict) -> dict:
    symbols = settings.get("symbols", [])
    if not isinstance(symbols, list) or not [str(s).strip() for s in symbols]:
        raise ValueError("At least one symbol is required.")

    normalized_symbols = []
    for symbol in symbols:
        symbol_text = str(symbol).strip().upper()
        if not symbol_text:
            continue
        if not symbol_text.replace(".", "").replace("-", "").isalnum():
            raise ValueError(f"Invalid symbol: {symbol}")
        normalized_symbols.append(symbol_text)

    refresh_interval = max(5, int(settings.get("refresh_interval", DEFAULT_SETTINGS["refresh_interval"])))
    rate_limit = max(0.1, float(settings.get("api_rate_limit_per_second", DEFAULT_SETTINGS["api_rate_limit_per_second"])))
    utilization = float(settings.get("api_rate_limit_utilization", DEFAULT_SETTINGS["api_rate_limit_utilization"]))
    if utilization < 0.1 or utilization > 1.0:
        raise ValueError("Limit utilization must be between 0.1 and 1.0.")

    min_poll = max(1, int(settings.get("min_poll_interval_seconds", DEFAULT_SETTINGS["min_poll_interval_seconds"])))
    max_poll = int(settings.get("max_poll_interval_seconds", DEFAULT_SETTINGS["max_poll_interval_seconds"]))
    if max_poll < min_poll:
        raise ValueError("Maximum poll seconds must be greater than or equal to minimum poll seconds.")

    retention_days = max(1, int(settings.get("raw_retention_days", DEFAULT_SETTINGS["raw_retention_days"])))

    settings["symbols"] = normalized_symbols
    settings["refresh_interval"] = refresh_interval
    settings["api_rate_limit_per_second"] = rate_limit
    settings["api_rate_limit_utilization"] = utilization
    settings["min_poll_interval_seconds"] = min_poll
    settings["max_poll_interval_seconds"] = max_poll
    settings["raw_retention_days"] = retention_days
    settings["theme"] = settings.get("theme") if settings.get("theme") in {"dark", "light"} else DEFAULT_SETTINGS["theme"]
    return settings

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
def save_settings(new_settings: dict) -> dict:
    """Updates the settings.json file with new values.

    Merges the provided settings into the existing file to preserve
    keys that are not present in `new_settings`.

    Args:
        new_settings: A dictionary of settings to update.

    Returns:
        A result object with ok/message/settings fields.
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
    try:
        existing = _validate_settings(existing)
    except (TypeError, ValueError) as e:
        return {"ok": False, "message": str(e)}

    with open('settings.json', 'w', encoding='utf-8') as f:
        json.dump(existing, f, indent=2)
    print(f"Settings merged and saved: {existing.keys()}")
    return {"ok": True, "message": "Settings saved.", "settings": existing}

@eel.expose
def get_backend_status() -> dict:
    """Returns latest collector and snapshot freshness from the local DB."""
    try:
        with engine.connect() as conn:
            run = conn.execute(text("""
                SELECT id, started_at, finished_at, status, message
                FROM collection_runs
                ORDER BY started_at DESC
                LIMIT 1
            """)).fetchone()
            snap = conn.execute(text("""
                SELECT symbol, timestamp
                FROM gex_snapshots
                ORDER BY timestamp DESC
                LIMIT 1
            """)).fetchone()

        now = datetime.now()
        latest_snapshot_at = getattr(snap, "timestamp", None) if snap else None
        latest_run_started = getattr(run, "started_at", None) if run else None
        latest_run_finished = getattr(run, "finished_at", None) if run else None

        snapshot_age_seconds = None
        if latest_snapshot_at:
            parsed_snapshot_at = parse_timestamp(latest_snapshot_at)
            if parsed_snapshot_at:
                snapshot_age_seconds = max(0, (now - parsed_snapshot_at).total_seconds())

        run_age_seconds = None
        if latest_run_started:
            parsed_run_started = parse_timestamp(latest_run_started)
            if parsed_run_started:
                run_age_seconds = max(0, (now - parsed_run_started).total_seconds())

        return {
            "ok": bool(run),
            "run_id": getattr(run, "id", None) if run else None,
            "run_status": getattr(run, "status", None) if run else None,
            "run_message": getattr(run, "message", None) if run else None,
            "run_started_at": str(latest_run_started) if latest_run_started else None,
            "run_finished_at": str(latest_run_finished) if latest_run_finished else None,
            "run_age_seconds": run_age_seconds,
            "latest_symbol": getattr(snap, "symbol", None) if snap else None,
            "latest_snapshot_at": str(latest_snapshot_at) if latest_snapshot_at else None,
            "snapshot_age_seconds": snapshot_age_seconds,
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}

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
    return _dashboard_data_from_engine(engine, symbol, DB_SCHEMA_CURRENT)


def _dashboard_data_from_engine(db_engine, symbol: str, schema_current: bool = True, snapshot_id: int | None = None) -> dict:
    symbol = str(symbol or "").strip().upper()
    try:
        with db_engine.connect() as conn:
            if snapshot_id:
                query_snap = text("""
                    SELECT *
                    FROM gex_snapshots
                    WHERE id = :snapshot_id
                    LIMIT 1
                """)
                snap_row = conn.execute(query_snap, {"snapshot_id": snapshot_id}).fetchone()
            else:
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
                    "error": f"No data found for {symbol}.",
                    "snapshot": None,
                    "profile": [],
                    "history": []
                }

            latest_time = snap_row.timestamp
            symbol = snap_row.symbol or symbol

            if schema_current:
                query_profile = text("""
                    SELECT strike_price, option_type, delta, gamma, gex_value, open_interest, underlying_price, expiration_date
                    FROM raw_option_greeks
                    WHERE snapshot_id = :snapshot_id
                    ORDER BY strike_price ASC
                """)
                df_profile = pd.read_sql(query_profile, conn, params={"snapshot_id": snap_row.id})
            else:
                query_profile = text("""
                    SELECT strike_price, option_type, gex_value, open_interest, expiration_date
                    FROM raw_option_greeks
                    WHERE symbol = :symbol AND timestamp = :ts
                    ORDER BY strike_price ASC
                """)
                df_profile = pd.read_sql(query_profile, conn, params={"symbol": symbol, "ts": latest_time})

            spot = snap_row.spot_price or 0
            expiration_date = None
            if not df_profile.empty and "expiration_date" in df_profile:
                expiration_date = str(df_profile["expiration_date"].dropna().iloc[0]) if not df_profile["expiration_date"].dropna().empty else None

            snapshot = {
                "id": snap_row.id,
                "symbol": symbol,
                "timestamp": str(latest_time),
                "expiration_date": expiration_date,
                "spot_price": spot,
                "total_net_gex": snap_row.total_net_gex or 0,
                "total_call_gex": snap_row.total_call_gex or 0,
                "total_put_gex": snap_row.total_put_gex or 0,
                "max_call_gex_strike": snap_row.max_call_gex_strike or 0,
                "max_put_gex_strike": snap_row.max_put_gex_strike or 0,
                "flip_strike": snap_row.flip_strike or 0,
                "effective_gex": snap_row.effective_gex or 0,
                "gex_slope": calculate_gex_slope(spot, df_profile.to_dict(orient='records'))
            }

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
            if "timestamp" in df_hist:
                df_hist['timestamp'] = df_hist['timestamp'].apply(lambda x: str(x))

            return {
                "snapshot": snapshot,
                "profile": df_profile.to_dict(orient='records'),
                "history": df_hist.to_dict(orient='records'),
                "gamma_sweep": build_gamma_sweep(df_profile.to_dict(orient='records'), spot, symbol),
            }

    except Exception as e:
        print(f"Error: {e}")
        return {"error": str(e)}


@eel.expose
def get_trace_data(symbol: str = "SPX", minutes: int = 390) -> dict:
    """Returns a TRACE-style intraday gamma heatmap grid for one symbol."""
    symbol = str(symbol or "").strip().upper()
    try:
        lookback_minutes = max(30, min(int(minutes or 390), 480))
    except (TypeError, ValueError):
        lookback_minutes = 390

    try:
        with engine.connect() as conn:
            latest = conn.execute(
                text("""
                    SELECT id, timestamp, symbol, spot_price, flip_strike, total_net_gex
                    FROM gex_snapshots
                    WHERE symbol = :symbol
                    ORDER BY timestamp DESC
                    LIMIT 1
                """),
                {"symbol": symbol},
            ).fetchone()

            if not latest:
                return {"error": f"No data found for {symbol}.", "symbol": symbol}

            latest_time = parse_timestamp(latest.timestamp) or datetime.now()
            start_time = latest_time - timedelta(minutes=lookback_minutes)

            heatmap_rows = conn.execute(
                text("""
                    SELECT
                        strftime('%Y-%m-%d %H:%M:00', r.timestamp) AS bucket,
                        r.strike_price,
                        SUM(r.gex_value) AS net_gex,
                        SUM(CASE WHEN UPPER(r.option_type) LIKE '%CALL%' THEN r.gex_value ELSE 0 END) AS call_gex,
                        SUM(CASE WHEN UPPER(r.option_type) LIKE '%PUT%' THEN r.gex_value ELSE 0 END) AS put_gex,
                        SUM(r.open_interest) AS open_interest
                    FROM raw_option_greeks r
                    WHERE r.symbol = :symbol
                      AND r.timestamp >= :start_time
                      AND date(r.timestamp) = date(:latest_time)
                    GROUP BY bucket, r.strike_price
                    ORDER BY bucket ASC, r.strike_price ASC
                """),
                {
                    "symbol": symbol,
                    "start_time": start_time,
                    "latest_time": latest_time,
                },
            ).fetchall()

            spot_rows = conn.execute(
                text("""
                    SELECT
                        strftime('%Y-%m-%d %H:%M:00', timestamp) AS bucket,
                        AVG(spot_price) AS spot_price
                    FROM gex_snapshots
                    WHERE symbol = :symbol
                      AND timestamp >= :start_time
                      AND date(timestamp) = date(:latest_time)
                    GROUP BY bucket
                    ORDER BY bucket ASC
                """),
                {
                    "symbol": symbol,
                    "start_time": start_time,
                    "latest_time": latest_time,
                },
            ).fetchall()

            spot_tick_rows = conn.execute(
                text("""
                    SELECT timestamp, spot_price
                    FROM gex_snapshots
                    WHERE symbol = :symbol
                      AND timestamp >= :start_time
                      AND date(timestamp) = date(:latest_time)
                    ORDER BY timestamp ASC
                """),
                {
                    "symbol": symbol,
                    "start_time": start_time,
                    "latest_time": latest_time,
                },
            ).fetchall()

            latest_profile_rows = conn.execute(
                text("""
                    SELECT
                        strike_price,
                        SUM(gex_value) AS net_gex,
                        SUM(CASE WHEN UPPER(option_type) LIKE '%CALL%' THEN gex_value ELSE 0 END) AS call_gex,
                        SUM(CASE WHEN UPPER(option_type) LIKE '%PUT%' THEN gex_value ELSE 0 END) AS put_gex,
                        SUM(open_interest) AS open_interest
                    FROM raw_option_greeks
                    WHERE snapshot_id = :snapshot_id
                    GROUP BY strike_price
                    ORDER BY strike_price ASC
                """),
                {"snapshot_id": latest.id},
            ).fetchall()

        heatmap = [
            {
                "timestamp": str(row.bucket),
                "time": str(row.bucket)[11:16],
                "strike": float(row.strike_price or 0),
                "net_gex": float(row.net_gex or 0),
                "call_gex": float(row.call_gex or 0),
                "put_gex": float(row.put_gex or 0),
                "open_interest": int(row.open_interest or 0),
            }
            for row in heatmap_rows
            if row.strike_price is not None
        ]
        spot_path = [
            {
                "timestamp": str(row.bucket),
                "time": str(row.bucket)[11:16],
                "spot_price": float(row.spot_price or 0),
            }
            for row in spot_rows
        ]
        spot_ticks = [
            {
                "timestamp": str(row.timestamp),
                "time": str(row.timestamp)[11:16],
                "spot_price": float(row.spot_price or 0),
            }
            for row in spot_tick_rows
        ]
        latest_profile = [
            {
                "strike": float(row.strike_price or 0),
                "net_gex": float(row.net_gex or 0),
                "call_gex": float(row.call_gex or 0),
                "put_gex": float(row.put_gex or 0),
                "open_interest": int(row.open_interest or 0),
            }
            for row in latest_profile_rows
            if row.strike_price is not None
        ]
        strikes = sorted({row["strike"] for row in heatmap})
        buckets = sorted({row["timestamp"] for row in heatmap})
        values = [row["net_gex"] for row in heatmap]

        return {
            "symbol": symbol,
            "timestamp": str(latest.timestamp),
            "spot_price": float(latest.spot_price or 0),
            "flip_strike": float(latest.flip_strike or 0),
            "total_net_gex": float(latest.total_net_gex or 0),
            "range": {
                "start": str(start_time),
                "end": str(latest_time),
                "minutes": lookback_minutes,
                "buckets": len(buckets),
                "strikes": len(strikes),
                "cells": len(heatmap),
                "min_strike": min(strikes) if strikes else None,
                "max_strike": max(strikes) if strikes else None,
                "min_net_gex": min(values) if values else 0,
                "max_net_gex": max(values) if values else 0,
            },
            "heatmap": heatmap,
            "spot_path": spot_path,
            "spot_ticks": spot_ticks,
            "latest_profile": latest_profile,
        }

    except Exception as e:
        print(f"TRACE data error: {e}")
        return {"error": str(e), "symbol": symbol}


def _parse_one_off_date(value: str) -> date:
    raw = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%m-%d-%y", "%m/%d/%y", "%m-%d-%Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    raise ValueError("Date must be like 07-07-26 or 2026-07-07.")


def _validate_one_off_symbol(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        raise ValueError("Symbol is required.")
    if not re.fullmatch(r"[A-Z0-9.-]{1,12}", normalized):
        raise ValueError("Symbol can only contain letters, numbers, dots, and hyphens.")
    return normalized


def _ensure_one_off_index(conn) -> None:
    conn.exec_driver_sql("""
        CREATE TABLE IF NOT EXISTS one_off_profile_index (
            symbol TEXT NOT NULL,
            expiration_date TEXT NOT NULL,
            snapshot_id INTEGER,
            run_id INTEGER,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (symbol, expiration_date)
        )
    """)
    conn.exec_driver_sql("""
        INSERT OR IGNORE INTO one_off_profile_index (
            symbol,
            expiration_date,
            snapshot_id,
            run_id,
            updated_at
        )
        WITH grouped AS (
            SELECT
                s.symbol AS symbol,
                MIN(r.expiration_date) AS expiration_date,
                s.id AS snapshot_id,
                s.collection_run_id AS run_id,
                s.timestamp AS timestamp
            FROM gex_snapshots s
            JOIN raw_option_greeks r ON r.snapshot_id = s.id
            GROUP BY s.id
        )
        SELECT
            g.symbol,
            g.expiration_date,
            g.snapshot_id,
            g.run_id,
            g.timestamp
        FROM grouped g
        WHERE g.expiration_date IS NOT NULL
          AND NOT EXISTS (
              SELECT 1
              FROM grouped newer
              WHERE newer.symbol = g.symbol
                AND newer.expiration_date = g.expiration_date
                AND newer.timestamp > g.timestamp
          )
    """)


@eel.expose
def build_one_off_profile(symbol: str, expiration_date: str) -> dict:
    try:
        normalized_symbol = _validate_one_off_symbol(symbol)
        target_date = _parse_one_off_date(expiration_date)

        from publicData import run_one_off_profile

        result = run_one_off_profile(normalized_symbol, target_date, ONE_OFF_DB_PATH)
        if not result.get("ok"):
            result["db_path"] = str(ONE_OFF_DB_PATH)
            return result

        one_off_engine = get_engine(ONE_OFF_DB_PATH)
        try:
            data = _dashboard_data_from_engine(
                one_off_engine,
                normalized_symbol,
                schema_is_current(ONE_OFF_DB_PATH),
                snapshot_id=result.get("snapshot_id"),
            )
        finally:
            one_off_engine.dispose()
        if data.get("error"):
            return {
                "ok": False,
                "message": data["error"],
                "db_path": str(ONE_OFF_DB_PATH),
                **result,
            }

        return {
            **result,
            "ok": True,
            "db_path": str(ONE_OFF_DB_PATH),
            "data": data,
        }
    except Exception as e:
        return {"ok": False, "message": str(e), "db_path": str(ONE_OFF_DB_PATH)}


@eel.expose
def get_one_off_profiles() -> list[dict]:
    try:
        one_off_engine = initialize_database(db_path=ONE_OFF_DB_PATH)
        try:
            with one_off_engine.begin() as conn:
                _ensure_one_off_index(conn)
                rows = conn.execute(text("""
                    SELECT
                        i.snapshot_id,
                        i.symbol,
                        i.expiration_date,
                        i.updated_at,
                        s.timestamp,
                        COUNT(r.id) AS contract_count,
                        s.spot_price,
                        s.total_net_gex
                    FROM one_off_profile_index i
                    JOIN gex_snapshots s ON s.id = i.snapshot_id
                    LEFT JOIN raw_option_greeks r ON r.snapshot_id = i.snapshot_id
                    GROUP BY i.symbol, i.expiration_date, i.snapshot_id
                    ORDER BY i.updated_at DESC
                    LIMIT 20
                """)).fetchall()
        finally:
            one_off_engine.dispose()
        return [
            dict(row._mapping) | {
                "timestamp": str(row.timestamp),
                "updated_at": str(row.updated_at),
                "expiration_date": str(row.expiration_date),
            }
            for row in rows
        ]
    except Exception as e:
        print(f"Error loading one-off profiles: {e}")
        return []


@eel.expose
def get_one_off_profile(snapshot_id: int) -> dict:
    try:
        one_off_engine = initialize_database(db_path=ONE_OFF_DB_PATH)
        try:
            return _dashboard_data_from_engine(
                one_off_engine,
                "",
                schema_is_current(ONE_OFF_DB_PATH),
                snapshot_id=int(snapshot_id),
            )
        finally:
            one_off_engine.dispose()
    except Exception as e:
        return {"error": str(e)}

def _latest_snapshot_and_raw_rows(conn, symbol: str):
    snap_row = conn.execute(
        text("""
            SELECT *
            FROM gex_snapshots
            WHERE symbol = :symbol
            ORDER BY timestamp DESC
            LIMIT 1
        """),
        {"symbol": symbol},
    ).fetchone()
    if not snap_row:
        return None, []

    if DB_SCHEMA_CURRENT:
        raw_query = text("""
            SELECT
                expiration_date,
                osi_symbol,
                strike_price,
                option_type,
                delta,
                gamma,
                open_interest,
                underlying_price,
                gex_value
            FROM raw_option_greeks
            WHERE snapshot_id = :snapshot_id
            ORDER BY strike_price ASC, option_type ASC
        """)
        raw_rows = conn.execute(raw_query, {"snapshot_id": snap_row.id}).fetchall()
    else:
        raw_query = text("""
            SELECT
                expiration_date,
                osi_symbol,
                strike_price,
                option_type,
                open_interest,
                gex_value
            FROM raw_option_greeks
            WHERE symbol = :symbol AND timestamp = :ts
            ORDER BY strike_price ASC, option_type ASC
        """)
        raw_rows = conn.execute(raw_query, {"symbol": symbol, "ts": snap_row.timestamp}).fetchall()

    return snap_row, [dict(row._mapping) for row in raw_rows]


def _setup_side(center: float, spot: float) -> str:
    return "CALL" if center >= spot else "PUT"


def _build_butterfly_idea(raw_rows: list[dict], summary: dict, spot: float) -> dict:
    center_data = _center_strike(summary, "gex")
    if not center_data:
        return {"status": "unavailable", "reason": "No GEX profile available"}

    center = _strike_key(center_data["strike"])
    width = _available_symmetric_width(center, summary.keys(), None)
    if width is None:
        return {"status": "unavailable", "reason": "No symmetric strikes around the GEX center"}

    lower = _strike_key(center - width)
    upper = _strike_key(center + width)
    side = _setup_side(center, spot)
    priced = _price_fly(
        raw_rows,
        lower,
        center,
        upper,
        spot,
        side=side,
        price_method="greeks",
        min_debit=0.0,
    )
    if not priced:
        return {"status": "unavailable", "reason": f"Could not price {side.lower()} butterfly legs"}

    debit = priced.debit
    max_profit = max(width - debit, 0)
    return {
        "status": "ready",
        "kind": "Butterfly",
        "method": "Largest GEX body",
        "side": side,
        "center": center,
        "lower": lower,
        "upper": upper,
        "width": width,
        "estimated_debit": debit,
        "estimated_debit_dollars": debit * 100,
        "max_profit": max_profit,
        "max_profit_dollars": max_profit * 100,
        "max_loss": debit,
        "lower_breakeven": lower + debit,
        "upper_breakeven": upper - debit,
        "net_gex": center_data["net_gex"],
        "call_gex": center_data["call_gex"],
        "put_gex": center_data["put_gex"],
        "rationale": "Backtest favored the largest GEX strike as the body for pin-sensitive butterflies.",
    }


def _priced_debit_spread(lookup: dict, spot: float, side: str, long_strike: float, short_strike: float):
    long_row = lookup.get((_strike_key(long_strike), side))
    short_row = lookup.get((_strike_key(short_strike), side))
    if not long_row or not short_row:
        return None

    long_price = _leg_price(long_row, spot, side, "greeks")
    short_price = _leg_price(short_row, spot, side, "greeks")
    if long_price is None or short_price is None:
        return None

    debit = long_price - short_price
    if debit <= 0:
        return None
    return {
        "long_price": long_price,
        "short_price": short_price,
        "debit": debit,
    }


def _build_debit_spread_idea(raw_rows: list[dict], summary: dict, spot: float) -> dict:
    center_data = _center_strike(summary, "gamma-pit-walls")
    if not center_data:
        return {"status": "unavailable", "reason": "No major-wall gamma pit available"}

    center = _strike_key(center_data["strike"])
    side = _setup_side(center, spot)
    strikes = sorted(summary.keys())
    if side == "CALL":
        long_candidates = [strike for strike in strikes if strike < center]
        if not long_candidates:
            return {"status": "unavailable", "reason": "No lower strike for call debit spread"}
        long_strike = max(long_candidates)
        short_strike = center
        width = short_strike - long_strike
        breakeven = long_strike
    else:
        long_candidates = [strike for strike in strikes if strike > center]
        if not long_candidates:
            return {"status": "unavailable", "reason": "No upper strike for put debit spread"}
        long_strike = min(long_candidates)
        short_strike = center
        width = long_strike - short_strike
        breakeven = long_strike

    lookup = _rows_by_strike_and_side(raw_rows)
    priced = _priced_debit_spread(lookup, spot, side, long_strike, short_strike)
    if not priced:
        return {"status": "unavailable", "reason": f"Could not price {side.lower()} debit spread legs"}

    debit = priced["debit"]
    max_profit = max(width - debit, 0)
    breakeven = breakeven + debit if side == "CALL" else breakeven - debit
    return {
        "status": "ready",
        "kind": "Debit Spread",
        "method": "Major-wall gamma pit",
        "side": side,
        "long_strike": _strike_key(long_strike),
        "short_strike": _strike_key(short_strike),
        "target": center,
        "width": width,
        "estimated_debit": debit,
        "estimated_debit_dollars": debit * 100,
        "max_profit": max_profit,
        "max_profit_dollars": max_profit * 100,
        "max_loss": debit,
        "breakeven": breakeven,
        "long_price": priced["long_price"],
        "short_price": priced["short_price"],
        "pit_score": center_data["pit_score"],
        "pit_left_wall_strike": center_data["pit_left_wall_strike"],
        "pit_left_wall_gex": center_data["pit_left_wall_gex"],
        "pit_right_wall_strike": center_data["pit_right_wall_strike"],
        "pit_right_wall_gex": center_data["pit_right_wall_gex"],
        "net_gex": center_data["net_gex"],
        "rationale": "Backtest showed major-wall pits were touched intraday more often than they settled as fly bodies.",
    }


@eel.expose
def get_trade_setups(symbol: str = "SPX") -> dict:
    try:
        symbol = str(symbol or "SPX").upper()
        with engine.connect() as conn:
            snap_row, raw_rows = _latest_snapshot_and_raw_rows(conn, symbol)
            if not snap_row:
                return {"error": f"No data found for {symbol}"}
            if not raw_rows:
                return {"error": f"No option rows found for {symbol}"}

            spot = float(getattr(snap_row, "spot_price", 0) or 0)
            timestamp = parse_timestamp(getattr(snap_row, "timestamp", None)) or datetime.now()
            summary = _strike_summary(
                raw_rows,
                spot=spot,
                timestamp=timestamp,
                settlement_time=dt_time(16, 0),
            )
            _score_hybrid_levels(summary, 0.7, 0.3)
            _score_gamma_wall_pit_levels(summary, DEFAULT_PIT_WALL_COUNT)

            profile = [
                {
                    "strike": item["strike"],
                    "net_gex": item["net_gex"],
                    "call_gex": item["call_gex"],
                    "put_gex": item["put_gex"],
                    "net_charm": item["net_charm"],
                    "hybrid_score": item["hybrid_score"],
                    "pit_score": item["pit_score"],
                }
                for item in sorted(summary.values(), key=lambda row: row["strike"])
            ]

            return {
                "symbol": symbol,
                "timestamp": str(getattr(snap_row, "timestamp", "")),
                "snapshot_id": getattr(snap_row, "id", None),
                "spot": spot,
                "pricing_model": "Greek-implied theoretical mid from stored snapshot delta/gamma",
                "pit_wall_count": DEFAULT_PIT_WALL_COUNT,
                "profile": profile,
                "ideas": {
                    "butterfly": _build_butterfly_idea(raw_rows, summary, spot),
                    "debit_spread": _build_debit_spread_idea(raw_rows, summary, spot),
                },
                "backtest_lens": {
                    "butterfly": "GEX-centered flies led the sample.",
                    "debit_spread": "Major-wall pits had the strongest intraday touch profile among pit variants.",
                    "sample_warning": "Small sample; prices are model-implied, not bid/ask fills.",
                },
            }
    except Exception as e:
        print(f"Error in trade setups: {e}")
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
            "gamma_levels": {"NDX": [], "SPX": []},
            "cockpit_levels": {"NDX": [], "SPX": []},
            "modeled_zero_gex": {"NDX": None, "SPX": None},
            "edge_stats": {}
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
                    SELECT strike_price, option_type, gex_value, open_interest
                    FROM raw_option_greeks
                    WHERE snapshot_id = :snapshot_id
                    ORDER BY strike_price
                """)
                level_rows = conn.execute(query_levels, {"snapshot_id": snap_row.id}).fetchall()
            else:
                query_levels = text("""
                    SELECT strike_price, option_type, gex_value, open_interest
                    FROM raw_option_greeks
                    WHERE symbol = :symbol AND timestamp = :ts
                    ORDER BY strike_price
                """)
                level_rows = conn.execute(query_levels, {"symbol": symbol, "ts": snap_row.timestamp}).fetchall()

            spot = getattr(snap_row, 'spot_price', 0) or 0
            return aggregate_gamma_levels(level_rows, spot=spot, per_side=per_side)

        def _nearest_modeled_zero(gamma_sweep: dict, spot: float):
            if not gamma_sweep or gamma_sweep.get("status") != "ok":
                return None

            crossings = gamma_sweep.get("zero_crossings", {}).get("all", []) or []
            finite_crossings = []
            for crossing in crossings:
                try:
                    value = float(crossing)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(value) and value > 0:
                    finite_crossings.append(value)

            if not finite_crossings:
                return None

            try:
                spot_value = float(spot)
            except (TypeError, ValueError):
                spot_value = 0

            if spot_value and math.isfinite(spot_value):
                return min(finite_crossings, key=lambda value: abs(value - spot_value))
            return finite_crossings[0]

        def _modeled_zero_gex_for_symbol(symbol, conn):
            if not DB_SCHEMA_CURRENT:
                return None

            snap_row = conn.execute(
                text("SELECT * FROM gex_snapshots WHERE symbol = :symbol ORDER BY timestamp DESC LIMIT 1"),
                {"symbol": symbol}
            ).fetchone()
            if not snap_row:
                return None

            query_sweep_rows = text("""
                SELECT strike_price, option_type, delta, gamma, gex_value, open_interest, underlying_price, expiration_date
                FROM raw_option_greeks
                WHERE snapshot_id = :snapshot_id
                ORDER BY strike_price ASC
            """)
            rows = conn.execute(query_sweep_rows, {"snapshot_id": snap_row.id}).fetchall()
            spot = getattr(snap_row, 'spot_price', 0) or 0
            gamma_sweep = build_gamma_sweep([dict(row._mapping) for row in rows], spot, symbol)
            return _nearest_modeled_zero(gamma_sweep, spot)

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
                    "effective_gex": data.get('effective_gex', 0),
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
                overview_data["cockpit_levels"][idx_symbol] = _gamma_levels_for_symbol(idx_symbol, conn, per_side=None)
                overview_data["modeled_zero_gex"][idx_symbol] = _modeled_zero_gex_for_symbol(idx_symbol, conn)

        try:
            from ninjatrader_broadcaster import _dashboard_payload_for_symbol
            from signal_performance import edge_stats_for_dashboard, label_due_outcomes

            label_due_outcomes()
            for idx_symbol in ["NDX", "SPX"]:
                dashboard_payload = _dashboard_payload_for_symbol(idx_symbol, overview_data)
                overview_data["edge_stats"][idx_symbol] = edge_stats_for_dashboard(
                    dashboard_payload,
                    overview_data.get("compass", {}).get("label", "NEUTRAL"),
                )
        except Exception as e:
            overview_data["edge_stats_error"] = str(e)

        return overview_data

    except Exception as e:
        print(f"Error in market overview: {e}")
        return {"error": str(e)}

# --- Run App ---
def main():
    start_collector_process()
    try:
        eel.start('index.html', size=(1500, 900), port=8080)
    except OSError:
        eel.start('index.html', mode='edge', size=(1500, 900), port=8080)
    finally:
        stop_collector_process()


if __name__ == '__main__':
    main()
