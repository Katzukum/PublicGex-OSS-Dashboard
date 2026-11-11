"""Read-only volatility/regime study on this app's stored spot observations."""

from __future__ import annotations

import json
import math
import random
import sqlite3
import sys
from bisect import bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import mean

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
from market_context import stored_timestamp_to_eastern  # noqa: E402
from runtime_paths import data_path  # noqa: E402

HORIZONS = (15, 30)  # minutes
MAX_GAP_SECONDS = 120
BOOTSTRAP_SAMPLES = 1000


def _autocorrelation(returns):
    if len(returns) < 3:
        return None
    x, y = returns[:-1], returns[1:]
    mx, my = mean(x), mean(y)
    numerator = sum((a - mx) * (b - my) for a, b in zip(x, y))
    denominator = math.sqrt(sum((a - mx) ** 2 for a in x) * sum((b - my) ** 2 for b in y))
    return numerator / denominator if denominator else None


def study_series(points):
    """points: (timestamp, spot, net_gex, flip), chronologically ordered."""
    by_day = defaultdict(list)
    for timestamp, spot, net, flip in points:
        if spot and spot > 0:
            by_day[stored_timestamp_to_eastern(timestamp).date().isoformat()].append(
                (timestamp, float(spot), float(net or 0), float(flip or 0)))
    observations = defaultdict(list)
    for day, rows in by_day.items():
        # Last observation per minute is a collector-resolution proxy, never an API bar.
        minute_rows = {}
        for row in rows:
            minute_rows[row[0].replace(second=0, microsecond=0)] = row
        ordered = [minute_rows[key] for key in sorted(minute_rows)]
        for index, (timestamp, spot, net, flip) in enumerate(ordered):
            regime = "positive" if spot > flip > 0 and net > 0 else "negative" if 0 < spot < flip and net < 0 else "transition"
            for horizon in HORIZONS:
                future = [row for row in ordered[index + 1:]
                          if row[0] <= timestamp + timedelta(minutes=horizon)]
                if not future or future[-1][0] < timestamp + timedelta(minutes=horizon):
                    continue
                path = [(timestamp, spot), *[(row[0], row[1]) for row in future]]
                if any((right[0] - left[0]).total_seconds() > MAX_GAP_SECONDS
                       for left, right in zip(path, path[1:])):
                    continue
                returns = [math.log(right[1] / left[1]) for left, right in zip(path, path[1:])]
                volatility = math.sqrt(sum(value * value for value in returns))
                observations[(regime, horizon)].append((day, volatility, _autocorrelation(returns)))
    report = {"source": "collector minute-resampled proxy", "max_gap_seconds": MAX_GAP_SECONDS,
              "sessions": len(by_day), "regimes": {}}
    for regime in ("positive", "negative", "transition"):
        report["regimes"][regime] = {}
        for horizon in HORIZONS:
            samples = observations[(regime, horizon)]
            correlations = [item[2] for item in samples if item[2] is not None]
            report["regimes"][regime][str(horizon)] = {
                "samples": len(samples), "sessions": len({item[0] for item in samples}),
                "mean_forward_realized_volatility": mean(item[1] for item in samples) if samples else None,
                "mean_lag_one_autocorrelation": mean(correlations) if correlations else None}
    return report, observations


def difference_interval(observations, horizon=30):
    positive = observations[("positive", horizon)]
    negative = observations[("negative", horizon)]
    by_group = []
    for samples in (positive, negative):
        days = defaultdict(list)
        for day, volatility, _ in samples:
            days[day].append(volatility)
        by_group.append(days)
    if any(len(days) < 2 for days in by_group):
        return None
    rng = random.Random(42)
    differences = []
    for _ in range(BOOTSTRAP_SAMPLES):
        means = []
        for group in by_group:
            days = list(group)
            sample = [value for _day in days for value in group[rng.choice(days)]]
            means.append(mean(sample))
        differences.append(means[0] - means[1])
    differences.sort()
    return [differences[int(.025 * (len(differences) - 1))], differences[int(.975 * (len(differences) - 1))]]


def main():
    db = data_path("gex_data.db")
    report = {"status": "exploratory; API one-minute bars unverified", "symbols": {}}
    with sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True) as connection:
        has_bars = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='index_minute_bars'").fetchone() is not None
        for symbol in ("SPX", "NDX", "SPY", "QQQ", "IWM"):
            rows = [(datetime.fromisoformat(time), spot, net, flip)
                    for time, spot, net, flip in connection.execute(
                        "SELECT timestamp, spot_price, total_net_gex, flip_strike FROM gex_snapshots "
                        "WHERE symbol = ? ORDER BY timestamp", (symbol,))]
            source = "collector minute-resampled proxy"
            if has_bars and symbol in {"SPX", "NDX"} and rows:
                bar_rows = [(datetime.fromisoformat(time), close)
                            for time, close in connection.execute(
                                "SELECT timestamp, spot_price FROM index_minute_bars WHERE symbol = ? ORDER BY timestamp", (symbol,))]
                snapshot_times = [row[0] for row in rows]
                observed = []
                for timestamp, close in bar_rows:
                    index = bisect_right(snapshot_times, timestamp) - 1
                    if index >= 0 and (timestamp - snapshot_times[index]).total_seconds() <= MAX_GAP_SECONDS:
                        observed.append((timestamp, close, rows[index][2], rows[index][3]))
                if observed:
                    rows = observed
                    source = "Public.com one-minute index bars"
            summary, observations = study_series(rows)
            summary["source"] = source
            summary["positive_minus_negative_30m_interval"] = difference_interval(observations)
            report["symbols"][symbol] = summary
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
