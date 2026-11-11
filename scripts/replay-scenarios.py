"""Read-only historical replay of scenario v1 and shadow v2; prints JSON.

The default five-minute sample is exploratory. Use --stride-minutes 0 for every
stored snapshot before assessing the Phase 2 frequency gate.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, text


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BACKEND = PROJECT_ROOT / "backend"


def default_database() -> Path:
    local = os.environ.get("LOCALAPPDATA")
    return (Path(local) / "com.publicgex.dashboard" / "data" / "gex_data.db") if local else PROJECT_ROOT / "data" / "gex_data.db"


def allowed_database(path: Path) -> Path:
    path = path.expanduser().resolve(strict=True)
    roots = [PROJECT_ROOT / "data", PROJECT_ROOT / ".demo-data"]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local) / "com.publicgex.dashboard" / "data")
    if not any(path.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("Replay database must be in this app's isolated data directory")
    return path


def sample_rows(rows, stride_minutes: int, maximum: int):
    last_by_session = {}
    selected = []
    invalid = []
    for row in rows:
        try:
            stamp = datetime.fromisoformat(str(row.timestamp))
        except ValueError:
            invalid.append({"snapshot_id": row.id, "symbol": row.symbol, "reason": "invalid stored timestamp"})
            continue
        key = (row.symbol, stamp.date())
        previous = last_by_session.get(key)
        if stride_minutes and previous is not None and (stamp - previous).total_seconds() < stride_minutes * 60:
            continue
        last_by_session[key] = stamp
        selected.append(row)
        if maximum and len(selected) >= maximum:
            break
    return selected, invalid


def market_score_for_symbol(overview: dict) -> float:
    traders = overview.get("compass_traders") or overview.get("compass") or {}
    basket = overview.get("index_basket") or overview.get("compass_whale") or {}
    trader_quality = (traders.get("data_quality") or {}).get("score", traders.get("confidence", 0)) or 0
    basket_quality = (basket.get("data_quality") or {}).get("score", basket.get("confidence", 0)) or 0
    total = trader_quality + basket_quality
    return (((traders.get("y_score", 0) or 0) * trader_quality
             + (basket.get("y_score", 0) or 0) * basket_quality) / total) if total else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=default_database())
    parser.add_argument("--stride-minutes", type=int, default=5, help="0 replays every snapshot")
    parser.add_argument("--max-snapshots", type=int, default=0, help="0 means no limit")
    parser.add_argument("--session", help="Replay only snapshots with this stored YYYY-MM-DD session date")
    args = parser.parse_args()
    if args.stride_minutes < 0 or args.max_snapshots < 0:
        parser.error("stride and maximum must be nonnegative")
    db_path = allowed_database(args.db)
    os.environ["PUBLICGEX_DATA_DIR"] = str(db_path.parent)
    sys.path.insert(0, str(BACKEND))

    import appy
    from market_context import build_market_context, stored_timestamp_to_eastern
    from scenario_engine import build_scenario_workspace
    from scenario_engine_v2 import build_scenario_workspace_v2

    db_engine = create_engine("sqlite:///file:" + db_path.as_posix() + "?mode=ro&uri=true")
    appy.engine = db_engine
    appy.DB_SCHEMA_CURRENT = True
    with db_engine.connect() as connection:
        all_rows = connection.execute(text(
            "SELECT id, symbol, timestamp FROM gex_snapshots ORDER BY timestamp, id"
        )).fetchall()
    rows, invalid_timestamps = sample_rows(all_rows, args.stride_minutes, args.max_snapshots)
    if args.session:
        try:
            datetime.strptime(args.session, "%Y-%m-%d")
        except ValueError:
            parser.error("session must be YYYY-MM-DD")
        rows = [row for row in rows if str(row.timestamp).startswith(args.session)]
    counts = defaultdict(Counter)
    eligible_transitions = defaultdict(Counter)
    previous_state = {}
    errors = []
    try:
        for row in rows:
            try:
                # appy prints its own diagnostics on errors. Capture them so stdout stays JSON.
                with contextlib.redirect_stdout(io.StringIO()) as diagnostics:
                    dashboard = appy._dashboard_data_from_engine(db_engine, row.symbol, True, snapshot_id=row.id, include_research=False)
                    overview = appy.get_market_overview(as_of=row.timestamp, include_edge_stats=False, include_levels=False)
                if dashboard.get("error") or overview.get("error"):
                    raise RuntimeError(dashboard.get("error") or overview.get("error") or diagnostics.getvalue())
                snapshot = dashboard["snapshot"]
                profile = dashboard.get("profile") or []
                history = dashboard.get("history") or []
                component = next((item for item in overview.get("components", []) if item.get("symbol") == row.symbol), {})
                quality = component.get("data_quality") or {"score": component.get("confidence", 0), "warnings": component.get("warnings", [])}
                at = stored_timestamp_to_eastern(datetime.fromisoformat(str(row.timestamp)))
                context = build_market_context(row.symbol, snapshot.get("spot_price"), profile, history, None, now=at)
                score = market_score_for_symbol(overview)
                versions = {
                    "scenario_v1": build_scenario_workspace(snapshot, profile, context, quality,
                                                            market_score=score, index_basket=overview.get("index_basket")),
                    "scenario_v2": build_scenario_workspace_v2(snapshot, profile, context, quality,
                                                                gamma_sweep=dashboard.get("gamma_sweep"), spot_history=history),
                }
                session = at.date().isoformat()
                for engine_name, workspace in versions.items():
                    active = workspace["active_scenario"]
                    decision = workspace.get("decision") or {}
                    group = (engine_name, row.symbol, session)
                    kind = active["scenario_type"]
                    counts[group][kind] += 1
                    state = (kind, active.get("bias"), decision.get("target"), decision.get("invalidation"),
                             active.get("pin_band_low"), active.get("pin_band_high"))
                    if active.get("eligible") and previous_state.get(group) != state:
                        eligible_transitions[group][kind] += 1
                    previous_state[group] = state
            except Exception as exc:
                errors.append({"snapshot_id": row.id, "symbol": row.symbol, "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
                if len(errors) >= 20:
                    break
    finally:
        appy.engine = None
        db_engine.dispose()

    groups = [
        {"engine": engine_name, "symbol": symbol, "session": session,
         "snapshots": sum(kinds.values()), "scenario_counts": dict(kinds),
         "eligible_state_transitions": dict(eligible_transitions.get((engine_name, symbol, session), {}))}
        for (engine_name, symbol, session), kinds in sorted(counts.items())
    ]
    output = {"database": db_path.name, "data_scope": "isolated_app_directory", "read_only": True, "stride_minutes": args.stride_minutes,
              "session_filter": args.session,
              "max_snapshots": args.max_snapshots, "available_snapshots": len(all_rows),
              "selected_snapshots": len(rows), "processed_snapshots": sum(item["snapshots"] for item in groups) // 2,
              "groups": groups, "errors": errors, "invalid_timestamps": invalid_timestamps[:20],
              "invalid_timestamp_count": len(invalid_timestamps),
              "limitations": ["Historical event calendars are not stored for all snapshots; replay uses UNAVAILABLE event state.",
                              "Historical decisions use the current saved composition weights.",
                              "Any nonzero stride samples snapshots and cannot establish the full frequency gate."]}
    print(json.dumps(output, indent=2, default=str))
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
