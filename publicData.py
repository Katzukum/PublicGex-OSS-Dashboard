import os
import time
import logging
import re
from datetime import datetime, date, timedelta
from typing import List, Optional, Any

from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Date, Index
from sqlalchemy.orm import declarative_base, sessionmaker, Session

# Public.com SDK Imports
from public_api_sdk import (
    PublicApiClient,
    PublicApiClientConfiguration,
    ApiKeyAuthConfig,
    OrderInstrument,
    InstrumentType,
    OptionExpirationsRequest,
    OptionChainRequest
)

# --- Configuration & Setup ---
load_dotenv()
import json
with open('settings.json') as f:
    SETTINGS = json.load(f)
SYMBOLS_TO_TRACK = SETTINGS.get("symbols", ["SPY"])

API_KEY = os.getenv("PUBLIC_API_KEY")
ACCOUNT_ID = os.getenv("PUBLIC_ACCOUNT_ID")
DB_CONNECTION_STR = "sqlite:///gex_data.db"
API_RATE_LIMIT_PER_MINUTE = int(os.getenv("API_RATE_LIMIT", "60"))
STRIKE_RANGE_PCT = 0.03

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("gex_collector.log"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

Base = declarative_base()

class RawOptionGreek(Base):
    """SQLAlchemy model representing a single option contract's Greek data.

    This table stores the granular data for every contract fetched during a run.
    """
    __tablename__ = 'raw_option_greeks'
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    symbol = Column(String, index=True)
    expiration_date = Column(Date)
    osi_symbol = Column(String, index=True)
    strike_price = Column(Float)
    option_type = Column(String)
    delta = Column(Float)
    gamma = Column(Float)
    open_interest = Column(Integer)
    underlying_price = Column(Float)
    gex_value = Column(Float)
    __table_args__ = (Index('idx_symbol_time', 'symbol', 'timestamp'),)

class GexSnapshot(Base):
    """SQLAlchemy model representing a high-level GEX summary for a symbol.

    Stores the aggregated metrics (Net GEX, Max Pain, Flip Point) for a 
    specific timestamp, used for plotting history and determining market regime.
    """
    __tablename__ = 'gex_snapshots'
    id = Column(Integer, primary_key=True)
    timestamp = Column(DateTime, default=datetime.now, index=True)
    symbol = Column(String, index=True)
    spot_price = Column(Float)
    total_net_gex = Column(Float)
    total_call_gex = Column(Float)
    total_put_gex = Column(Float)
    max_call_gex_strike = Column(Float)
    max_put_gex_strike = Column(Float)
    flip_strike = Column(Float)
    regime = Column(String)
    effective_gex = Column(Float)
    total_gamma = Column(Float, default=0.0)
    total_theta = Column(Float, default=0.0)

class RateLimiter:
    """Simple blocking rate limiter to respect API tokens."""
    def __init__(self, requests_per_minute):
        self.delay = 60.0 / requests_per_minute
        self.last_call = 0.0

    def wait(self):
        """Blocks execution until enough time has passed since the last call."""
        now = time.time()
        elapsed = now - self.last_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_call = time.time()

rate_limiter = RateLimiter(API_RATE_LIMIT_PER_MINUTE)
engine = create_engine(DB_CONNECTION_STR)
Base.metadata.create_all(engine)
SessionLocal = sessionmaker(bind=engine)

# --- Helpers ---

def send_event_to_backend(payload, port=5005):
    """Sends a JSON event to the main backend server (appy.py).

    Args:
        payload: Dictionary containing the event data.
        port: TCP port of the backend event server.
    """
    try:
        import socket
        import json
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(('127.0.0.1', port))
            message = json.dumps(payload).encode('utf-8')
            s.sendall(message)
    except Exception as e:
        # Don't crash the collector if the frontend is down
        logger.debug(f"Failed to send event to backend: {e}")

def get_client():
    if not API_KEY:
        raise ValueError("Please set PUBLIC_API_KEY in your .env file")
    config = PublicApiClientConfiguration(default_account_number=ACCOUNT_ID)
    return PublicApiClient(
        auth_config=ApiKeyAuthConfig(api_secret_key=API_KEY),
        config=config
    )

def get_val(obj: Any, keys: List[str], default=None):
    if obj is None: return default
    for key in keys:
        if isinstance(obj, dict):
            if key in obj: return obj[key]
        elif hasattr(obj, key):
            val = getattr(obj, key)
            if val is not None: return val
    return default

def parse_osi_from_symbol(osi_str: str):
    if not osi_str: return 0.0, None
    try:
        match = re.search(r'(\d{6})([CP])(\d{8})$', osi_str)
        if match:
            otype = 'CALL' if match.group(2) == 'C' else 'PUT'
            strike = float(match.group(3)) / 1000.0
            return strike, otype
    except Exception:
        pass
    return 0.0, None

def extract_all_options(response_obj) -> list:
    """Robustly extracts option contract lists from varied API response structures.

    Public.com's API sometimes returns lists directly, or nested in 'calls'/'puts',
    or inside 'instrument' wrappers. This function attempts to unify them.

    Args:
        response_obj: The raw JSON object or list from the API response.

    Returns:
        A flat list of option dictionaries.
    """
    all_options = []
    found_specific = False

    # 1. Check for known separated lists (common in Option Chain APIs)
    for key in ['calls', 'puts', 'options']:
        val = get_val(response_obj, [key])
        if isinstance(val, list):
            all_options.extend(val)
            found_specific = True
            
    if found_specific:
        return all_options

    # 2. If response itself is a list
    if isinstance(response_obj, list):
        return response_obj
        
    # 3. Fallback: Check generic keys if step 1 failed
    candidates = ['items', 'data', 'contracts', 'chain', 'instrument', 'quotes']
    for attr in candidates:
        val = get_val(response_obj, [attr])
        if isinstance(val, list): return val
            
    # 4. Deep Fallback: Inspect object attributes
    if hasattr(response_obj, '__dict__'):
        # If we are here, we might have { calls: [...], puts: [...] } as attributes
        # We want to collect ALL lists, not just the first one
        temp_list = []
        for v in vars(response_obj).values():
            if isinstance(v, list) and len(v) > 0:
                temp_list.extend(v)
        if temp_list:
            return temp_list
            
    return []

def calculate_flip_point(gex_by_strike: dict) -> float:
    """Identifies the strike price where Cumulative Net Gamma flips sign.

    The "Gamma Flip" is often viewed as a support/resistance level or a boundary
    between stable (positive gamma) and volatile (negative gamma) markets.

    Args:
        gex_by_strike: Dictionary mapping {strike_price: net_gex}.

    Returns:
        The first strike price where the cumulative GEX sum changes sign.
        Returns 0.0 if no flip occurs.
    """
    strikes = sorted(gex_by_strike.keys())
    if not strikes:
        return 0.0
    
    running_total = 0.0
    # To handle the "starts positive" or "starts negative" case correctly,
    # we look for a sign change in the running total.
    
    # Initial state
    prev_total = 0.0
    
    for i, s in enumerate(strikes):
        val = gex_by_strike[s]
        running_total += val
        
        # If this is the first item, just set prev and continue
        if i == 0:
            prev_total = running_total
            continue
            
        # Check sign flip
        if (prev_total < 0 and running_total >= 0) or (prev_total > 0 and running_total <= 0):
            return s
            
        prev_total = running_total
        
    return 0.0

def calculate_effective_gex(relevant_options: List[tuple], spot_price: float) -> float:
    """
    Calculates Effective GEX within a +/- 2% window of spot price.
    """
    if not relevant_options or spot_price == 0:
        return 0.0
        
    upper_bound = spot_price * 1.02
    lower_bound = spot_price * 0.98
    
    total_effective = 0.0
    
    # relevant_options is list of (opt, strike, osi)
    # We need gamma from the batch map, but we don't have it easily here
    # UNLESS we move this calcluation after the batch fetch loop.
    # Refactoring process_symbol to handle this.
    return 0.0 # Placeholder, logic moved to main loop to access instantiated Greek data

def get_instrument_type(symbol: str):
    # Common indices
    INDICES = {'SPX', 'NDX', 'RUT', 'VIX', 'DJX'}
    if symbol.upper() in INDICES:
        return InstrumentType.INDEX
    return InstrumentType.EQUITY

def get_target_expiration(symbol: str) -> date:
    """Calculates the target expiration date based on the 0DTE strategy.
    
    - SPX/NDX/SPY/QQQ: Targets Today (0DTE).
    - IWM: Targets the next Friday (Liquidity Rule).
    """
    today = date.today()
    
    # 1. SPY / QQQ / IWM -> Always target Today (0DTE)
    if symbol in ['SPY', 'QQQ', 'IWM']:
        return today
        
    # 2. SPX / NDX -> Target the nearest Friday (to capture liquidity/weekly structure)
    if symbol in ['SPX', 'NDX', 'SPXW', 'NDXP']:
        # Monday=0, Sunday=6
        # If today is Friday (4), we want today.
        # If today is Sat(5), we want next Fri.
        # If today is Mon(0), we want this Fri.
        days_ahead = 4 - today.weekday()
        if days_ahead < 0: # It's Saturday/Sunday, aim for next Friday
            days_ahead += 7
        return today + timedelta(days=days_ahead)
    
    # Default fallback
    return today

def get_0dte_expiration(client, symbol: str) -> Optional[str]:
    rate_limiter.wait()
    target_date = get_target_expiration(symbol)
    logger.info(f"Targeting expiration {target_date} for {symbol}")
    
    try:
        itype = get_instrument_type(symbol)
        req = OptionExpirationsRequest(instrument=OrderInstrument(symbol=symbol, type=itype))
        resp = client.get_option_expirations(req)
        exp_list = extract_all_options(resp) # Use new extractor here too

        for exp in exp_list:
            exp_str = exp if isinstance(exp, str) else get_val(exp, ['expirationDate', 'date', 'expiration_date'])
            if not isinstance(exp_str, str): continue
            try:
                # Compare against our calculated target date
                if datetime.strptime(exp_str, "%Y-%m-%d").date() == target_date:
                    return exp_str
            except ValueError:
                continue
    except Exception as e:
        logger.error(f"Error fetching expirations for {symbol}: {e}")
    return None

def get_option_greeks_batch(client, osi_symbols: List[str]) -> dict:
    """Fetches Greeks (Delta, Gamma) for multiple contracts in a single request.

    Uses the internal Public.com API gateway for efficiency.

    Args:
        client: The authenticated PublicApiClient.
        osi_symbols: A list of OSI-compliant option strings (e.g. 'SPY260127C00500000').

    Returns:
        Dictionary mapping OSI Symbol -> {delta, gamma, theta, ...}.
    """
    results = {}
    if not osi_symbols:
        return results

    chunk_size = 200
    total_len = len(osi_symbols)
    # Access internal session for auth
    try:
        api_client = client.api_client
        session = api_client.session
        base_url = api_client.base_url
    except AttributeError:
        logger.error("Could not access internal client session for batch request.")
        return results

    endpoint = f"/userapigateway/option-details/{ACCOUNT_ID}/greeks"
    url = f"{base_url}{endpoint}"

    for i in range(0, total_len, chunk_size):
        chunk = osi_symbols[i : i + chunk_size]
        rate_limiter.wait()
        
        try:
            resp = session.get(url, params={"osiSymbols": chunk})
            if resp.status_code == 200:
                data = resp.json()
                # data is expected to be { "greeks": [ { "symbol": "...", "greeks": {...} } ] }
                # OR just a list [ { "symbol": "...", "greeks": {...} } ] based on test output?
                # Test output showed: { "greeks": [ ... ] }
                
                items = data.get("greeks", []) if isinstance(data, dict) else data
                
                for item in items:
                    sym = item.get("symbol")
                    greeks = item.get("greeks")
                    if sym and greeks:
                        results[sym] = greeks
            else:
                logger.error(f"Batch Greeks request failed: {resp.status_code} {resp.text}")
        except Exception as e:
            logger.error(f"Error during batch Greeks fetch: {e}")

    return results

def process_symbol(client, session: Session, symbol: str):
    """Main data collection pipeline for a single symbol.

    1. Fetches current Spot Price.
    2. Identifies the relevant Expiration Date (0DTE).
    3. Downloads the Option Chain.
    4. Filters contracts near the money.
    5. Batches fetches Greeks (Gamma/Delta).
    6. Calculates GEX, Net GEX, Flip Point, and Magnets.
    7. Saves to SQL Database and broadcasts updates.

    Args:
        client: The authenticated API client.
        session: Active SQLAlchemy DB session.
        symbol: The ticker symbol (e.g., "SPY").
    """
    logger.info(f"Starting collection for {symbol}...")
    
    # 1. Spot Price
    rate_limiter.wait()
    try:
        itype = get_instrument_type(symbol)
        quotes = client.get_quotes([OrderInstrument(symbol=symbol, type=itype)])
        q_obj = quotes[0] if quotes else {}
        spot_price = float(get_val(q_obj, ['last', 'lastPrice', 'price'], 0))
        logger.info(f"Spot Price for {symbol}: {spot_price}")
    except Exception as e:
        logger.error(f"Failed to get spot price for {symbol}: {e}")
        return

    if spot_price == 0:
        logger.error(f"Spot price is 0 for {symbol}, aborting.")
        return

    # 2. Expiration
    expiration_str = get_0dte_expiration(client, symbol)
    if not expiration_str:
        logger.info(f"No 0DTE expiration found for {symbol}. Skipping.")
        return
    expiration_date = datetime.strptime(expiration_str, "%Y-%m-%d").date()

    # 3. Get Chain
    rate_limiter.wait()
    try:
        req = OptionChainRequest(
            instrument=OrderInstrument(symbol=symbol, type=itype),
            expiration_date=expiration_str
        )
        chain_resp = client.get_option_chain(req)
        options_list = extract_all_options(chain_resp)
    except Exception as e:
        logger.error(f"Failed to fetch chain for {symbol}: {e}")
        return

    # 4. Filter
    relevant_options = []
    upper_bound = spot_price * (1 + STRIKE_RANGE_PCT)
    lower_bound = spot_price * (1 - STRIKE_RANGE_PCT)

    logger.info(f"Filtering {symbol}: Spot {spot_price} | Range {lower_bound:.2f} - {upper_bound:.2f}")

    for i, opt in enumerate(options_list):
        instrument = get_val(opt, ['instrument'])
        strike = float(get_val(instrument, ['strike_price', 'strikePrice', 'strike'], 0))
        osi = get_val(instrument, ['symbol', 'ticker', 'osi_symbol']) or get_val(opt, ['symbol', 'ticker'])
        
        if strike == 0:
            strike, _ = parse_osi_from_symbol(osi)

        if lower_bound <= strike <= upper_bound:
            relevant_options.append((opt, strike, osi))

    logger.info(f"Found {len(options_list)} total contracts. Filtered to {len(relevant_options)} near-the-money contracts.")

    if not relevant_options:
        logger.warning(f"No valid data processed for {symbol}.")
        return

    # 5. Fetch Greeks (BATCH)
    all_osi = [osi for (_, _, osi) in relevant_options if osi]
    logger.info(f"Fetching Greeks for {len(all_osi)} contracts using batch API...")
    
    greeks_map = get_option_greeks_batch(client, all_osi)
    logger.info(f"Batch fetch complete. Received data for {len(greeks_map)} contracts.")

    # 6. Process
    total_net_gex = 0.0
    total_call_gex = 0.0
    total_put_gex = 0.0
    
    # New Theta/Gamma accumulation
    total_gamma_sum = 0.0
    total_theta_sum = 0.0
    
    effective_gex = 0.0 # +/- 2% GEX
    gex_by_strike = {} # For Flip Point Calc
    batch_data = []
    
    # Pre-calc effective bounds
    eff_upper = spot_price * 1.02
    eff_lower = spot_price * 0.98
    timestamp = datetime.now()
    
    for i, (opt, strike, osi) in enumerate(relevant_options):
        try:
            instrument = get_val(opt, ['instrument'])
            oi = int(get_val(opt, ['open_interest', 'openInterest'], 0) or 0)
            if oi == 0: continue

            # Resolve Type
            otype_raw = get_val(instrument, ['option_type', 'optionType'], '')
            
            # Force OSI check if missing or generic
            if not otype_raw or str(otype_raw).upper() == 'OPTION':
                _, parsed_type = parse_osi_from_symbol(osi)
                otype = parsed_type
            else:
                otype = str(otype_raw).upper()

            if not otype: otype = 'UNKNOWN'

            # Get Greeks from Map
            greek_data = greeks_map.get(osi, {})
            # API returns strings usually, cast to float
            gamma = float(greek_data.get('gamma') or 0)
            delta = float(greek_data.get('delta') or 0)
            theta = float(greek_data.get('theta') or 0)

            # Accumulate raw Gamma (absolute magnitude usually matters for regime, but here we sum raw)
            # Actually, standard practice for "Total Gamma" exposure is sum of Gamma * OI * Spot possibly?
            # Or just sum of raw Gamma? Implemenntation Plan said "Sum gamma (absolute sum)".
            # Let's sum raw gamma for now as per common GEX dashboards, or usually it's Net Gamma.
            # Plan said "Sum gamma (absolute sum) and theta".
            # Checking "Respect the Clock": "if total_gamma is massive..."
            # Usually we want the RAW sum of gamma to see total liquidity providing.
            total_gamma_sum += gamma * oi * 100 # Weighted by OI
            total_theta_sum += theta * oi * 100

            # Calc GEX
            raw_gex = gamma * oi * spot_price * 100
            
            # Log one sample of each type for debugging
            if i < 10 and ('SAMPLE_CALL' not in locals() and otype == 'CALL'):
                logger.info(f"SAMPLE CALL: {osi} | Gamma: {gamma} | GEX: {raw_gex}")
                locals()['SAMPLE_CALL'] = True
            if i < 50 and ('SAMPLE_PUT' not in locals() and otype == 'PUT'):
                logger.info(f"SAMPLE PUT: {osi} | Gamma: {gamma} | GEX: -{raw_gex}")
                locals()['SAMPLE_PUT'] = True

            if 'PUT' in otype:
                raw_gex = raw_gex * -1
                total_put_gex += raw_gex
            else:
                total_call_gex += raw_gex

            total_net_gex += raw_gex
            
            # Effective GEX Summation
            if eff_lower <= strike <= eff_upper:
                effective_gex += raw_gex
            
            # Aggregate for Flip Point
            if strike not in gex_by_strike:
                gex_by_strike[strike] = 0.0
            gex_by_strike[strike] += raw_gex

            batch_data.append(RawOptionGreek(
                timestamp=timestamp,
                symbol=symbol,
                expiration_date=expiration_date,
                osi_symbol=osi,
                strike_price=strike,
                option_type=otype,
                delta=delta,
                gamma=gamma,
                open_interest=oi,
                underlying_price=spot_price,
                gex_value=raw_gex
            ))
            
        except Exception as e:
            logger.error(f"Error processing {osi}: {e}")
            continue

    # 7. Save
    if batch_data:
        session.bulk_save_objects(batch_data)
        
        call_rows = [r for r in batch_data if 'CALL' in r.option_type]
        put_rows = [r for r in batch_data if 'PUT' in r.option_type]
        
        max_call_gex_strike = max(call_rows, key=lambda x: x.gex_value).strike_price if call_rows else 0
        max_put_gex_strike = min(put_rows, key=lambda x: x.gex_value).strike_price if put_rows else 0

        # --- Magnet Calculation (Current) ---
        # Magnet = Strike with Max Abs Net GEX
        # utilizing the already aggregated gex_by_strike from the loop
        if gex_by_strike:
            magnet_strike = max(gex_by_strike, key=lambda s: abs(gex_by_strike[s]))
            magnet_strength = gex_by_strike[magnet_strike]
        else:
            magnet_strike = 0
            magnet_strength = 0
        
        # --- Check Previous Magnet (For Event) ---
        # Get the previous snapshot to find its timestamp
        prev_snap = session.query(GexSnapshot).filter(
            GexSnapshot.symbol == symbol
        ).order_by(GexSnapshot.timestamp.desc()).first()
        
        prev_magnet_strike = 0
        if prev_snap:
            # Query the raw data for that timestamp to find its magnet
            # SQL: SELECT strike_price, SUM(gex_value) FROM raw... GROUP BY strike ...
            from sqlalchemy import func
            prev_magnet_row = session.query(
                RawOptionGreek.strike_price,
                func.sum(RawOptionGreek.gex_value).label('net_gex')
            ).filter(
                RawOptionGreek.symbol == symbol,
                RawOptionGreek.timestamp == prev_snap.timestamp
            ).group_by(RawOptionGreek.strike_price).order_by(
                func.abs(func.sum(RawOptionGreek.gex_value)).desc()
            ).first()
            
            if prev_magnet_row:
                prev_magnet_strike = prev_magnet_row.strike_price
        
        # Commit the new snapshot
        session.add(GexSnapshot(
            timestamp=timestamp,
            symbol=symbol,
            spot_price=spot_price,
            total_net_gex=total_net_gex,
            total_call_gex=total_call_gex,
            total_put_gex=total_put_gex,
            max_call_gex_strike=max_call_gex_strike,
            max_put_gex_strike=max_put_gex_strike,
            flip_strike=calculate_flip_point(gex_by_strike),
            regime="Sentiment",
            effective_gex=effective_gex,
            total_gamma=total_gamma_sum,
            total_theta=total_theta_sum
        ))
        session.commit()
        logger.info(f"Saved {len(batch_data)} records for {symbol}. Net GEX: ${total_net_gex:,.2f}")
        
        # --- Emit Events ---
        from event_utils import send_event
        
        # 1. General Data Refresh
        send_event("data_refresh", {"symbol": symbol, "timestamp": str(timestamp)})
        
        # 2. Magnet Change Event
        if prev_snap and prev_magnet_strike != 0 and magnet_strike != prev_magnet_strike:
            logger.info(f"** MAGNET CHANGE ** {prev_magnet_strike} -> {magnet_strike}")
            send_event("magnet_change", {
                "symbol": symbol,
                "old_magnet": prev_magnet_strike,
                "new_magnet": magnet_strike,
                "strength": magnet_strength,
                "timestamp": str(timestamp)
            })

def main():
    """Entry point for the GEX Data Collector.

    Iterates through all tracked symbols (from settings.json), runs the
    collection pipeline, aggregates market sentiment, and broadcasts the
    unified regime to NinjaTrader.
    """
    session = SessionLocal()
    try:
        client = get_client()
        for symbol in SYMBOLS_TO_TRACK:
            process_symbol(client, session, symbol)
        
        # --- Broadcast regime to NinjaTrader after all symbols processed ---
        try:
            # from ninjatrader_broadcaster import send_regime_update # Moved to appy.py
            
            # Build minimal overview data for broadcast
            weights = SETTINGS.get('weights', {'SPY': 1.0})
            overview_data = {
                "compass": {"x_score": 0, "y_score": 0, "label": "NEUTRAL", "strategy": ""},
                "components": []
            }
            
            weighted_vol_score = 0
            weighted_trend_score = 0
            total_weight = 0
            
            for symbol, weight in weights.items():
                snap = session.query(GexSnapshot).filter(
                    GexSnapshot.symbol == symbol
                ).order_by(GexSnapshot.timestamp.desc()).first()
                
                if snap:
                    net_gex = snap.total_net_gex
                    spot = snap.spot_price
                    flip = snap.flip_strike or 0
                    
                    vol_sign = 1 if net_gex > 0 else -1
                    trend_sign = (1 if spot > flip else -1) if flip > 0 else vol_sign
                    
                    weighted_vol_score += vol_sign * weight
                    weighted_trend_score += trend_sign * weight
                    total_weight += weight
                    
                    overview_data["components"].append({
                        "symbol": symbol, "spot": spot, "flip_strike": flip, "net_gex": net_gex
                    })
            
            if total_weight > 0:
                final_vol = weighted_vol_score / total_weight
                final_trend = weighted_trend_score / total_weight
                
                is_pos_gex = final_vol > 0
                is_bull_trend = final_trend > 0
                
                if is_pos_gex and is_bull_trend:
                    label = "GRIND UP"
                    code = 1
                elif is_pos_gex and not is_bull_trend:
                    label = "SUPPORT / CHOP"
                    code = 3
                elif not is_pos_gex and is_bull_trend:
                    label = "MELT UP"
                    code = 2
                else:
                    label = "CRASH / FLUSH"
                    code = 4
                
                overview_data["compass"]["x_score"] = final_vol
                overview_data["compass"]["y_score"] = final_trend
                overview_data["compass"]["label"] = label
            
            # Add NDX and SPX spot prices (for NinjaTrader futures charts)
            overview_data["gamma_levels"] = {"NDX": [], "SPX": []}
            
            for idx_symbol in ["NDX", "SPX"]:
                idx_snap = session.query(GexSnapshot).filter(
                    GexSnapshot.symbol == idx_symbol
                ).order_by(GexSnapshot.timestamp.desc()).first()
                
                if idx_snap:
                    overview_data["components"].append({
                        "symbol": idx_symbol,
                        "spot": idx_snap.spot_price,
                        "flip_strike": idx_snap.flip_strike or 0,
                        "net_gex": idx_snap.total_net_gex
                    })
                    print(f"[NinjaTrader] Added {idx_symbol} spot: {idx_snap.spot_price}")
                    
                    # Query 10 levels below spot (nearest to price)
                    from sqlalchemy import func
                    levels_below = session.query(
                        RawOptionGreek.strike_price,
                        func.sum(RawOptionGreek.gex_value).label('net_gex')
                    ).filter(
                        RawOptionGreek.symbol == idx_symbol,
                        RawOptionGreek.timestamp == idx_snap.timestamp,
                        RawOptionGreek.strike_price < idx_snap.spot_price
                    ).group_by(
                        RawOptionGreek.strike_price
                    ).order_by(
                        RawOptionGreek.strike_price.desc()
                    ).limit(10).all()

                    # Query 10 levels above spot (nearest to price)
                    levels_above = session.query(
                        RawOptionGreek.strike_price,
                        func.sum(RawOptionGreek.gex_value).label('net_gex')
                    ).filter(
                        RawOptionGreek.symbol == idx_symbol,
                        RawOptionGreek.timestamp == idx_snap.timestamp,
                        RawOptionGreek.strike_price >= idx_snap.spot_price
                    ).group_by(
                        RawOptionGreek.strike_price
                    ).order_by(
                        RawOptionGreek.strike_price.asc()
                    ).limit(10).all()
                    
                    # Combine results
                    all_levels = levels_below + levels_above
                    
                    for level in all_levels:
                        overview_data["gamma_levels"][idx_symbol].append({
                            "strike": level.strike_price,
                            "gex": level.net_gex,
                            "type": "resistance" if level.net_gex > 0 else "support"
                        })
                    print(f"[NinjaTrader] Added {len(all_levels)} gamma levels for {idx_symbol} (Profile View)")
                
            # Send to Backend via Event Server (Port 5005)
            # appy.py will receive this and forward it to NinjaTrader (Port 5010)
            event_payload = {
                "type": "MARKET_UPDATE",
                "data": overview_data
            }
            send_event_to_backend(event_payload)
            print("[Event] Sent market update to backend.")
            
        except Exception as e:
            logger.warning(f"Event broadcast failed: {e}")
            
    except Exception as e:
        logger.critical(f"Global Error: {e}")
    finally:
        session.close()
        logger.info("Run complete.")

if __name__ == "__main__":
    main()
