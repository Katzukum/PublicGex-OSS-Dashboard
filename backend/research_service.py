"""Assemble observational option research from one bounded session read."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from exhaustion import build_exhaustion_events, unavailable_exhaustion
from flow_pressure import build_estimated_pressure, unavailable_estimated_pressure
from option_envelopes import build_option_envelopes, unavailable_option_envelopes
from research_observations import load_session_observations


def _value(row: Any, key: str, default=None):
    if isinstance(row, Mapping):
        return row.get(key, default)
    if hasattr(row, "_mapping"):
        return row._mapping.get(key, default)
    return getattr(row, key, default)


def _config(settings: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict((settings or {}).get("option_research") or {})
    return source


def _fingerprint(config: Mapping[str, Any]) -> str:
    encoded = json.dumps(config, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return hashlib.sha256(encoded).hexdigest()[:16]


def _pair(rows, *, spot, symbol, as_of, available_at=None) -> dict[str, Any]:
    pair = {
        "flow": build_option_envelopes(
            rows, spot=spot, source="volume_gamma", symbol=symbol, as_of=as_of,
        ),
        "structural": build_option_envelopes(
            rows, spot=spot, source="oi_gamma", symbol=symbol, as_of=as_of,
        ),
    }
    for envelope in pair.values():
        envelope["available_at"] = available_at
    return pair


def unavailable_research(*, symbol: str = "", as_of: Any = None, config=None, warning: str) -> dict[str, Any]:
    resolved = dict(config or {})
    return {
        "envelopes": {
            "flow": unavailable_option_envelopes(
                source="volume_gamma", symbol=symbol, as_of=as_of, warning=warning,
            ),
            "structural": unavailable_option_envelopes(
                source="oi_gamma", symbol=symbol, as_of=as_of, warning=warning,
            ),
        },
        "envelope_history": [],
        "estimated_pressure": unavailable_estimated_pressure(
            symbol=symbol, as_of=as_of, config=resolved, warning=warning,
        ),
        "exhaustion": unavailable_exhaustion(
            symbol=symbol, as_of=as_of, config=resolved, warning=warning,
        ),
        "exhaustion_events": [],
        "research_config": {**resolved, "fingerprint": _fingerprint(resolved)},
        "research_coverage": {"status": "unavailable", "warnings": [warning]},
    }


def build_option_research(
    conn, snapshot, settings: Mapping[str, Any] | None = None, *, include_history: bool = False,
) -> dict[str, Any]:
    """Calculate research without modifying decisions, signals, or stored rows."""
    symbol = str(_value(snapshot, "symbol", "") or "").upper()
    as_of = _value(snapshot, "timestamp")
    snapshot_id = _value(snapshot, "id", _value(snapshot, "snapshot_id"))
    spot = _value(snapshot, "spot_price", _value(snapshot, "spot"))
    config = _config(settings)
    if not symbol or as_of is None or snapshot_id is None:
        return unavailable_research(
            symbol=symbol, as_of=as_of, config=config,
            warning="A selected snapshot is required for option research.",
        )
    observations = load_session_observations(
        conn,
        symbol=symbol,
        as_of=as_of,
        snapshot_id=int(snapshot_id),
        observation_timezone=str(config.get("observation_timezone", "America/New_York")),
        quote_clock_policy="naive_utc",
    )
    snapshots = observations.get("snapshots") or []
    if not snapshots:
        return unavailable_research(
            symbol=symbol, as_of=as_of, config=config,
            warning="No bounded session observations are available for option research.",
        )

    current = snapshots[-1]
    current_rows = current.get("contracts") or []
    current_spot = current.get("spot") or spot
    envelopes = _pair(
        current_rows,
        spot=current_spot,
        symbol=symbol,
        as_of=current.get("timestamp", as_of),
        available_at=current.get("available_at"),
    )
    envelope_history = []
    history_snapshots = snapshots
    if include_history:
        # TRACE uses one stable state per display minute, matching its heatmap
        # sampling and avoiding repeated sweeps for adaptive sub-minute polls.
        by_minute = {}
        for item in snapshots:
            by_minute[str(item.get("timestamp") or "")[:16]] = item
        history_snapshots = list(by_minute.values())
    else:
        # Cockpit needs current levels. Exhaustion remains causal because this
        # state is unavailable to earlier pressure points rather than backfilled.
        history_snapshots = [current]
    for item in history_snapshots:
        available_at = item.get("available_at")
        item_spot = item.get("spot")
        if available_at is None or item_spot is None:
            continue
        envelope_history.append({
            "snapshot_id": item.get("snapshot_id"),
            "as_of": item.get("timestamp"),
            "available_at": available_at,
            **_pair(
                item.get("contracts") or [],
                spot=item_spot,
                symbol=symbol,
                as_of=item.get("timestamp"),
                available_at=available_at,
            ),
        })

    # Calculators compare one clock domain. Collection timestamps are localized
    # by the reader and provider quote timestamps use the explicit UTC policy.
    pressure_rows = []
    for raw in observations.get("rows") or []:
        row = dict(raw)
        available_at = row.get("available_at")
        if not available_at:
            continue
        for name in ("bid_timestamp", "ask_timestamp", "last_timestamp"):
            row[name] = row.get(f"{name}_utc")
        pressure_rows.append(row)
    pressure_as_of = observations.get("available_at")
    if pressure_as_of:
        estimated_pressure = build_estimated_pressure(
            pressure_rows, symbol=symbol, as_of=pressure_as_of, config=config,
        )
    else:
        estimated_pressure = unavailable_estimated_pressure(
            symbol=symbol, as_of=as_of, config=config,
            warning="Collection clocks could not be normalized for pressure.",
        )

    spot_observations = [
        {
            "snapshot_id": item.get("snapshot_id"),
            "timestamp": item.get("available_at"),
            "available_at": item.get("available_at"),
            "price": item.get("price"),
            "spot_price": item.get("price"),
        }
        for item in observations.get("spot_observations") or []
        if item.get("available_at")
    ]
    if pressure_as_of:
        exhaustion = build_exhaustion_events(
            estimated_pressure,
            spot_observations,
            envelope_history,
            config=config,
            as_of=pressure_as_of,
        )
    else:
        exhaustion = unavailable_exhaustion(
            symbol=symbol, as_of=as_of, config=config,
            warning="Normalized collection time is required for exhaustion research.",
        )

    # Keep the frontend contract explicit while retaining the calculator's
    # diagnostic aliases for evaluator output.
    for event in exhaustion.get("events") or []:
        if "nearest_envelope" not in event:
            event["nearest_envelope"] = event.get("envelope")
    current_event = exhaustion.get("current")
    if isinstance(current_event, dict) and "nearest_envelope" not in current_event:
        current_event["nearest_envelope"] = current_event.get("envelope")

    return {
        "envelopes": envelopes,
        "envelope_history": envelope_history,
        "estimated_pressure": estimated_pressure,
        "exhaustion": exhaustion,
        "exhaustion_events": exhaustion.get("events") or [],
        "research_config": {**config, "fingerprint": _fingerprint(config)},
        "research_coverage": {
            "status": observations.get("status", "unavailable"),
            "coverage": observations.get("coverage") or {},
            "warnings": observations.get("warnings") or [],
        },
    }

