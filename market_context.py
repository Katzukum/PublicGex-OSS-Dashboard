"""Market-condition calculations with explicit offline/unavailable states."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import pstdev
from urllib.request import urlopen
from zoneinfo import ZoneInfo


EASTERN = ZoneInfo("America/New_York")
BLS_ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
FOMC_ICS_URL = "https://www.federalreserve.gov/monetarypolicy/files/fomcmeetings.ics"


def _parse_ics_datetime(field: str, value: str) -> datetime | None:
    timezone_name = None
    if ";TZID=" in field:
        timezone_name = field.split(";TZID=", 1)[1]
    text = value.strip()
    try:
        if text.endswith("Z"):
            return datetime.strptime(text, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc).astimezone(EASTERN)
        if "T" in text:
            parsed = datetime.strptime(text, "%Y%m%dT%H%M%S")
            return parsed.replace(tzinfo=ZoneInfo(timezone_name) if timezone_name else EASTERN).astimezone(EASTERN)
        return datetime.strptime(text, "%Y%m%d").replace(tzinfo=EASTERN)
    except (ValueError, KeyError):
        return None


def parse_ics_events(text: str, source: str) -> list[dict]:
    events = []
    current = None
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if line == "BEGIN:VEVENT":
            current = {"source": source}
        elif line == "END:VEVENT" and current:
            if current.get("starts_at") and current.get("title"):
                events.append(current)
            current = None
        elif current is not None and ":" in line:
            field, value = line.split(":", 1)
            if field.startswith("DTSTART"):
                current["starts_at"] = _parse_ics_datetime(field, value)
            elif field == "SUMMARY":
                current["title"] = value.replace("\\,", ",")
            elif field == "UID":
                current["uid"] = value

    unique = {}
    for event in events:
        key = event.get("uid") or (event["source"], event["title"], event["starts_at"].isoformat())
        unique[key] = event
    return sorted(unique.values(), key=lambda item: item["starts_at"])


class CachedIcsProvider:
    def __init__(self, source: str, url: str, cache_path: Path, max_age: timedelta = timedelta(hours=12), fetch=None):
        self.source = source
        self.url = url
        self.cache_path = Path(cache_path)
        self.max_age = max_age
        self.fetch = fetch or self._fetch

    @staticmethod
    def _fetch(url: str) -> str:
        with urlopen(url, timeout=5) as response:  # nosec B310 - fixed provider URLs
            return response.read().decode("utf-8")

    def load(self, now: datetime | None = None) -> dict:
        now = now or datetime.now(timezone.utc)
        cached_text = None
        cache_age = None
        if self.cache_path.exists():
            cached_text = self.cache_path.read_text(encoding="utf-8")
            modified = datetime.fromtimestamp(self.cache_path.stat().st_mtime, tz=timezone.utc)
            cache_age = now.astimezone(timezone.utc) - modified
        if cached_text is not None and cache_age is not None and cache_age <= self.max_age:
            return {"status": "cached", "events": parse_ics_events(cached_text, self.source), "warnings": []}
        try:
            text = self.fetch(self.url)
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(text, encoding="utf-8")
            return {"status": "live", "events": parse_ics_events(text, self.source), "warnings": []}
        except Exception as exc:
            if cached_text is not None:
                return {"status": "stale_cache", "events": parse_ics_events(cached_text, self.source), "warnings": [str(exc)]}
            return {"status": "unavailable", "events": [], "warnings": [str(exc)]}


def classify_event_window(events: list[dict], now: datetime | None = None) -> dict:
    now = (now or datetime.now(EASTERN)).astimezone(EASTERN)
    nearest = None
    for event in events:
        starts_at = event.get("starts_at")
        if starts_at is None:
            continue
        minutes = (starts_at.astimezone(EASTERN) - now).total_seconds() / 60
        if nearest is None or abs(minutes) < abs(nearest[0]):
            nearest = (minutes, event)
    if nearest is None:
        return {"state": "NORMAL", "next_event": None, "minutes_to_event": None}
    minutes, event = nearest
    if -15 <= minutes <= 10:
        state = "BLOCKED"
    elif -30 <= minutes <= 60:
        state = "CAUTION"
    else:
        state = "NORMAL"
    return {
        "state": state,
        "next_event": {**event, "starts_at": event["starts_at"].isoformat()},
        "minutes_to_event": round(minutes, 1),
    }


def atm_straddle_implied_move(option_rows: list[dict], spot: float) -> float | None:
    eligible = [row for row in option_rows if row.get("strike_price") is not None]
    if not eligible or not spot:
        return None
    strike = min({float(row["strike_price"]) for row in eligible}, key=lambda value: abs(value - spot))
    premiums = {}
    for row in eligible:
        if float(row["strike_price"]) != strike:
            continue
        side = "put" if "PUT" in str(row.get("option_type", "")).upper() else "call"
        mid = row.get("mid_price")
        if mid is None and row.get("bid") is not None and row.get("ask") is not None:
            mid = (float(row["bid"]) + float(row["ask"])) / 2
        if mid is not None:
            premiums[side] = float(mid)
    return round(premiums["call"] + premiums["put"], 4) if {"call", "put"} <= premiums.keys() else None


def realized_volatility_15m(prices: list[float]) -> float | None:
    clean = [float(value) for value in prices if value and float(value) > 0]
    if len(clean) < 3:
        return None
    returns = [math.log(clean[index] / clean[index - 1]) for index in range(1, len(clean))]
    return round(pstdev(returns) * math.sqrt(len(returns)), 6)


def build_market_context(symbol: str, spot: float, option_rows: list[dict], spot_history: list[dict], events: list[dict], *, now=None, volatility_quotes=None, cross_asset_state=None) -> dict:
    prices = [row.get("spot_price") for row in spot_history if row.get("spot_price") is not None]
    implied_move = atm_straddle_implied_move(option_rows, spot)
    session_range = (max(prices) - min(prices)) if prices else None
    range_consumed = session_range / implied_move if session_range is not None and implied_move else None
    event_risk = classify_event_window(events, now)
    warnings = []
    if implied_move is None:
        warnings.append("ATM straddle quote unavailable")
    volatility_quotes = volatility_quotes or {}
    if not volatility_quotes:
        warnings.append("VIX1D/VIX9D/VIX quotes unavailable")
    return {
        "symbol": symbol.upper(),
        "captured_at": (now or datetime.now(EASTERN)).isoformat(),
        "event_risk": event_risk,
        "implied_move": implied_move,
        "range_consumed": round(range_consumed, 4) if range_consumed is not None else None,
        "realized_volatility_15m": realized_volatility_15m(prices[-16:]),
        "cross_asset_state": cross_asset_state,
        "volatility_term": volatility_quotes,
        "warnings": warnings,
        "status": "available" if not warnings else "partial",
    }


def persist_market_context(session, context: dict):
    from models import MarketContextSnapshot

    row = MarketContextSnapshot(
        captured_at=datetime.fromisoformat(context["captured_at"]),
        symbol=context["symbol"],
        session_date=datetime.fromisoformat(context["captured_at"]).date(),
        event_state=context["event_risk"]["state"],
        implied_move=context.get("implied_move"),
        range_consumed=context.get("range_consumed"),
        realized_volatility_15m=context.get("realized_volatility_15m"),
        cross_asset_state=context.get("cross_asset_state"),
        volatility_term_json=json.dumps(context.get("volatility_term") or {}),
        warnings_json=json.dumps(context.get("warnings") or []),
        source_status_json=json.dumps({"status": context.get("status")}),
    )
    session.add(row)
    return row
