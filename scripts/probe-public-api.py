"""Read-only Public.com capability probe; prints JSON and never opens the app DB.

Run by 09:20 ET with --full to include the scheduled open-interest readings.
The default mode runs the remaining checks during regular market hours.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, time as clock_time
from pathlib import Path
from zoneinfo import ZoneInfo

from public_api_sdk import (
    ApiKeyAuthConfig,
    BarAggregation,
    BarPeriod,
    InstrumentType,
    OptionChainRequest,
    OptionExpirationsRequest,
    OrderInstrument,
    PublicApiClient,
    PublicApiClientConfiguration,
)


EASTERN = ZoneInfo("America/New_York")
SYMBOLS = ("SPX", "SPY", "QQQ", "NDX", "IWM")
INDEX_SYMBOLS = frozenset(("SPX", "NDX"))
MIN_REQUEST_INTERVAL_SECONDS = 0.2  # 5 requests per second maximum.
FLOW_POLL_SECONDS = 3.0
FLOW_DURATION_SECONDS = 300.0
OI_READING_TIMES = (clock_time(9, 25), clock_time(9, 35), clock_time(12, 0))


def field(value, *names):
    for name in names:
        candidate = value.get(name) if isinstance(value, dict) else getattr(value, name, None)
        if candidate is not None:
            return candidate
    return None


def safe_error(exc: Exception) -> str:
    message = str(exc)
    for marker in ("api_secret_key", "Authorization", "Bearer "):
        if marker.lower() in message.lower():
            return f"{type(exc).__name__}: redacted authentication error"
    return f"{type(exc).__name__}: {message[:240]}"


def credentials(path: Path) -> tuple[str, str]:
    values = {}
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    key = values.get("PUBLIC_API_KEY", "")
    account = values.get("PUBLIC_ACCOUNT_ID", "")
    if not key or not account:
        raise ValueError("PUBLIC_API_KEY and PUBLIC_ACCOUNT_ID are required in the selected app .env")
    return key, account


def default_env_path() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    if local:
        desktop = Path(local) / "com.publicgex.dashboard" / "data" / ".env"
        if desktop.is_file():
            return desktop
    return Path(__file__).resolve().parents[1] / "data" / ".env"


class Probe:
    def __init__(self, client):
        self.client = client
        self.next_request_at = 0.0
        self.request_count = 0
        self.http_429_count = 0

    def call(self, method, *args, **kwargs):
        delay = self.next_request_at - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self.next_request_at = time.monotonic() + MIN_REQUEST_INTERVAL_SECONDS
        self.request_count += 1
        try:
            return method(*args, **kwargs)
        except Exception as exc:
            response = getattr(exc, "response", None)
            status = getattr(response, "status_code", None) or getattr(exc, "status_code", None)
            if status == 429 or "429" in str(exc):
                self.http_429_count += 1
                raise RuntimeError("HTTP 429 received; probe stopped") from None
            raise

    @staticmethod
    def instrument(symbol: str):
        kind = InstrumentType.INDEX if symbol in INDEX_SYMBOLS else InstrumentType.EQUITY
        return OrderInstrument(symbol=symbol, type=kind)

    def expirations(self, symbol: str):
        response = self.call(
            self.client.get_option_expirations,
            OptionExpirationsRequest(instrument=self.instrument(symbol)),
        )
        return list(field(response, "expirations") or [])

    def chain(self, symbol: str, expiration: str):
        response = self.call(
            self.client.get_option_chain,
            OptionChainRequest(instrument=self.instrument(symbol), expiration_date=expiration),
        )
        return list(field(response, "calls") or []) + list(field(response, "puts") or [])

    def quotes(self, symbols: list[str], kind=InstrumentType.OPTION):
        instruments = [OrderInstrument(symbol=symbol, type=kind) for symbol in symbols]
        return list(self.call(self.client.get_quotes, instruments) or [])


def attempt(fn):
    try:
        return {"yes": True, "result": fn()}
    except Exception as exc:
        if "HTTP 429" in str(exc):
            raise
        return {"yes": False, "error": safe_error(exc)}


def coverage(chain: list) -> dict:
    paths = {
        "greeks": lambda q: field(field(q, "option_details"), "greeks"),
        "implied_volatility": lambda q: field(field(field(q, "option_details"), "greeks"), "implied_volatility", "impliedVolatility"),
        "open_interest": lambda q: field(q, "open_interest"),
        "volume": lambda q: field(q, "volume"),
        "bid_size": lambda q: field(q, "bid_size"),
        "ask_size": lambda q: field(q, "ask_size"),
        "bid_timestamp": lambda q: field(q, "bid_timestamp"),
        "ask_timestamp": lambda q: field(q, "ask_timestamp"),
        "last_timestamp": lambda q: field(q, "last_timestamp"),
    }
    total = len(chain)
    return {
        "yes": total > 0,
        "contracts": total,
        "field_coverage_percent": {
            name: round(100 * sum(getter(item) is not None for item in chain) / total, 2) if total else 0
            for name, getter in paths.items()
        },
    }


def bar_summary(response) -> dict:
    session = field(response, "regular_market")
    bars = list(field(session, "bars") or [])
    return {
        "yes": bool(bars),
        "count": len(bars),
        "first": str(field(bars[0], "timestamp")) if bars else None,
        "last": str(field(bars[-1], "timestamp")) if bars else None,
        "volume_populated_percent": round(100 * sum(field(bar, "volume") is not None for bar in bars) / len(bars), 2) if bars else 0,
    }


def nearest_contracts(chain: list, spot: float, count: int) -> list[str]:
    eligible = []
    for quote in chain:
        instrument = field(quote, "instrument")
        symbol = field(instrument, "symbol")
        details = field(quote, "option_details")
        strike = field(details, "strike_price")
        if symbol and strike is not None:
            eligible.append((abs(float(strike) - spot), str(symbol)))
    eligible.sort()
    return [symbol for _, symbol in eligible[:count]]


def flow_poll(probe: Probe, contracts: list[str]) -> dict:
    sizes = (50, 100, 250)
    samples = {size: [] for size in sizes}
    previous = {}
    changed = Counter()
    deadline = time.monotonic() + FLOW_DURATION_SECONDS
    while time.monotonic() < deadline:
        cycle_start = time.monotonic()
        for size in sizes:
            chosen = contracts[:size]
            if len(chosen) < size:
                samples[size].append({"requested": len(chosen), "error": "insufficient contracts"})
                continue
            try:
                quotes = probe.quotes(chosen)
                samples[size].append({"requested": size, "returned": len(quotes)})
                for quote in quotes:
                    key = field(field(quote, "instrument"), "symbol")
                    current = (field(quote, "volume"), str(field(quote, "last_timestamp")))
                    if key in previous:
                        if current[0] != previous[key][0]:
                            changed["volume"] += 1
                        if current[1] != previous[key][1]:
                            changed["last_timestamp"] += 1
                    previous[key] = current
            except Exception as exc:
                if "HTTP 429" in str(exc):
                    raise
                samples[size].append({"requested": size, "error": safe_error(exc)})
        time.sleep(max(0.0, FLOW_POLL_SECONDS - (time.monotonic() - cycle_start)))
    return {"duration_seconds": FLOW_DURATION_SECONDS, "batch_samples": samples, "changed_between_polls": dict(changed)}


def wait_until_et(target: clock_time):
    while True:
        now = datetime.now(EASTERN)
        if now.time() >= target:
            return now
        time.sleep(min(30.0, max(0.1, (datetime.combine(now.date(), target, EASTERN) - now).total_seconds())))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=default_env_path(), help="this app's .env path")
    parser.add_argument("--full", action="store_true", help="start by 09:20 ET and include 09:25, 09:35 and noon OI readings")
    args = parser.parse_args()
    started = datetime.now(EASTERN)
    if started.weekday() >= 5 or not clock_time(9, 0) <= started.time() <= clock_time(16, 0):
        parser.error("run during a weekday market session (09:00–16:00 ET); verify market holidays separately")
    if args.full and started.time() > clock_time(9, 20):
        parser.error("--full must start by 09:20 ET to prepare the 09:25 reading")
    if (args.env_file.parent / "gex_collector.lock").exists():
        parser.error("the app collector lock exists; stop this app's collector before probing so account requests stay within the rate limit")
    key, account = credentials(args.env_file)
    client = PublicApiClient(
        ApiKeyAuthConfig(api_secret_key=key),
        config=PublicApiClientConfiguration(default_account_number=account),
    )
    probe = Probe(client)
    output = {
        "run_started_et": started.isoformat(),
        "sdk_version": importlib.metadata.version("publicdotcom-py"),
        "credential_source": str(args.env_file),
        "checks": {},
    }
    try:
        expirations = {}
        for symbol in SYMBOLS:
            expirations[symbol] = attempt(lambda s=symbol: probe.expirations(s))
        output["checks"]["0.2_expirations"] = {
            "yes": all(item["yes"] and bool(item.get("result")) for item in expirations.values()),
            "symbols": {
            symbol: {"yes": item["yes"], "count": len(item.get("result", [])), "error": item.get("error")}
            for symbol, item in expirations.items()
            },
        }
        spx_exp = expirations["SPX"].get("result", [])
        today = started.date().isoformat()
        expiry = today if today in spx_exp else None
        chain = []
        spot = None
        if expiry:
            chain_result = attempt(lambda: probe.chain("SPX", expiry))
            chain = chain_result.get("result", [])
            output["checks"]["0.1_chain"] = coverage(chain) if chain_result["yes"] else chain_result
        else:
            output["checks"]["0.1_chain"] = {"yes": False, "error": "SPX 0DTE expiration unavailable"}

        spot_result = attempt(lambda: probe.quotes(["SPX"], InstrumentType.INDEX))
        spot = field(spot_result["result"][0], "last") if spot_result["yes"] and spot_result["result"] else None
        contracts = nearest_contracts(chain, float(spot), 250) if spot is not None else []
        readings = []
        if args.full:
            observed = wait_until_et(OI_READING_TIMES[0])
            result = attempt(lambda: probe.quotes(contracts[:20])) if len(contracts) >= 20 else {"yes": False, "error": "fewer than 20 contracts"}
            readings.append({
                "scheduled_et": OI_READING_TIMES[0].isoformat(timespec="minutes"),
                "observed_et": observed.isoformat(),
                "yes": result["yes"],
                "open_interest": {str(field(field(q, "instrument"), "symbol")): field(q, "open_interest") for q in result.get("result", [])},
                "error": result.get("error"),
            })

        if chain:
            timings = []
            for _ in range(10):
                begin = time.monotonic()
                probe.chain("SPX", expiry)
                timings.append(round(time.monotonic() - begin, 3))
            output["checks"]["0.2_chain_call_seconds"] = {"yes": len(timings) == 10, "samples": timings}

        for symbol in INDEX_SYMBOLS:
            for period in (BarPeriod.DAY, BarPeriod.WEEK, BarPeriod.MONTH):
                name = f"{symbol}_{period.value}"
                result = attempt(lambda s=symbol, p=period: probe.call(client.get_bars, s, p, instrument_type=InstrumentType.INDEX, aggregation=BarAggregation.ONE_MINUTE))
                output["checks"].setdefault("0.3_index_bars", {})[name] = bar_summary(result["result"]) if result["yes"] else result

        if contracts:
            response = attempt(lambda: probe.call(client.get_bars, contracts[0], BarPeriod.DAY, instrument_type=InstrumentType.OPTION, aggregation=BarAggregation.ONE_MINUTE))
            output["checks"]["0.4_option_bars"] = bar_summary(response["result"]) if response["yes"] else response
        else:
            output["checks"]["0.4_option_bars"] = {"yes": False, "error": "no near-ATM option contract"}

        output["checks"]["0.5_volatility_indexes"] = {}
        for symbol in ("VIX", "VIX1D", "VIX9D"):
            result = attempt(lambda s=symbol: probe.quotes([s], InstrumentType.INDEX))
            output["checks"]["0.5_volatility_indexes"][symbol] = {
                "yes": result["yes"] and bool(result.get("result")),
                "last_present": bool(result.get("result")) and field(result["result"][0], "last") is not None,
                "error": result.get("error"),
            }

        if args.full:
            for target in OI_READING_TIMES[1:2]:
                observed = wait_until_et(target)
                result = attempt(lambda: probe.quotes(contracts[:20])) if len(contracts) >= 20 else {"yes": False, "error": "fewer than 20 contracts"}
                readings.append({
                    "scheduled_et": target.isoformat(timespec="minutes"),
                    "observed_et": observed.isoformat(),
                    "yes": result["yes"],
                    "open_interest": {
                        str(field(field(q, "instrument"), "symbol")): field(q, "open_interest")
                        for q in result.get("result", [])
                    },
                    "error": result.get("error"),
                })
        output["checks"]["0.6_quote_polling"] = attempt(lambda: flow_poll(probe, contracts)) if contracts else {"yes": False, "error": "no near-ATM contracts"}

        if args.full:
            for target in OI_READING_TIMES[2:]:
                observed = wait_until_et(target)
                result = attempt(lambda: probe.quotes(contracts[:20])) if len(contracts) >= 20 else {"yes": False, "error": "fewer than 20 contracts"}
                readings.append({
                    "scheduled_et": target.isoformat(timespec="minutes"),
                    "observed_et": observed.isoformat(),
                    "yes": result["yes"],
                    "open_interest": {str(field(field(q, "instrument"), "symbol")): field(q, "open_interest") for q in result.get("result", [])},
                    "error": result.get("error"),
                })
            output["checks"]["0.7_open_interest"] = {"yes": all(item["yes"] for item in readings) and len(readings) == 3, "readings": readings}
        else:
            if datetime.now(EASTERN).time() >= OI_READING_TIMES[1] and len(contracts) >= 20:
                result = attempt(lambda: probe.quotes(contracts[:20]))
                output["checks"]["0.7_open_interest"] = {
                    "yes": False, "fallback": "first available reading after 09:35 ET",
                    "observed_et": datetime.now(EASTERN).isoformat(),
                    "reading_succeeded": result["yes"],
                    "open_interest": {
                        str(field(field(q, "instrument"), "symbol")): field(q, "open_interest")
                        for q in result.get("result", [])
                    },
                    "error": result.get("error"),
                }
            else:
                output["checks"]["0.7_open_interest"] = {"yes": False, "error": "timing unverified; use --full by 09:20 ET for the three scheduled readings"}
    except Exception as exc:
        output["stopped_error"] = safe_error(exc)
    output["run_finished_et"] = datetime.now(EASTERN).isoformat()
    output["requests"] = probe.request_count
    output["http_429_responses"] = probe.http_429_count
    print(json.dumps(output, indent=2, default=str))
    return 1 if output.get("stopped_error") or probe.http_429_count else 0


if __name__ == "__main__":
    sys.exit(main())
