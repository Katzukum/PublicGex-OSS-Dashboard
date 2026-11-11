"""Replay every isolated-app snapshot by session in parallel; print one JSON report."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPLAY = ROOT / "scripts" / "replay-scenarios.py"


def allowed_database(path: Path) -> Path:
    path = path.expanduser().resolve(strict=True)
    roots = [ROOT / "data", ROOT / ".demo-data"]
    if os.environ.get("LOCALAPPDATA"):
        roots.append(Path(os.environ["LOCALAPPDATA"]) / "com.publicgex.dashboard" / "data")
    if not any(path.is_relative_to(root.resolve()) for root in roots):
        raise ValueError("Replay database must be in this app's isolated data directory")
    return path


def sessions_in(path: Path) -> list[str]:
    with sqlite3.connect("file:" + path.as_posix() + "?mode=ro", uri=True) as connection:
        values = connection.execute("SELECT DISTINCT substr(timestamp,1,10) FROM gex_snapshots ORDER BY 1")
        result = []
        for (value,) in values:
            try:
                datetime.strptime(str(value), "%Y-%m-%d")
            except ValueError:
                continue
            result.append(value)
        return result


def replay_session(path: Path, session: str, stride: int) -> dict:
    command = [sys.executable, str(REPLAY), "--db", str(path),
               "--stride-minutes", str(stride), "--session", session]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    if not result.stdout or result.returncode not in (0, 1):
        raise RuntimeError(f"{session} replay failed ({result.returncode}): {result.stderr[-500:]}")
    return json.loads(result.stdout)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    local = os.environ.get("LOCALAPPDATA")
    default = (Path(local) / "com.publicgex.dashboard" / "data" / "gex_data.db") if local else ROOT / "data" / "gex_data.db"
    parser.add_argument("--db", type=Path, default=default)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--stride-minutes", type=int, default=0)
    args = parser.parse_args()
    if args.workers < 1 or args.stride_minutes < 0:
        parser.error("workers must be positive and stride nonnegative")
    path = allowed_database(args.db)
    sessions = sessions_in(path)
    if not sessions:
        parser.error("no valid sessions in database")
    pieces = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(replay_session, path, day, args.stride_minutes): day for day in sessions}
        for future in as_completed(futures):
            piece = future.result()
            pieces.append(piece)
            print(f"Replayed {futures[future]}: {piece['processed_snapshots']} snapshots", file=sys.stderr, flush=True)
    first = pieces[0]
    invalid = {item["snapshot_id"]: item for piece in pieces for item in piece["invalid_timestamps"]}
    report = {key: first[key] for key in ("database", "data_scope", "read_only", "stride_minutes", "available_snapshots", "limitations")}
    report.update({"session_filter": None, "parallel_sessions": len(sessions), "worker_count": args.workers,
                   "max_snapshots": 0, "selected_snapshots": sum(p["selected_snapshots"] for p in pieces),
                   "processed_snapshots": sum(p["processed_snapshots"] for p in pieces),
                   "groups": sorted((g for p in pieces for g in p["groups"]),
                                    key=lambda g: (g["engine"], g["symbol"], g["session"])),
                   "errors": [e for p in pieces for e in p["errors"]],
                   "invalid_timestamps": list(invalid.values())[:20],
                   "invalid_timestamp_count": len(invalid)})
    print(json.dumps(report, indent=2))
    return 1 if report["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
