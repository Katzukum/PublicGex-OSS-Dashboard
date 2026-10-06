"""Refuse legacy folders and serialize process ownership before opening any database."""
from __future__ import annotations

import json
import os
from pathlib import Path

MARKER_NAME = ".publicgex-workspace.json"
APPLICATION_ID = "com.publicgex.dashboard.isolated"


def inspect_workspace(directory: Path, demo: bool, *, collector: bool = False):
    directory = directory.resolve()
    for name in (MARKER_NAME, ".publicgex-instance.lock"):
        if not (directory / name).resolve().is_relative_to(directory):
            raise ValueError("Workspace ownership files must stay inside the data directory.")
    if any((directory / name).exists() for name in ("appy.py", "publicData.py", "src-tauri", ".git")):
        raise ValueError("Choose a dedicated data directory, not an existing application or source workspace.")
    marker = directory / MARKER_NAME
    ownership = None
    if marker.exists():
        try:
            ownership = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ValueError("The workspace ownership marker is invalid; no database was opened.") from None
        if ownership.get("application_id") != APPLICATION_ID or ownership.get("version") != 1:
            raise ValueError("This directory is not owned by this isolated application.")
        if ownership.get("mode") != ("demo" if demo else "live"):
            raise ValueError("Demo and live workspaces must use separate data directories.")
    elif collector:
        raise ValueError("A collector requires an initialized workspace owned by its application.")
    elif directory.exists():
        existing_databases = [path for path in directory.iterdir() if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}]
        if existing_databases:
            raise ValueError("An uninitialized directory already contains a database. Start once with an empty new data directory, stop the app, then explicitly copy history into that owned workspace.")
    synthetic = (directory / "SYNTHETIC_DEMO.json").exists()
    if synthetic and not demo:
        raise ValueError("This workspace contains synthetic demo data. Use --demo or a separate empty live data directory.")
    return ownership


class WorkspaceLease:
    """The operating system releases the exclusive byte/file lock after a crash."""

    def __init__(self, directory: Path, demo: bool):
        inspect_workspace(directory, demo)
        directory.mkdir(parents=True, exist_ok=True)
        self.handle = (directory / ".publicgex-instance.lock").open("a+b")
        self.closed = False
        try:
            self.handle.seek(0, os.SEEK_END)
            if self.handle.tell() == 0:
                self.handle.write(b"0")
                self.handle.flush()
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.closed = True
            raise ValueError("This workspace is already open in another app instance. Close that instance or choose another data directory.") from None
        try:
            ownership = inspect_workspace(directory, demo)
            if ownership is None:
                (directory / MARKER_NAME).write_text(json.dumps({"application_id": APPLICATION_ID, "version": 1, "mode": "demo" if demo else "live"}, indent=2), encoding="utf-8")
        except Exception:
            self.close()
            raise

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            self.handle.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
