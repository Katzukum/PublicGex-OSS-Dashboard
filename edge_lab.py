"""Filtered historical-evidence queries for the Edge Lab."""

from __future__ import annotations

import json
from datetime import datetime

from models import SignalEvent, SignalOutcome
from signal_performance import _stats_from_rows, independent_outcomes, parse_datetime


def _time_bucket(value) -> str:
    timestamp = parse_datetime(value)
    if not timestamp:
        return "unknown"
    if timestamp.hour < 11:
        return "open"
    if timestamp.hour < 14:
        return "midday"
    return "close"


def query_edge_lab(session, filters: dict | None = None) -> dict:
    filters = filters or {}
    symbol = str(filters.get("symbol") or "SPX").upper()
    horizon = max(15, min(int(filters.get("horizon") or 30), 60))
    include_legacy = bool(filters.get("include_legacy", False))
    query = (
        session.query(SignalOutcome)
        .join(SignalEvent)
        .filter(SignalEvent.symbol == symbol)
        .filter(SignalOutcome.horizon_minutes == horizon)
        .order_by(SignalEvent.emitted_at.asc())
    )
    if not include_legacy:
        query = query.filter(SignalEvent.is_opportunity.is_(True))
    if filters.get("scenario"):
        query = query.filter(SignalEvent.scenario_type == filters["scenario"])
    if filters.get("regime"):
        query = query.filter(SignalEvent.regime == filters["regime"])
    if filters.get("liquidity_grade"):
        query = query.filter(SignalEvent.liquidity_grade == filters["liquidity_grade"])

    rows = query.limit(5000).all()
    event_tag = filters.get("event_tag")
    time_bucket = filters.get("time_bucket")
    if event_tag:
        rows = [row for row in rows if event_tag in json.loads(row.signal.event_tags_json or "[]")]
    if time_bucket:
        rows = [row for row in rows if _time_bucket(row.signal.emitted_at) == time_bucket]

    independent = independent_outcomes(rows, horizon)
    stats = _stats_from_rows(rows, "independent opportunities", horizon)
    opportunities = []
    distribution = []
    breakdown = {"target": 0, "invalidation": 0, "directional": 0, "against": 0, "wait": 0}
    calibration = []
    for row in independent:
        event = row.signal
        directional = row.directional_move_points if row.directional_move_points is not None else row.move_points
        after_cost = directional - 0.10 if directional is not None else None
        if after_cost is not None:
            distribution.append(after_cost)
        breakdown[row.outcome_label if row.outcome_label in breakdown else "against"] += 1
        if event.edge_probability is not None and row.is_win is not None:
            calibration.append({"forecast": event.edge_probability, "outcome": 1 if row.is_win else 0})
        opportunities.append(
            {
                "signal_id": event.id,
                "scenario_id": event.scenario_id,
                "session_date": str(event.session_date or parse_datetime(event.emitted_at).date()),
                "emitted_at": str(event.emitted_at),
                "scenario_type": event.scenario_type,
                "regime": event.regime,
                "liquidity_grade": event.liquidity_grade,
                "outcome": row.outcome_label,
                "after_cost_points": after_cost,
                "mfe": row.max_favorable_points,
                "mae": row.max_adverse_points,
            }
        )

    warning = "Legacy rows are refresh-correlated diagnostics, not independent evidence." if include_legacy else None
    return {
        "symbol": symbol,
        "horizon_minutes": horizon,
        "filters": filters,
        "status": stats["evidence_status"],
        "stats": stats,
        "calibration": calibration,
        "expectancy_distribution": distribution,
        "outcome_breakdown": breakdown,
        "opportunities": opportunities,
        "legacy_included": include_legacy,
        "warning": warning,
        "generated_at": datetime.now().isoformat(),
    }
