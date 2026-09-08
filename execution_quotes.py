"""Read-only strategy quote normalization and liquidity validation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any


def _value(obj: Any, *names: str, default=None):
    for name in names:
        if isinstance(obj, dict) and name in obj:
            return obj[name]
        if hasattr(obj, name):
            return getattr(obj, name)
    return default


def _number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None


def _timestamp(value):
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def build_strategy_request(base_symbol: str, legs: list[dict]):
    """Build the SDK quote request without importing any trading endpoints."""
    from public_api_sdk import StrategyQuoteRequest
    from public_api_sdk.models.strategy_quote import OpenCloseIndicator, OrderSide, StrategyOrderLeg

    sdk_legs = [
        StrategyOrderLeg(
            symbol=str(leg["symbol"]),
            side=OrderSide(str(leg["side"]).upper()),
            open_close_indicator=OpenCloseIndicator.OPEN,
            ratio_quantity=int(leg.get("ratio_quantity", 1)),
        )
        for leg in legs
    ]
    return StrategyQuoteRequest(base_symbol=base_symbol.upper(), option_legs=sdk_legs)


def get_strategy_market_quote(
    client,
    request,
    *,
    account_id: str | None = None,
    now: datetime | None = None,
    max_age_seconds: int = 20,
    max_spread_ratio: float = 0.35,
    structure_width: float | None = None,
    fees_per_contract: float = 0.0,
) -> dict:
    """Call only ``get_strategy_quote`` and return conservative economics."""
    received_at = now or datetime.now(timezone.utc)
    if received_at.tzinfo is None:
        received_at = received_at.replace(tzinfo=timezone.utc)
    response = client.get_strategy_quote(request, account_id=account_id)

    bid = _number(_value(response, "bid", "price"))
    ask = _number(_value(response, "ask"))
    mid = _number(_value(response, "mark"))
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2
    legs = []
    timestamps = []
    for item in _value(response, "strategy_legs", "strategyLegs", default=[]) or []:
        quote = _value(item, "quote", default={}) or {}
        ts = _timestamp(_value(quote, "timestamp"))
        if ts:
            timestamps.append(ts)
        legs.append(
            {
                "symbol": _value(_value(item, "instrument", default={}), "symbol") or _value(quote, "symbol"),
                "bid": _number(_value(quote, "bid")),
                "ask": _number(_value(quote, "ask")),
                "bid_size": _number(_value(quote, "bid_size", "bidSize")),
                "ask_size": _number(_value(quote, "ask_size", "askSize")),
                "timestamp": ts.isoformat() if ts else None,
            }
        )

    warnings = []
    rejection = None
    if bid is None or ask is None or mid is None:
        rejection = "missing strategy market"
    elif ask < bid:
        rejection = "crossed strategy market"
    elif bid <= 0:
        rejection = "zero bid"

    quote_time = min(timestamps) if timestamps else None
    age_seconds = (received_at - quote_time).total_seconds() if quote_time else None
    if quote_time is None:
        warnings.append("missing source timestamp")
    elif age_seconds > max_age_seconds and rejection is None:
        rejection = f"stale quote ({round(age_seconds)}s)"

    spread = (ask - bid) if bid is not None and ask is not None else None
    spread_ratio = spread / mid if spread is not None and mid and mid > 0 else None
    if spread_ratio is not None and spread_ratio > max_spread_ratio and rejection is None:
        rejection = "strategy spread too wide"

    sizes = [size for leg in legs for size in (leg["bid_size"], leg["ask_size"]) if size is not None]
    min_size = min(sizes) if sizes else 0
    if rejection:
        grade = "REJECTED"
        status = "REJECTED"
    elif spread_ratio is not None and spread_ratio <= 0.10 and min_size >= 10:
        grade, status = "A", "EXECUTABLE"
    elif spread_ratio is not None and spread_ratio <= 0.20 and min_size >= 3:
        grade, status = "B", "EXECUTABLE"
    else:
        grade, status = "C", "EXECUTABLE"

    entry_debit = ask if status == "EXECUTABLE" else None
    exit_credit = bid if status == "EXECUTABLE" else None
    max_loss = ((entry_debit or 0) * 100 + fees_per_contract) if entry_debit is not None else None
    max_reward = None
    if max_loss is not None and structure_width is not None:
        max_reward = max(0.0, float(structure_width) * 100 - max_loss)

    return {
        "status": status,
        "liquidity_grade": grade,
        "reason": rejection,
        "timestamp": quote_time.isoformat() if quote_time else received_at.isoformat(),
        "age_seconds": age_seconds,
        "bid": bid,
        "mid": mid,
        "ask": ask,
        "spread": spread,
        "spread_ratio": spread_ratio,
        "entry_debit": entry_debit,
        "exit_credit": exit_credit,
        "max_loss": max_loss,
        "max_reward": max_reward,
        "legs": legs,
        "warnings": warnings,
    }


def candidate_from_persisted_quotes(idea: dict, rows: list[dict], *, symbol: str, now: datetime, maximum_risk: float = 500, fees_per_contract: float = 1.25, max_age_seconds: int = 20) -> dict:
    """Enrich a modeled fly/spread with conservative stored leg markets."""
    candidate = dict(idea or {})
    if candidate.get("status") != "ready":
        candidate["status"] = "REJECTED"
        candidate["reason"] = candidate.get("reason") or "modeled structure unavailable"
        return candidate
    side = str(candidate.get("side") or "").upper()
    if candidate.get("kind") == "Butterfly":
        strikes = [candidate.get("lower"), candidate.get("center"), candidate.get("upper")]
        ratios = [1, -2, 1]
        width = float(candidate.get("width") or 0)
    else:
        strikes = [candidate.get("long_strike"), candidate.get("short_strike")]
        ratios = [1, -1]
        width = float(candidate.get("width") or 0)

    selected = []
    for strike, ratio in zip(strikes, ratios):
        row = next(
            (
                item
                for item in rows
                if _number(item.get("strike_price")) == _number(strike)
                and side in str(item.get("option_type") or "").upper()
            ),
            None,
        )
        if row is None or row.get("bid") is None or row.get("ask") is None:
            candidate.update({"status": "MODELED_ONLY", "quote": None, "contracts": None, "reason": "one or more leg markets unavailable"})
            return candidate
        selected.append((row, ratio))

    strategy_ask = sum((float(row["ask"]) if ratio > 0 else float(row["bid"])) * ratio for row, ratio in selected)
    strategy_bid = sum((float(row["bid"]) if ratio > 0 else float(row["ask"])) * ratio for row, ratio in selected)
    strategy_mid = (strategy_bid + strategy_ask) / 2
    timestamps = [
        _timestamp(row.get("ask_timestamp") if ratio > 0 else row.get("bid_timestamp"))
        for row, ratio in selected
    ]
    timestamps = [value for value in timestamps if value]
    quote_time = min(timestamps) if timestamps else None
    normalized_now = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    age_seconds = (normalized_now.astimezone(timezone.utc) - quote_time.astimezone(timezone.utc)).total_seconds() if quote_time else None
    if age_seconds is not None and age_seconds > -5:
        age_seconds = max(0, age_seconds)
    spread = strategy_ask - strategy_bid
    spread_ratio = spread / strategy_mid if strategy_mid > 0 else None
    sizes = [int(row.get("bid_size") or 0) for row, _ in selected] + [int(row.get("ask_size") or 0) for row, _ in selected]
    min_size = min(sizes) if sizes else 0
    reason = None
    if strategy_bid <= 0:
        reason = "zero or negative strategy bid"
    elif strategy_ask < strategy_bid:
        reason = "crossed strategy market"
    elif quote_time is None:
        reason = "missing leg quote timestamp"
    elif age_seconds < 0:
        reason = "leg quote timestamp is in the future"
    elif age_seconds > max_age_seconds:
        reason = f"stale leg quote ({round(age_seconds)}s)"
    elif spread_ratio is None or spread_ratio > 0.35:
        reason = "strategy spread too wide"

    if reason:
        status, grade = "REJECTED", "REJECTED"
    elif spread_ratio <= 0.10 and min_size >= 10:
        status, grade = "EXECUTABLE", "A"
    elif spread_ratio <= 0.20 and min_size >= 3:
        status, grade = "EXECUTABLE", "B"
    else:
        status, grade = "EXECUTABLE", "C"

    max_loss = strategy_ask * 100 + fees_per_contract if status == "EXECUTABLE" else None
    max_reward = max(0, width * 100 - max_loss) if max_loss is not None else None
    contracts = int(maximum_risk // max_loss) if max_loss and maximum_risk >= max_loss else 0 if status == "EXECUTABLE" else None
    settlement = "CASH" if symbol.upper() in {"SPX", "NDX", "RUT", "VIX", "DJX"} else "PHYSICAL"
    candidate.update(
        {
            "modeled_debit": candidate.get("estimated_debit"),
            "status": status,
            "liquidity_grade": grade,
            "reason": reason,
            "contracts": contracts,
            "maximum_risk_input": maximum_risk,
            "settlement_type": settlement,
            "quote": {
                "timestamp": quote_time.isoformat() if quote_time else None,
                "age_seconds": age_seconds,
                "bid": round(strategy_bid, 4),
                "mid": round(strategy_mid, 4),
                "ask": round(strategy_ask, 4),
                "spread": round(spread, 4),
                "spread_ratio": spread_ratio,
            },
            "max_loss_dollars": max_loss,
            "max_reward_dollars": max_reward,
            "time_to_close": "16:00 America/New_York",
        }
    )
    return candidate
