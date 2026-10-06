"""Validated local trade-journal CRUD and weekly review calculations."""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import date, datetime, timedelta

from models import SignalEvent, TradeJournalEntry


def _datetime(value, field):
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc


def _date(value, field):
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc


def validate_entry(session, payload: dict, existing=None) -> dict:
    data = dict(payload or {})
    symbol = str(data.get("symbol") or getattr(existing, "symbol", "")).strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    contracts = int(data.get("contracts", getattr(existing, "contracts", 0)) or 0)
    if contracts <= 0:
        raise ValueError("contracts must be a positive integer")
    entry_price = float(data.get("entry_price", getattr(existing, "entry_price", -1)))
    if entry_price < 0:
        raise ValueError("entry_price cannot be negative")
    session_date = _date(data.get("session_date", getattr(existing, "session_date", None)), "session_date")
    entry_time = _datetime(data.get("entry_time", getattr(existing, "entry_time", None)), "entry_time")
    exit_time_raw = data.get("exit_time", getattr(existing, "exit_time", None))
    exit_time = _datetime(exit_time_raw, "exit_time") if exit_time_raw else None
    if exit_time and exit_time < entry_time:
        raise ValueError("exit_time cannot precede entry_time")
    exit_price_raw = data.get("exit_price", getattr(existing, "exit_price", None))
    exit_price = float(exit_price_raw) if exit_price_raw not in (None, "") else None
    if exit_price is not None and exit_price < 0:
        raise ValueError("exit_price cannot be negative")
    scenario_id = data.get("scenario_id", getattr(existing, "scenario_id", None))
    linked = None
    if scenario_id:
        linked = session.query(SignalEvent).filter(SignalEvent.scenario_id == scenario_id).first()
        if linked and (linked.symbol != symbol or (linked.session_date and linked.session_date != session_date)):
            raise ValueError("linked scenario must match journal symbol and session")
    fees = float(data.get("fees", getattr(existing, "fees", 0)) or 0)
    expected_entry = data.get("expected_entry")
    slippage = (entry_price - float(expected_entry)) * 100 * contracts if expected_entry not in (None, "") else data.get("slippage", getattr(existing, "slippage", None))
    pnl = ((exit_price - entry_price) * 100 * contracts - fees) if exit_price is not None else None
    return {
        "symbol": symbol,
        "session_date": session_date,
        "scenario_id": scenario_id,
        "scenario_type": data.get("scenario_type", getattr(existing, "scenario_type", None)) or (linked.scenario_type if linked else None),
        "regime": data.get("regime", getattr(existing, "regime", None)) or (linked.regime if linked else None),
        "event_tags_json": json.dumps(data.get("event_tags", json.loads(getattr(existing, "event_tags_json", "[]") or "[]"))),
        "liquidity_grade": data.get("liquidity_grade", getattr(existing, "liquidity_grade", None)),
        "contracts": contracts,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "entry_time": entry_time,
        "exit_time": exit_time,
        "fees": fees,
        "pnl": pnl,
        "slippage": float(slippage) if slippage not in (None, "") else None,
        "underlying_mfe": data.get("underlying_mfe", getattr(existing, "underlying_mfe", None)),
        "underlying_mae": data.get("underlying_mae", getattr(existing, "underlying_mae", None)),
        "exit_reason": data.get("exit_reason", getattr(existing, "exit_reason", None)),
        "adhered_to_plan": data.get("adhered_to_plan", getattr(existing, "adhered_to_plan", None)),
        "notes": str(data.get("notes", getattr(existing, "notes", "")) or ""),
    }


def serialize_entry(row):
    return {column.name: getattr(row, column.name).isoformat() if isinstance(getattr(row, column.name), (datetime, date)) else getattr(row, column.name) for column in row.__table__.columns}


def create_entry(session, payload):
    values = validate_entry(session, payload)
    row = TradeJournalEntry(created_at=datetime.now(), updated_at=datetime.now(), **values)
    session.add(row)
    session.flush()
    return serialize_entry(row)


def update_entry(session, entry_id: int, payload):
    row = session.get(TradeJournalEntry, int(entry_id))
    if not row:
        raise ValueError("journal entry not found")
    for key, value in validate_entry(session, payload, row).items():
        setattr(row, key, value)
    row.updated_at = datetime.now()
    session.flush()
    return serialize_entry(row)


def delete_entry(session, entry_id: int) -> bool:
    row = session.get(TradeJournalEntry, int(entry_id))
    if not row:
        return False
    session.delete(row)
    return True


def list_entries(session, symbol=None, limit=200):
    query = session.query(TradeJournalEntry)
    if symbol:
        query = query.filter(TradeJournalEntry.symbol == str(symbol).upper())
    return [serialize_entry(row) for row in query.order_by(TradeJournalEntry.entry_time.desc()).limit(limit).all()]


def weekly_review(session, week_start=None):
    start = _date(week_start, "week_start") if week_start else date.today() - timedelta(days=date.today().weekday())
    end = start + timedelta(days=7)
    rows = session.query(TradeJournalEntry).filter(TradeJournalEntry.session_date >= start, TradeJournalEntry.session_date < end).all()
    groups = defaultdict(lambda: {"trades": 0, "pnl": 0.0, "adherent": 0})
    for row in rows:
        keys = {
            "scenario": row.scenario_type or "UNSPECIFIED",
            "regime": row.regime or "UNSPECIFIED",
            "liquidity": row.liquidity_grade or "UNSPECIFIED",
            "adherence": "ADHERED" if row.adhered_to_plan else "DEVIATED",
        }
        for dimension, value in keys.items():
            group = groups[(dimension, value)]
            group["trades"] += 1
            group["pnl"] += float(row.pnl or 0)
            group["adherent"] += 1 if row.adhered_to_plan else 0
    return {"week_start": start.isoformat(), "week_end": end.isoformat(), "groups": [{"dimension": key[0], "value": key[1], **value} for key, value in sorted(groups.items())], "entries": [serialize_entry(row) for row in rows]}
