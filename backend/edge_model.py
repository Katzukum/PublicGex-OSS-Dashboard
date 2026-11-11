"""Pure-Python application of an offline, explicitly approved probability model."""

from __future__ import annotations

import json
import math
from pathlib import Path

from runtime_paths import data_path

FEATURES = ("regime", "zero_distance_implied", "wall_distance_implied",
            "range_consumed", "minutes_to_close")
MODEL_PATH = data_path("edge_model.json")


def family(scenario: str | None) -> str | None:
    value = str(scenario or "").upper()
    if "PIN" in value:
        return "pin"
    if "FADE" in value:
        return "fade"
    if "EXPANSION" in value:
        return "expansion"
    return None


def signal_features(workspace: dict) -> dict:
    """Read only fields frozen when the signal is emitted."""
    from market_context import stored_timestamp_to_eastern
    from signal_performance import parse_datetime

    levels = workspace.get("levels") or {}
    context = workspace.get("market_context") or {}
    snapshot = (workspace.get("dashboard") or {}).get("snapshot") or {}
    spot = snapshot.get("spot_price")
    move = levels.get("implied_move")
    zero = levels.get("zero_gamma")
    walls = [value for value in (levels.get("call_wall"), levels.get("put_wall")) if value is not None]
    timestamp = parse_datetime(workspace.get("as_of"))
    eastern = stored_timestamp_to_eastern(timestamp) if timestamp else None
    minutes = (16 * 60 - eastern.hour * 60 - eastern.minute) if eastern else None
    regime = {"positive": 1.0, "transition": 0.0, "negative": -1.0}.get(
        str(workspace.get("regime") or "").lower())
    return {"regime": regime,
            "zero_distance_implied": (spot - zero) / move if spot is not None and zero is not None and move and move > 0 else None,
            "wall_distance_implied": min(abs(spot - wall) for wall in walls) / move if spot is not None and walls and move and move > 0 else None,
            "range_consumed": context.get("range_consumed"),
            "minutes_to_close": minutes}


def predict(features: dict, scenario: str, path: Path = MODEL_PATH) -> tuple[float | None, str | None]:
    group = family(scenario)
    if group is None:
        return None, "scenario family unavailable"
    try:
        artifact = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError):
        return None, "approved coefficient file unavailable"
    model = (artifact.get("families") or {}).get(group) or {}
    if not artifact.get("user_reviewed") or not model.get("publish"):
        return None, "probability model has not passed publication review"
    if tuple(model.get("features") or ()) != FEATURES:
        return None, "model feature contract mismatch"
    try:
        values = [float(features[name]) for name in FEATURES]
        means = [float(value) for value in model["means"]]
        scales = [float(value) for value in model["scales"]]
        weights = [float(value) for value in model["coefficients"]]
        intercept = float(model["intercept"])
        if not all(map(math.isfinite, [*values, *means, *scales, *weights, intercept])):
            raise ValueError("nonfinite coefficient")
        if len(means) != len(FEATURES) or len(scales) != len(FEATURES) or len(weights) != len(FEATURES) or min(scales) <= 0:
            raise ValueError("invalid coefficient shape")
    except (KeyError, ValueError, TypeError, ZeroDivisionError):
        return None, "missing or invalid signal feature or coefficient"
    logit = intercept + sum(weight * (value - mean) / scale
                            for weight, value, mean, scale in zip(weights, values, means, scales))
    probability = 1 / (1 + math.exp(-max(-40.0, min(40.0, logit))))
    return probability, None
