import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent


def test_import_has_no_runtime_side_effects(tmp_path):
    probe = r"""
import pathlib
import socket
import sys
import threading

project_root = pathlib.Path(sys.argv[1])
work_dir = pathlib.Path(sys.argv[2])
sys.path.insert(0, str(project_root))

import models

def forbidden_database(*args, **kwargs):
    raise AssertionError("database initialized during import")

original_bind = socket.socket.bind
original_start = threading.Thread.start

def forbidden_bind(self, *args, **kwargs):
    raise AssertionError("socket bound during import")

def forbidden_start(self, *args, **kwargs):
    raise AssertionError("thread started during import")

models.initialize_database = forbidden_database
socket.socket.bind = forbidden_bind
threading.Thread.start = forbidden_start

import appy

assert appy.engine is None
assert appy.event_thread is None
assert appy._runtime_initialized is False
assert not (work_dir / "gex_data.db").exists()

socket.socket.bind = original_bind
threading.Thread.start = original_start
"""
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", probe, str(PROJECT_ROOT), str(tmp_path)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_runtime_initialization_and_shutdown_are_idempotent(monkeypatch):
    import appy
    import ninjatrader_broadcaster

    calls = {"database": 0, "start_nt": 0, "stop_nt": 0}

    class FakeEngine:
        def __init__(self):
            self.dispose_calls = 0

        def dispose(self):
            self.dispose_calls += 1

    class FakeThread:
        def __init__(self, *args, **kwargs):
            self.started = False
            self.join_calls = 0

        def start(self):
            self.started = True

        def is_alive(self):
            return self.started

        def join(self, timeout=None):
            self.join_calls += 1
            self.started = False

    fake_engine = FakeEngine()

    def initialize_database(*args, **kwargs):
        calls["database"] += 1
        return fake_engine

    def start_nt(*args, **kwargs):
        calls["start_nt"] += 1

    def stop_nt(*args, **kwargs):
        calls["stop_nt"] += 1

    monkeypatch.setattr(appy, "initialize_database", initialize_database)
    monkeypatch.setattr(appy, "schema_is_current", lambda: True)
    monkeypatch.setattr(appy.threading, "Thread", FakeThread)
    monkeypatch.setattr(ninjatrader_broadcaster, "start_server", start_nt)
    monkeypatch.setattr(ninjatrader_broadcaster, "stop_server", stop_nt, raising=False)

    appy.engine = None
    appy.event_thread = None
    appy._runtime_initialized = False

    assert appy.initialize_runtime() is fake_engine
    assert appy.initialize_runtime() is fake_engine
    assert calls == {"database": 1, "start_nt": 1, "stop_nt": 0}

    appy.shutdown_runtime()
    appy.shutdown_runtime()

    assert calls == {"database": 1, "start_nt": 1, "stop_nt": 2}
    assert fake_engine.dispose_calls == 1
    assert appy.engine is None
    assert appy.event_thread is None
    assert appy._runtime_initialized is False
