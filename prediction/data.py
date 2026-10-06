"""Timestamp, contract and all-hours bar rules for the live futures experiment.

The verified cash calendar labels forecasts; it does not restrict ingestion or
forecasting. Actual missing observations reset return sequences at any hour.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
import re
from zoneinfo import ZoneInfo

import numpy as np

UTC = timezone.utc
NEW_YORK = ZoneInfo("America/New_York")
STEP = timedelta(minutes=5)
MIN_FIT_RETURNS = 20
MIN_LIVE_CLOSES = 6
MAX_TRAINING_SESSIONS = 60
MAX_LIVE_DELAY_SECONDS = 90
FORECAST_TTL_SECONDS = 390
CALENDAR_SOURCE = "https://ir.theice.com/press/news-details/2024/NYSE-Group-Announces-2025-2026-and-2027-Holiday-and-Early-Closings-Calendar/default.aspx"
HOLIDAYS = frozenset("""
2025-01-01 2025-01-09 2025-01-20 2025-02-17 2025-04-18 2025-05-26
2025-06-19 2025-07-04 2025-09-01 2025-11-27 2025-12-25
2026-01-01 2026-01-19 2026-02-16 2026-04-03 2026-05-25 2026-06-19
2026-07-03 2026-09-07 2026-11-26 2026-12-25
2027-01-01 2027-01-18 2027-02-15 2027-03-26 2027-05-31 2027-06-18
2027-07-05 2027-09-06 2027-11-25 2027-12-24
""".split())
EARLY_CLOSES = frozenset("2025-07-03 2025-11-28 2025-12-24 2026-11-27 2026-12-24 2027-11-26".split())
CONTRACT = re.compile(r"(ES|MES|NQ|MNQ) (?:(?:03|06|09|12)-|(?:MAR|JUN|SEP|DEC))[0-9]{2}")


def utc_now():
    return datetime.now(UTC)


def parse_utc(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamps must include a UTC offset")
    return parsed.astimezone(UTC)


def iso_utc(value):
    return parse_utc(value).isoformat(timespec="microseconds").replace("+00:00", "Z")


def validate_instrument(instrument, symbol=None):
    """Accept supported dated labels without changing their series identity."""
    match = CONTRACT.fullmatch(instrument) if isinstance(instrument, str) else None
    if match is None or (symbol is not None and symbol != match[1]):
        raise ValueError("Use an exact ES/MES/NQ/MNQ quarterly contract, e.g. ES 12-26 or ES DEC26; continuous/index symbols are unsupported")
    return instrument


def session_bounds(day):
    day = date.fromisoformat(day) if isinstance(day, str) else day
    if day.year not in (2025, 2026, 2027):
        raise ValueError("Cash-session calendar is verified only for 2025–2027")
    if day.weekday() >= 5 or day.isoformat() in HOLIDAYS:
        return None
    closing = time(13) if day.isoformat() in EARLY_CLOSES else time(16)
    return (datetime.combine(day, time(9, 30), NEW_YORK).astimezone(UTC),
            datetime.combine(day, closing, NEW_YORK).astimezone(UTC))


def forecast_session_kind(origin, horizon_minutes=30):
    """Label only horizons fully inside a verified cash session as CASH."""
    origin = parse_utc(origin)
    if not math.isfinite(horizon_minutes) or horizon_minutes < 0:
        raise ValueError("Forecast horizon must be finite and nonnegative")
    try:
        bounds = session_bounds(origin.astimezone(NEW_YORK).date())
    except ValueError:
        return "EXTENDED"
    if bounds is not None and bounds[0] < origin and origin + timedelta(minutes=horizon_minutes) <= bounds[1]:
        return "CASH"
    return "EXTENDED"


def normalize_bars(bars, now):
    """Validate the whole frame and retain completed bars at every hour."""
    now = parse_utc(now)
    if not isinstance(bars, list) or not 1 <= len(bars) <= 256:
        raise ValueError("BAR_BATCH must contain 1–256 bars")
    selected, seen = [], {}
    for bar in bars:
        if not isinstance(bar, dict) or isinstance(bar.get("close"), bool):
            raise ValueError("Each bar needs end_utc and a positive finite close")
        stamp = parse_utc(bar["end_utc"])
        close = float(bar["close"])
        if not math.isfinite(close) or not 0 < close < 1_000_000:
            raise ValueError("Bar close must be positive and finite")
        if stamp.second or stamp.microsecond or stamp.minute % 5:
            raise ValueError("Bar end must align to a completed five-minute boundary")
        if stamp > now:
            raise ValueError("Future or unfinished bars are not accepted")
        key = iso_utc(stamp)
        if key in seen and seen[key] != close:
            raise ValueError("Conflicting duplicate bars in the same batch")
        seen[key] = close
        selected.append({"end_utc": key, "close": close})
    return sorted(selected, key=lambda row: row["end_utc"])


def split_contiguous(rows):
    blocks, current, previous = [], [], None
    for row in rows:
        stamp = parse_utc(row["end_utc"])
        if previous is not None and stamp - previous != STEP:
            if current:
                blocks.append(current)
            current = []
        current.append(row)
        previous = stamp
    if current:
        blocks.append(current)
    return blocks


def training_data(rows, forecast_day, *, before=None):
    """Use available fragments, with no minimum session count or coverage gate.

    By default only preceding dates are included. ``before`` also permits
    same-day data strictly earlier than that forecast origin for cold starts.
    Return sequences cross midnight and cash-session boundaries, resetting only
    when actual bars are missing. At most 60 observed New York dates are used.
    """
    cutoff = parse_utc(before) if before is not None else None
    grouped = defaultdict(list)
    for row in rows:
        stamp = parse_utc(row["end_utc"])
        day = stamp.astimezone(NEW_YORK).date()
        if cutoff is not None and stamp >= cutoff:
            continue
        if day < forecast_day or (cutoff is not None and day == forecast_day):
            grouped[day].append(row)
    selected_days = sorted(grouped)[-MAX_TRAINING_SESSIONS:]
    values = sorted((row for day in selected_days for row in grouped[day]), key=lambda row: parse_utc(row["end_utc"]))
    fragments = [fragment for fragment in split_contiguous(values) if len(fragment) >= 2]
    blocks = [{"y": np.log([row["close"] for row in fragment]) * 10000}
              for fragment in fragments]
    used_rows = [row for fragment in fragments for row in fragment]
    used_days = sorted({parse_utc(row["end_utc"]).astimezone(NEW_YORK).date() for row in used_rows})
    evidence = [[(row["end_utc"], row["close"]) for row in fragment] for fragment in fragments]
    fingerprint = hashlib.sha256(json.dumps(evidence, separators=(",", ":")).encode()).hexdigest()
    return blocks, {
        "sessions": len(used_days), "required_returns": MIN_FIT_RETURNS,
        "training_points": sum(len(b["y"]) for b in blocks),
        "training_returns": sum(len(b["y"]) - 1 for b in blocks),
        "trained_through": used_days[-1].isoformat() if used_days else None,
        "training_end_utc": iso_utc(max(parse_utc(row["end_utc"]) for row in used_rows)) if used_rows else None,
        "fingerprint": fingerprint,
    }


def live_closes(rows, origin):
    """Use the latest actual contiguous sequence, without reading past origin."""
    origin = parse_utc(origin)
    values = sorted((r for r in rows if parse_utc(r["end_utc"]) <= origin), key=lambda row: parse_utc(row["end_utc"]))
    fragments = split_contiguous(values)
    if not fragments or parse_utc(fragments[-1][-1]["end_utc"]) != origin or len(fragments[-1]) < MIN_LIVE_CLOSES:
        return []
    return [r["close"] for r in fragments[-1]]
