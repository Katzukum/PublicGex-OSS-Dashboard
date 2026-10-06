"""Persisted decision-transition alerts with deterministic deduplication."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta

from models import DecisionAlert


DEFAULT_COOLDOWN = timedelta(minutes=10)


def _dedupe_key(symbol: str, alert_type: str, state_to: str, scenario_id: str | None) -> str:
    raw = f"{symbol}:{alert_type}:{state_to}:{scenario_id or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def emit_transition_alert(session, *, symbol: str, alert_type: str, state_from: str | None, state_to: str, message: str, scenario_id: str | None = None, severity: str = "info", payload: dict | None = None, now: datetime | None = None, cooldown: timedelta = DEFAULT_COOLDOWN):
    now = now or datetime.now()
    symbol = symbol.upper()
    dedupe = _dedupe_key(symbol, alert_type, state_to, scenario_id)
    if session.query(DecisionAlert).filter(DecisionAlert.dedupe_key == dedupe).first():
        return None
    latest = (
        session.query(DecisionAlert)
        .filter(DecisionAlert.symbol == symbol, DecisionAlert.alert_type == alert_type)
        .order_by(DecisionAlert.created_at.desc())
        .first()
    )
    opposite = latest is not None and latest.state_from == state_to and latest.state_to == state_from
    critical = severity.lower() == "critical"
    if latest and now - latest.created_at < cooldown and not opposite and not critical:
        return None
    row = DecisionAlert(
        created_at=now,
        symbol=symbol,
        alert_type=alert_type,
        severity=severity,
        scenario_id=scenario_id,
        dedupe_key=dedupe,
        state_from=state_from,
        state_to=state_to,
        message=message,
        payload_json=json.dumps(payload or {}, default=str),
    )
    session.add(row)
    session.flush()
    return serialize_alert(row)


def serialize_alert(row: DecisionAlert) -> dict:
    return {
        "alert_id": f"alert_{row.id}",
        "created_at": row.created_at.isoformat(),
        "symbol": row.symbol,
        "alert_type": row.alert_type,
        "severity": row.severity,
        "scenario_id": row.scenario_id,
        "state_from": row.state_from,
        "state_to": row.state_to,
        "message": row.message,
    }


def evaluate_workspace_alerts(session, workspace: dict, now: datetime | None = None) -> list[dict]:
    symbol = workspace["symbol"]
    scenario = workspace.get("active_scenario") or {}
    scenario_type = scenario.get("scenario_type") or "NO_TRADE"
    scenario_id = scenario.get("scenario_id")
    previous = (
        session.query(DecisionAlert)
        .filter(DecisionAlert.symbol == symbol, DecisionAlert.alert_type == "scenario")
        .order_by(DecisionAlert.created_at.desc())
        .first()
    )
    emitted = []
    alert = emit_transition_alert(
        session,
        symbol=symbol,
        alert_type="scenario",
        state_from=previous.state_to if previous else None,
        state_to=scenario_type,
        scenario_id=scenario_id,
        message=f"{symbol} scenario changed to {scenario_type}",
        payload={"eligibility": workspace.get("eligibility")},
        now=now,
    )
    if alert:
        emitted.append(alert)
    event_state = workspace.get("market_context", {}).get("event_risk", {}).get("state")
    if event_state in {"CAUTION", "BLOCKED"}:
        alert = emit_transition_alert(
            session,
            symbol=symbol,
            alert_type="event",
            state_from=None,
            state_to=event_state,
            scenario_id=scenario_id,
            severity="critical" if event_state == "BLOCKED" else "warning",
            message=f"{symbol} event window is {event_state}",
            now=now,
        )
        if alert:
            emitted.append(alert)
    return emitted


def recent_alerts(session, limit=20) -> list[dict]:
    rows = session.query(DecisionAlert).order_by(DecisionAlert.created_at.desc()).limit(limit).all()
    return [serialize_alert(row) for row in rows]
