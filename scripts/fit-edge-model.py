"""Fit shadow scenario probabilities offline; publication always requires explicit review."""

from __future__ import annotations

import json
import random
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from edge_model import FEATURES, family  # noqa: E402
from runtime_paths import data_path  # noqa: E402

MIN_SESSIONS = 40
MIN_OPPORTUNITIES = 200
BOOTSTRAP_SAMPLES = 1000


def _rows(connection):
    query = """SELECT e.session_date, e.symbol, e.emitted_at, e.scenario_type, e.payload_json, o.outcome_label
               FROM signal_events e JOIN signal_outcomes o ON o.signal_event_id = e.id
               WHERE e.engine = 'scenario_v2' AND e.is_opportunity = 1
                 AND o.label_version = 2 AND o.horizon_minutes = 30
               ORDER BY e.session_date, e.emitted_at"""
    result = []
    timeout_count = 0
    next_allowed = {}
    for day, symbol, emitted_at, scenario, payload_json, label in connection.execute(query):
        try:
            timestamp = datetime.fromisoformat(str(emitted_at))
        except ValueError:
            continue
        if timestamp < next_allowed.get(symbol, datetime.min):
            continue
        next_allowed[symbol] = timestamp + timedelta(minutes=30)
        if label == "timeout":
            timeout_count += 1
        if label not in {"target_first", "invalidation_first", "timeout", "pin_held", "pin_broken"}:
            continue
        try:
            features = json.loads(payload_json).get("model_features") or {}
            values = [float(features[name]) for name in FEATURES]
        except (ValueError, TypeError, KeyError):
            continue
        group = family(scenario)
        if group and all(__import__("math").isfinite(value) for value in values):
            result.append((str(day), group, values, int(label in {"target_first", "pin_held"})))
    return result, timeout_count


def _fit(rows):
    from sklearn.linear_model import LogisticRegression
    from sklearn.preprocessing import StandardScaler
    import numpy as np
    x = np.asarray([row[2] for row in rows], dtype=float)
    y = np.asarray([row[3] for row in rows], dtype=int)
    if len(set(y)) < 2:
        return None
    scaler = StandardScaler().fit(x)
    scales = [float(value) if value > 0 else 1.0 for value in scaler.scale_]
    scaled = (x - scaler.mean_) / scales
    model = LogisticRegression(penalty="l2", C=1.0, max_iter=1000, random_state=42).fit(scaled, y)
    return model, scaler.mean_.tolist(), scales


def _interval(predictions):
    by_day = defaultdict(list)
    for day, probability, outcome, base_rate in predictions:
        by_day[day].append((probability, outcome, base_rate))
    days = sorted(by_day)
    if len(days) < 2:
        return None
    rng = random.Random(42)
    estimates = []
    for _ in range(BOOTSTRAP_SAMPLES):
        sample = [point for _day in days for point in by_day[rng.choice(days)]]
        model_error = sum((probability - outcome) ** 2 for probability, outcome, _ in sample) / len(sample)
        base_error = sum((base - outcome) ** 2 for _, outcome, base in sample) / len(sample)
        if base_error > 0:
            estimates.append(1 - model_error / base_error)
    if not estimates:
        return None
    estimates.sort()
    return [estimates[int(.025 * (len(estimates) - 1))], estimates[int(.975 * (len(estimates) - 1))]]


def fit_all(rows, timeout_count: int) -> dict:
    sessions = {row[0] for row in rows}
    report = {"sessions": len(sessions), "opportunities": len(rows), "timeouts_counted_non_target": timeout_count,
              "required_sessions": MIN_SESSIONS, "required_opportunities": MIN_OPPORTUNITIES,
              "families": {}}
    if len(sessions) < MIN_SESSIONS or len(rows) < MIN_OPPORTUNITIES:
        report["status"] = "insufficient_data"
        return report
    for group in ("pin", "fade", "expansion"):
        family_rows = [row for row in rows if row[1] == group]
        days = sorted({row[0] for row in family_rows})
        predictions = []
        # Weekly refits with an expanding, strictly earlier session window.
        for start in range(max(1, len(days) // 2), len(days), 5):
            train = [row for row in family_rows if row[0] < days[start]]
            fitted = _fit(train)
            if fitted is None:
                continue
            model, means, scales = fitted
            test_days = set(days[start:start + 5])
            base_rate = sum(row[3] for row in train) / len(train)
            for row in family_rows:
                if row[0] in test_days:
                    x = [(value - mean) / scale for value, mean, scale in zip(row[2], means, scales)]
                    probability = float(model.predict_proba([x])[0][1])
                    predictions.append((row[0], probability, row[3], base_rate))
        interval = _interval(predictions)
        fitted = _fit(family_rows)
        publish = bool(interval and interval[0] > 0 and len(days) >= 20 and len(predictions) >= 20)
        brier = sum((p - y) ** 2 for _, p, y, _ in predictions) / len(predictions) if predictions else None
        base_brier = sum((base - y) ** 2 for _, _, y, base in predictions) / len(predictions) if predictions else None
        entry = {"sessions": len(days), "opportunities": len(family_rows),
                 "walk_forward_predictions": len(predictions), "skill_interval": interval,
                 "base_rate": sum(row[3] for row in family_rows) / len(family_rows) if family_rows else None,
                 "brier_score": brier, "base_brier_score": base_brier,
                 "brier_skill_score": 1 - brier / base_brier if brier is not None and base_brier else None,
                 "publish": publish, "features": list(FEATURES)}
        if fitted:
            model, means, scales = fitted
            entry.update({"means": means, "scales": scales,
                          "coefficients": model.coef_[0].tolist(), "intercept": float(model.intercept_[0])})
        report["families"][group] = entry
    report["status"] = "review_required"
    return report


def main():
    db = data_path("gex_data.db")
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as connection:
        rows, timeouts = _rows(connection)
    report = fit_all(rows, timeouts)
    print(json.dumps(report, indent=2))
    if report["status"] == "review_required":
        artifact = {"version": 1, "user_reviewed": False, "features": list(FEATURES),
                    "families": report["families"]}
        data_path("edge_model.json").write_text(json.dumps(artifact, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
