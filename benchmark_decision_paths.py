"""Benchmark decision-workspace and TRACE queries on a disposable DB copy."""

import argparse
import json
import statistics
import time
from pathlib import Path

import appy
from models import initialize_database, schema_is_current


def percentile(values, percentile_value):
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile_value))))
    return ordered[index]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--symbol", default="SPX")
    parser.add_argument("--runs", type=int, default=20)
    args = parser.parse_args()

    engine = initialize_database(reset_old_schema=False, db_path=args.database)
    appy.engine = engine
    from signal_performance import configure_engine
    configure_engine(engine)
    appy.DB_SCHEMA_CURRENT = schema_is_current(args.database)
    appy._clear_overview_cache()
    try:
        cold_started = time.perf_counter()
        cold = appy.get_decision_workspace(args.symbol)
        cold_seconds = time.perf_counter() - cold_started
        if cold.get("error"):
            raise RuntimeError(cold["error"])

        workspace_times = []
        for _ in range(max(1, args.runs)):
            started = time.perf_counter()
            appy.get_decision_workspace(args.symbol)
            workspace_times.append(time.perf_counter() - started)

        session_date = appy.get_trace_dates(args.symbol, 1)[0]
        trace_started = time.perf_counter()
        trace = appy.get_trace_data(args.symbol, 390, session_date)
        trace_seconds = time.perf_counter() - trace_started
        if trace.get("error"):
            raise RuntimeError(trace["error"])
        trace_cached_started = time.perf_counter()
        appy.get_trace_data(args.symbol, 390, session_date)
        trace_cached_seconds = time.perf_counter() - trace_cached_started

        print(json.dumps({
            "database_gb": round(args.database.stat().st_size / (1024 ** 3), 3),
            "symbol": args.symbol,
            "session_date": session_date,
            "workspace_cold_ms": round(cold_seconds * 1000, 2),
            "workspace_cached_p50_ms": round(statistics.median(workspace_times) * 1000, 2),
            "workspace_cached_p95_ms": round(percentile(workspace_times, 0.95) * 1000, 2),
            "trace_session_ms": round(trace_seconds * 1000, 2),
            "trace_cached_ms": round(trace_cached_seconds * 1000, 2),
            "trace_cells": trace.get("range", {}).get("cells", 0),
            "targets": {"workspace_cached_p95_ms": 500, "trace_session_ms": 2000},
        }, indent=2))
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
