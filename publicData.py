import argparse
import json
import logging
import os
import re
import socket
import sys
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from sqlalchemy import delete, func
from sqlalchemy.orm import Session

from models import (
    CollectionRun,
    GexSnapshot,
    RawOptionGreek,
    compact_database,
    get_session_factory,
    initialize_database,
    reset_database,
)

DEFAULT_SETTINGS = {
    "refresh_interval": 180,
    "theme": "dark",
    "symbols": ["SPY"],
    "backend_update_delay": 180,
    "raw_retention_days": 30,
    "weights": {"SPY": 1.0},
    "weights_whale": {"SPX": 0.45, "NDX": 0.35, "IWM": 0.20},
}

STRIKE_RANGE_PCT = 0.03
LOCK_PATH = Path("gex_collector.lock")
COMPACT_MARKER_PATH = Path("gex_compact.marker")
LOCK_STALE_SECONDS = 30 * 60
COMPACT_INTERVAL_SECONDS = 24 * 60 * 60

ApiKeyAuthConfig = None
InstrumentType = None
OptionChainRequest = None
OptionExpirationsRequest = None
OrderInstrument = None
PublicApiClient = None
PublicApiClientConfiguration = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler("gex_collector.log"), logging.StreamHandler()],
)
logger = logging.getLogger(__name__)


class ConfigError(ValueError):
    pass


class RateLimiter:
    """Simple blocking rate limiter to respect API tokens."""

    def __init__(self, requests_per_minute: int):
        self.delay = 60.0 / max(1, requests_per_minute)
        self.last_call = 0.0

    def wait(self):
        now = time.time()
        elapsed = now - self.last_call
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self.last_call = time.time()


def load_public_sdk():
    global ApiKeyAuthConfig
    global InstrumentType
    global OptionChainRequest
    global OptionExpirationsRequest
    global OrderInstrument
    global PublicApiClient
    global PublicApiClientConfiguration

    if PublicApiClient is not None:
        return

    try:
        from public_api_sdk import (
            ApiKeyAuthConfig as _ApiKeyAuthConfig,
            InstrumentType as _InstrumentType,
            OptionChainRequest as _OptionChainRequest,
            OptionExpirationsRequest as _OptionExpirationsRequest,
            OrderInstrument as _OrderInstrument,
            PublicApiClient as _PublicApiClient,
            PublicApiClientConfiguration as _PublicApiClientConfiguration,
        )
    except ImportError as e:
        raise ConfigError("Public SDK is missing. Run: pip install -r requirements.txt") from e

    ApiKeyAuthConfig = _ApiKeyAuthConfig
    InstrumentType = _InstrumentType
    OptionChainRequest = _OptionChainRequest
    OptionExpirationsRequest = _OptionExpirationsRequest
    OrderInstrument = _OrderInstrument
    PublicApiClient = _PublicApiClient
    PublicApiClientConfiguration = _PublicApiClientConfiguration


@contextmanager
def collector_lock():
    now = time.time()
    if LOCK_PATH.exists() and now - LOCK_PATH.stat().st_mtime > LOCK_STALE_SECONDS:
        logger.warning("Removing stale collector lock: %s", LOCK_PATH)
        LOCK_PATH.unlink(missing_ok=True)

    fd = None
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("utf-8"))
        yield True
    except FileExistsError:
        yield False
    finally:
        if fd is not None:
            os.close(fd)
            LOCK_PATH.unlink(missing_ok=True)


def load_settings(path: str = "settings.json") -> dict:
    settings = DEFAULT_SETTINGS.copy()
    try:
        with open(path, "r", encoding="utf-8") as f:
            user_settings = json.load(f)
        settings.update(user_settings)
    except FileNotFoundError:
        logger.warning("settings.json not found; using defaults.")
    except json.JSONDecodeError as e:
        raise ConfigError(f"settings.json is invalid JSON: {e}") from e

    if not isinstance(settings.get("symbols"), list) or not settings["symbols"]:
        raise ConfigError("settings.symbols must be a non-empty list")

    try:
        settings["backend_update_delay"] = max(10, int(settings.get("backend_update_delay", 180)))
        settings["raw_retention_days"] = max(1, int(settings.get("raw_retention_days", 30)))
    except (TypeError, ValueError) as e:
        raise ConfigError("backend_update_delay and raw_retention_days must be integers") from e

    return settings


def load_runtime_config() -> dict:
    load_dotenv()
    settings = load_settings()

    try:
        api_rate_limit = int(os.getenv("API_RATE_LIMIT", "60"))
    except ValueError as e:
        raise ConfigError("API_RATE_LIMIT must be an integer") from e

    config = {
        "settings": settings,
        "symbols": [str(s).upper() for s in settings["symbols"]],
        "api_key": (os.getenv("PUBLIC_API_KEY") or "").strip(),
        "account_id": (os.getenv("PUBLIC_ACCOUNT_ID") or "").strip(),
        "api_rate_limit": max(1, api_rate_limit),
    }

    if not config["api_key"]:
        raise ConfigError("PUBLIC_API_KEY is missing. Add it to .env.")
    if not config["account_id"]:
        raise ConfigError("PUBLIC_ACCOUNT_ID is missing. Add it to .env.")

    return config


def json_list(values: list[str]) -> str:
    return json.dumps(values)


def send_event_to_backend(payload, port=5005):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5)
            s.connect(("127.0.0.1", port))
            s.sendall(json.dumps(payload).encode("utf-8"))
    except Exception as e:
        logger.debug("Failed to send event to backend: %s", e)


def get_client(config: dict):
    load_public_sdk()
    client_config = PublicApiClientConfiguration(default_account_number=config["account_id"])
    return PublicApiClient(
        auth_config=ApiKeyAuthConfig(api_secret_key=config["api_key"]),
        config=client_config,
    )


def get_val(obj: Any, keys: list[str], default=None):
    if obj is None:
        return default
    for key in keys:
        if isinstance(obj, dict):
            if key in obj:
                return obj[key]
        elif hasattr(obj, key):
            val = getattr(obj, key)
            if val is not None:
                return val
    return default


def parse_osi_from_symbol(osi_str: str):
    if not osi_str:
        return 0.0, None
    try:
        match = re.search(r"(\d{6})([CP])(\d{8})$", osi_str)
        if match:
            otype = "CALL" if match.group(2) == "C" else "PUT"
            strike = float(match.group(3)) / 1000.0
            return strike, otype
    except Exception:
        pass
    return 0.0, None


def extract_all_options(response_obj) -> list:
    all_options = []
    found_specific = False

    for key in ["calls", "puts", "options"]:
        val = get_val(response_obj, [key])
        if isinstance(val, list):
            all_options.extend(val)
            found_specific = True

    if found_specific:
        return all_options

    if isinstance(response_obj, list):
        return response_obj

    for attr in ["items", "data", "contracts", "chain", "instrument", "quotes"]:
        val = get_val(response_obj, [attr])
        if isinstance(val, list):
            return val

    if hasattr(response_obj, "__dict__"):
        temp_list = []
        for v in vars(response_obj).values():
            if isinstance(v, list) and v:
                temp_list.extend(v)
        if temp_list:
            return temp_list

    return []


def calculate_flip_point(gex_by_strike: dict) -> float:
    strikes = sorted(gex_by_strike.keys())
    if not strikes:
        return 0.0

    running_total = 0.0
    prev_total = 0.0
    prev_strike = strikes[0]

    for i, strike in enumerate(strikes):
        running_total += gex_by_strike[strike]
        if i == 0:
            prev_total = running_total
            prev_strike = strike
            continue
        if (prev_total < 0 <= running_total) or (prev_total > 0 >= running_total):
            span = running_total - prev_total
            if span == 0:
                return strike
            ratio = abs(prev_total) / abs(span)
            return prev_strike + ((strike - prev_strike) * ratio)
        prev_total = running_total
        prev_strike = strike

    return 0.0


def calculate_gex_slope(spot, profile_data):
    if not profile_data or spot == 0:
        return 0

    strikes_gex = {}
    for row in profile_data:
        s = getattr(row, "strike_price", None) if hasattr(row, "strike_price") else row.get("strike_price")
        g = getattr(row, "gex_value", 0) if hasattr(row, "gex_value") else row.get("gex_value", 0)
        if s is not None:
            strikes_gex[s] = strikes_gex.get(s, 0) + g

    sorted_strikes = sorted(strikes_gex.keys())
    if len(sorted_strikes) < 2:
        return 0

    import bisect

    idx = bisect.bisect_left(sorted_strikes, spot)
    if idx == 0:
        s1, s2 = sorted_strikes[0], sorted_strikes[1]
    elif idx >= len(sorted_strikes):
        s1, s2 = sorted_strikes[-2], sorted_strikes[-1]
    else:
        s1, s2 = sorted_strikes[idx - 1], sorted_strikes[idx]

    g1, g2 = strikes_gex[s1], strikes_gex[s2]
    return (g2 - g1) / (s2 - s1) if s2 != s1 else 0


def get_instrument_type(symbol: str):
    load_public_sdk()
    indices = {"SPX", "NDX", "RUT", "VIX", "DJX"}
    if symbol.upper() in indices:
        return InstrumentType.INDEX
    return InstrumentType.EQUITY


def get_target_expiration(symbol: str, now: Optional[datetime] = None) -> date:
    """Strict 0DTE target with an evening rollover.

    After 6 PM local time, the collector targets the next weekday because the
    market session has effectively moved on while the calendar date has not.
    """

    current = now or datetime.now()
    target = current.date()
    if current.hour >= 18:
        target += timedelta(days=1)

    while target.weekday() >= 5:
        target += timedelta(days=1)

    return target


def get_0dte_expiration(client, symbol: str, rate_limiter: RateLimiter) -> Optional[str]:
    rate_limiter.wait()
    target_date = get_target_expiration(symbol)
    logger.info("Targeting strict 0DTE expiration %s for %s", target_date, symbol)

    try:
        itype = get_instrument_type(symbol)
        req = OptionExpirationsRequest(instrument=OrderInstrument(symbol=symbol, type=itype))
        exp_list = extract_all_options(client.get_option_expirations(req))

        for exp in exp_list:
            exp_str = exp if isinstance(exp, str) else get_val(exp, ["expirationDate", "date", "expiration_date"])
            if not isinstance(exp_str, str):
                continue
            try:
                if datetime.strptime(exp_str, "%Y-%m-%d").date() == target_date:
                    return exp_str
            except ValueError:
                continue
    except Exception as e:
        logger.error("Error fetching expirations for %s: %s", symbol, e)
        raise

    return None


def get_option_greeks_batch(client, osi_symbols: list[str], account_id: str, rate_limiter: RateLimiter) -> dict:
    results = {}
    if not osi_symbols:
        return results

    try:
        api_client = client.api_client
        session = api_client.session
        base_url = api_client.base_url
    except AttributeError:
        logger.error("Could not access internal client session for batch request.")
        return results

    endpoint = f"/userapigateway/option-details/{account_id}/greeks"
    url = f"{base_url}{endpoint}"

    for i in range(0, len(osi_symbols), 200):
        chunk = osi_symbols[i : i + 200]
        rate_limiter.wait()

        try:
            resp = session.get(url, params={"osiSymbols": chunk})
            if resp.status_code == 200:
                data = resp.json()
                items = data.get("greeks", []) if isinstance(data, dict) else data
                for item in items:
                    sym = item.get("symbol")
                    greeks = item.get("greeks")
                    if sym and greeks:
                        results[sym] = greeks
            else:
                logger.error("Batch Greeks request failed: %s %s", resp.status_code, resp.text)
        except Exception as e:
            logger.error("Error during batch Greeks fetch: %s", e)

    return results


def process_symbol(client, session: Session, run: CollectionRun, symbol: str, config: dict, rate_limiter: RateLimiter):
    logger.info("Starting collection for %s...", symbol)
    timestamp = datetime.now()

    try:
        itype = get_instrument_type(symbol)

        rate_limiter.wait()
        quotes = client.get_quotes([OrderInstrument(symbol=symbol, type=itype)])
        q_obj = quotes[0] if quotes else {}
        spot_price = float(get_val(q_obj, ["last", "lastPrice", "price"], 0))
        logger.info("Spot Price for %s: %s", symbol, spot_price)

        if spot_price == 0:
            return {"symbol": symbol, "status": "failed", "message": "Spot price is 0"}

        expiration_str = get_0dte_expiration(client, symbol, rate_limiter)
        if not expiration_str:
            msg = f"No target-day 0DTE expiration found for {symbol}; skipped."
            logger.info(msg)
            return {"symbol": symbol, "status": "skipped", "message": msg}

        expiration_date = datetime.strptime(expiration_str, "%Y-%m-%d").date()

        rate_limiter.wait()
        req = OptionChainRequest(
            instrument=OrderInstrument(symbol=symbol, type=itype),
            expiration_date=expiration_str,
        )
        options_list = extract_all_options(client.get_option_chain(req))

        relevant_options = []
        upper_bound = spot_price * (1 + STRIKE_RANGE_PCT)
        lower_bound = spot_price * (1 - STRIKE_RANGE_PCT)
        logger.info("Filtering %s: Spot %.2f | Range %.2f - %.2f", symbol, spot_price, lower_bound, upper_bound)

        for opt in options_list:
            instrument = get_val(opt, ["instrument"])
            strike = float(get_val(instrument, ["strike_price", "strikePrice", "strike"], 0))
            osi = get_val(instrument, ["symbol", "ticker", "osi_symbol"]) or get_val(opt, ["symbol", "ticker"])
            if strike == 0:
                strike, _ = parse_osi_from_symbol(osi)
            if lower_bound <= strike <= upper_bound:
                relevant_options.append((opt, strike, osi))

        if not relevant_options:
            return {"symbol": symbol, "status": "failed", "message": "No valid near-the-money contracts"}

        all_osi = [osi for (_, _, osi) in relevant_options if osi]
        logger.info("Fetching Greeks for %s contracts using batch API...", len(all_osi))
        greeks_map = get_option_greeks_batch(client, all_osi, config["account_id"], rate_limiter)

        total_net_gex = 0.0
        total_call_gex = 0.0
        total_put_gex = 0.0
        total_gamma_sum = 0.0
        total_theta_sum = 0.0
        effective_gex = 0.0
        gex_by_strike = {}
        raw_rows = []
        eff_upper = spot_price * 1.02
        eff_lower = spot_price * 0.98

        for opt, strike, osi in relevant_options:
            try:
                instrument = get_val(opt, ["instrument"])
                oi = int(get_val(opt, ["open_interest", "openInterest"], 0) or 0)
                if oi == 0:
                    continue

                otype_raw = get_val(instrument, ["option_type", "optionType"], "")
                if not otype_raw or str(otype_raw).upper() == "OPTION":
                    _, parsed_type = parse_osi_from_symbol(osi)
                    otype = parsed_type
                else:
                    otype = str(otype_raw).upper()
                if not otype:
                    otype = "UNKNOWN"

                greek_data = greeks_map.get(osi, {})
                gamma = float(greek_data.get("gamma") or 0)
                delta = float(greek_data.get("delta") or 0)
                theta = float(greek_data.get("theta") or 0)

                total_gamma_sum += gamma * oi * 100
                total_theta_sum += theta * oi * 100

                # Dollar gamma exposure for a 1% underlying move.
                raw_gex = gamma * oi * 100 * spot_price * spot_price * 0.01
                if "PUT" in otype:
                    raw_gex *= -1
                    total_put_gex += raw_gex
                else:
                    total_call_gex += raw_gex

                total_net_gex += raw_gex
                if eff_lower <= strike <= eff_upper:
                    effective_gex += raw_gex
                gex_by_strike[strike] = gex_by_strike.get(strike, 0.0) + raw_gex

                raw_rows.append(
                    {
                        "timestamp": timestamp,
                        "symbol": symbol,
                        "expiration_date": expiration_date,
                        "osi_symbol": osi,
                        "strike_price": strike,
                        "option_type": otype,
                        "delta": delta,
                        "gamma": gamma,
                        "open_interest": oi,
                        "underlying_price": spot_price,
                        "gex_value": raw_gex,
                    }
                )
            except Exception as e:
                logger.error("Error processing %s: %s", osi, e)

        if not raw_rows:
            return {"symbol": symbol, "status": "failed", "message": "No contracts with open interest and Greeks"}

        prev_snap = (
            session.query(GexSnapshot)
            .filter(GexSnapshot.symbol == symbol)
            .order_by(GexSnapshot.timestamp.desc())
            .first()
        )

        prev_magnet_strike = 0
        if prev_snap:
            prev_magnet_row = (
                session.query(RawOptionGreek.strike_price, func.sum(RawOptionGreek.gex_value).label("net_gex"))
                .filter(RawOptionGreek.snapshot_id == prev_snap.id)
                .group_by(RawOptionGreek.strike_price)
                .order_by(func.abs(func.sum(RawOptionGreek.gex_value)).desc())
                .first()
            )
            if prev_magnet_row:
                prev_magnet_strike = prev_magnet_row.strike_price

        call_rows = [r for r in raw_rows if "CALL" in r["option_type"]]
        put_rows = [r for r in raw_rows if "PUT" in r["option_type"]]
        max_call_gex_strike = max(call_rows, key=lambda x: x["gex_value"])["strike_price"] if call_rows else 0
        max_put_gex_strike = min(put_rows, key=lambda x: x["gex_value"])["strike_price"] if put_rows else 0

        magnet_strike = max(gex_by_strike, key=lambda s: abs(gex_by_strike[s])) if gex_by_strike else 0
        magnet_strength = gex_by_strike.get(magnet_strike, 0)

        snapshot = GexSnapshot(
            collection_run_id=run.id,
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
            total_theta=total_theta_sum,
        )
        session.add(snapshot)
        session.flush()

        session.bulk_save_objects([RawOptionGreek(snapshot_id=snapshot.id, **row) for row in raw_rows])
        session.commit()

        logger.info("Saved %s records for %s. Net GEX: $%,.2f", len(raw_rows), symbol, total_net_gex)

        from event_utils import send_event

        send_event("data_refresh", {"symbol": symbol, "timestamp": str(timestamp), "snapshot_id": snapshot.id})
        if prev_snap and prev_magnet_strike != 0 and magnet_strike != prev_magnet_strike:
            send_event(
                "magnet_change",
                {
                    "symbol": symbol,
                    "old_magnet": prev_magnet_strike,
                    "new_magnet": magnet_strike,
                    "strength": magnet_strength,
                    "timestamp": str(timestamp),
                },
            )

        return {
            "symbol": symbol,
            "status": "saved",
            "message": f"Saved {len(raw_rows)} contracts",
            "snapshot_id": snapshot.id,
        }

    except Exception as e:
        logger.exception("Failed to process %s", symbol)
        session.rollback()
        return {"symbol": symbol, "status": "failed", "message": str(e)}


def latest_snapshot(session: Session, symbol: str):
    return (
        session.query(GexSnapshot)
        .filter(GexSnapshot.symbol == symbol)
        .order_by(GexSnapshot.timestamp.desc())
        .first()
    )


def build_overview_data(session: Session, settings: dict) -> dict:
    weights = settings.get("weights", {"SPY": 1.0})
    overview_data = {
        "compass": {"x_score": 0, "y_score": 0, "label": "NEUTRAL", "strategy": ""},
        "components": [],
        "gamma_levels": {"NDX": [], "SPX": []},
    }

    weighted_vol_score = 0
    weighted_trend_score = 0
    total_weight = 0

    for symbol, weight in weights.items():
        snap = latest_snapshot(session, symbol)
        if not snap:
            continue

        net_gex = snap.total_net_gex
        spot = snap.spot_price
        flip = snap.flip_strike or 0
        vol_sign = 1 if net_gex > 0 else -1
        trend_sign = (1 if spot > flip else -1) if flip > 0 else vol_sign

        weighted_vol_score += vol_sign * weight
        weighted_trend_score += trend_sign * weight
        total_weight += weight
        overview_data["components"].append(
            {"symbol": symbol, "spot": spot, "flip_strike": flip, "net_gex": net_gex}
        )

    if total_weight > 0:
        final_vol = weighted_vol_score / total_weight
        final_trend = weighted_trend_score / total_weight
        is_pos_gex = final_vol > 0
        is_bull_trend = final_trend > 0
        if is_pos_gex and is_bull_trend:
            label = "GRIND UP"
        elif is_pos_gex:
            label = "SUPPORT / CHOP"
        elif is_bull_trend:
            label = "MELT UP"
        else:
            label = "CRASH / FLUSH"
        overview_data["compass"].update({"x_score": final_vol, "y_score": final_trend, "label": label})

    for idx_symbol in ["NDX", "SPX"]:
        idx_snap = latest_snapshot(session, idx_symbol)
        if not idx_snap:
            continue

        raw_rows = session.query(RawOptionGreek).filter(RawOptionGreek.snapshot_id == idx_snap.id).all()
        overview_data["components"].append(
            {
                "symbol": idx_symbol,
                "spot": idx_snap.spot_price,
                "flip_strike": idx_snap.flip_strike or 0,
                "net_gex": idx_snap.total_net_gex,
                "acceleration": calculate_gex_slope(idx_snap.spot_price, raw_rows),
            }
        )

        levels_below = (
            session.query(RawOptionGreek.strike_price, func.sum(RawOptionGreek.gex_value).label("net_gex"))
            .filter(RawOptionGreek.snapshot_id == idx_snap.id, RawOptionGreek.strike_price < idx_snap.spot_price)
            .group_by(RawOptionGreek.strike_price)
            .order_by(func.abs(func.sum(RawOptionGreek.gex_value)).desc())
            .limit(5)
            .all()
        )
        levels_above = (
            session.query(RawOptionGreek.strike_price, func.sum(RawOptionGreek.gex_value).label("net_gex"))
            .filter(RawOptionGreek.snapshot_id == idx_snap.id, RawOptionGreek.strike_price >= idx_snap.spot_price)
            .group_by(RawOptionGreek.strike_price)
            .order_by(func.abs(func.sum(RawOptionGreek.gex_value)).desc())
            .limit(5)
            .all()
        )

        for level in sorted(levels_below + levels_above, key=lambda row: row.strike_price):
            overview_data["gamma_levels"][idx_symbol].append(
                {
                    "strike": level.strike_price,
                    "gex": level.net_gex,
                    "type": "resistance" if level.net_gex > 0 else "support",
                }
            )

    return overview_data


def apply_retention(session: Session, retention_days: int):
    cutoff = datetime.now() - timedelta(days=retention_days)
    old_snapshot_ids = [row[0] for row in session.query(GexSnapshot.id).filter(GexSnapshot.timestamp < cutoff).all()]
    if old_snapshot_ids:
        session.execute(delete(RawOptionGreek).where(RawOptionGreek.snapshot_id.in_(old_snapshot_ids)))
        session.execute(delete(GexSnapshot).where(GexSnapshot.id.in_(old_snapshot_ids)))
    session.execute(delete(CollectionRun).where(CollectionRun.started_at < cutoff))
    session.commit()


def maybe_compact_database():
    now = time.time()
    if COMPACT_MARKER_PATH.exists() and now - COMPACT_MARKER_PATH.stat().st_mtime < COMPACT_INTERVAL_SECONDS:
        return
    compact_database()
    COMPACT_MARKER_PATH.write_text(datetime.now().isoformat(), encoding="utf-8")


def run_collection_once() -> dict:
    started = datetime.now()
    try:
        config = load_runtime_config()
    except ConfigError as e:
        logger.error("%s", e)
        return {"ok": False, "message": str(e), "run_id": None, "saved": [], "failed": [], "skipped": []}

    try:
        engine = initialize_database()
    except Exception as e:
        logger.error("Database initialization failed: %s", e)
        return {"ok": False, "message": f"Database initialization failed: {e}", "run_id": None, "saved": [], "failed": [], "skipped": []}
    SessionLocal = get_session_factory(engine)
    session = SessionLocal()
    run = CollectionRun(started_at=started, status="running", symbols_requested=json_list(config["symbols"]))
    session.add(run)
    session.commit()

    saved = []
    failed = []
    skipped = []

    try:
        client = get_client(config)
        rate_limiter = RateLimiter(config["api_rate_limit"])

        for symbol in config["symbols"]:
            result = process_symbol(client, session, run, symbol, config, rate_limiter)
            if result["status"] == "saved":
                saved.append(symbol)
            elif result["status"] == "skipped":
                skipped.append(symbol)
            else:
                failed.append(symbol)

        try:
            overview_data = build_overview_data(session, config["settings"])
            send_event_to_backend({"type": "MARKET_UPDATE", "data": overview_data})
        except Exception as e:
            logger.warning("Event broadcast failed: %s", e)

        if saved:
            try:
                apply_retention(session, config["settings"]["raw_retention_days"])
                maybe_compact_database()
            except Exception as e:
                logger.warning("Retention/compaction failed: %s", e)

        ok = bool(saved or skipped) and not (failed and not saved and not skipped)
        if saved:
            message = f"Saved data for {len(saved)} symbols"
        elif skipped:
            message = f"No data saved; skipped {len(skipped)} symbols without target-day 0DTE expirations"
        else:
            message = "No symbols collected successfully"

        run.status = "success" if ok else "failed"
        run.message = message
        run.finished_at = datetime.now()
        run.symbols_succeeded = json_list(saved)
        run.symbols_failed = json_list(failed)
        run.symbols_skipped = json_list(skipped)
        session.commit()

        return {"ok": ok, "message": message, "run_id": run.id, "saved": saved, "failed": failed, "skipped": skipped}

    except Exception as e:
        logger.exception("Global collection error")
        run.status = "failed"
        run.message = str(e)
        run.finished_at = datetime.now()
        run.symbols_succeeded = json_list(saved)
        run.symbols_failed = json_list(failed or config["symbols"])
        run.symbols_skipped = json_list(skipped)
        session.commit()
        return {"ok": False, "message": str(e), "run_id": run.id, "saved": saved, "failed": failed, "skipped": skipped}
    finally:
        session.close()
        logger.info("Run complete.")


def main_once() -> dict:
    with collector_lock() as acquired:
        if not acquired:
            message = "Collector is already running; refresh skipped."
            logger.warning(message)
            return {"ok": False, "message": message, "run_id": None, "saved": [], "failed": [], "skipped": []}
        return run_collection_once()


def run_loop():
    logger.info("Starting polling collector. Press Ctrl+C to stop.")
    while True:
        result = main_once()
        logger.info("Collector result: %s", result["message"])
        try:
            settings = load_settings()
            delay = settings["backend_update_delay"]
        except ConfigError:
            delay = DEFAULT_SETTINGS["backend_update_delay"]
        time.sleep(delay)


def parse_args(argv: list[str]):
    parser = argparse.ArgumentParser(description="PublicGex 0DTE collector")
    parser.add_argument("--once", action="store_true", help="Run one collection pass and exit")
    parser.add_argument("--reset-db", action="store_true", help="Back up the existing DB and create the current schema")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args(sys.argv[1:])

    if args.reset_db:
        try:
            backup_path = reset_database()
            if backup_path:
                print(f"Backed up existing database to {backup_path}")
            print("Created fresh database schema.")
            sys.exit(0)
        except Exception as e:
            print(f"Failed to reset database: {e}", file=sys.stderr)
            sys.exit(1)

    if args.once:
        result = main_once()
        print(json.dumps(result))
        sys.exit(0 if result["ok"] else 1)

    try:
        run_loop()
    except KeyboardInterrupt:
        logger.info("Collector stopped.")
