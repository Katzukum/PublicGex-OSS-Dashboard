"""Project the selected scenario engine into the existing decision contracts."""

from __future__ import annotations


def selected_scenario(v1: dict, v2: dict, engine: str) -> dict:
    if engine == "scenario_v1":
        return {**v1, "engine": engine, "decision": {**v1["decision"], "engine": engine}}
    active = v2["active_scenario"]
    v1_decision = v1["decision"]
    decision = {
        **v1_decision,
        **v2["decision"],
        "engine": engine,
        "scenario_id": active["scenario_id"],
        "context": "; ".join(dict.fromkeys([*active.get("reasons", []), *active.get("warnings", [])])),
    }
    return {
        "engine": engine,
        "active_scenario": active,
        "scenarios": [active],
        "eligibility": {**v2["eligibility"], "event_state": (v2.get("market_context") or {}).get("event_risk", {}).get("state")},
        "decision": decision,
    }


def dashboard_from_scenario(dashboard: dict, workspace: dict) -> dict:
    """Keep the legacy NinjaTrader key set while replacing decision values."""
    if workspace.get("decision_engine") != "scenario_v2":
        return dashboard
    if workspace.get("error"):
        return {**dashboard, "dashboard_bias": "WAIT", "dashboard_bias_score": 0.0,
                "dashboard_target": None, "dashboard_invalidation": None,
                "dashboard_context": f"Scenario v2 unavailable: {workspace['error']}"}
    decision = workspace.get("decision") or {}
    active = workspace.get("active_scenario") or {}
    bias = decision.get("bias")
    dashboard = dict(dashboard)
    dashboard["dashboard_bias"] = "CALL BIAS" if bias == "CALL" else "PUT BIAS" if bias == "PUT" else "WAIT"
    dashboard["dashboard_bias_score"] = 0.0  # Version 2 has no calibrated directional score.
    dashboard["dashboard_target"] = decision.get("target")
    dashboard["dashboard_invalidation"] = decision.get("invalidation")
    context = f"Scenario v2: {active.get('scenario_type') or 'NO_TRADE'}. {decision.get('context') or ''}".strip()
    if active.get("scenario_type") == "PIN":
        context += f" Pin band {active.get('pin_band_low')} to {active.get('pin_band_high')}."
    dashboard["dashboard_context"] = context
    return dashboard
