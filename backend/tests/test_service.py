from __future__ import annotations

from contextlib import contextmanager
from datetime import date
import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
import threading
import urllib.error
import urllib.request

import pytest

from serialization import browser_json

SERVICE = Path(__file__).resolve().parents[1] / "service.py"


def strict_json(value):
    return json.loads(value, parse_constant=lambda _value: pytest.fail("Invalid non-finite wire JSON"))


class Client:
    def __init__(self, process, ready):
        self.process = process
        self.port = ready["port"]
        self._token = ready["token"]

    def request(self, method, args=None, authorized=True):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["Authorization"] = "Bearer " + self._token
        request = urllib.request.Request(f"http://127.0.0.1:{self.port}/rpc", data=json.dumps({"method": method, "args": args or []}).encode(), headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, strict_json(response.read())

    def call(self, method, args=None):
        status, response = self.request(method, args)
        assert status == 200, f"{method} returned HTTP {status}"
        assert "result" in response, f"{method} returned no result"
        return response["result"]


@contextmanager
def service(data_dir, demo=False):
    if not demo and not (data_dir / "settings.json").exists():
        # Each subprocess owns its own loopback port, including parallel suites.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            integration_port = probe.getsockname()[1]
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "settings.json").write_text(json.dumps({"ninjatrader_port": integration_port}))
    command = [sys.executable, "-B", str(SERVICE), "--data-dir", str(data_dir)]
    if demo:
        command.append("--demo")
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
    try:
        line = lines.get(timeout=40)
        if not line:
            pytest.fail("Sidecar did not become ready: " + process.stderr.read()[-500:])
        ready = strict_json(line)
        assert ready["type"] == "ready"
        assert Path(ready["data_dir"]) == data_dir
        client = Client(process, ready)
        yield client
        if process.poll() is None:
            client.call("shutdown")
        process.wait(timeout=10)
        assert process.returncode == 0, process.stderr.read()[-500:]
        assert process.stdout.read() == "", "Only the ready handshake may be written to stdout"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()


def test_finite_wire_json_handles_domain_values():
    payload = strict_json(browser_json({"values": [float("nan"), float("inf"), -float("inf"), 1.5], "date": date(2026, 9, 30)}))
    assert payload == {"values": [None, None, None, 1.5], "date": "2026-09-30"}


def test_live_service_is_empty_authenticated_and_credential_isolated(tmp_path, monkeypatch):
    # Even inherited variables/ancestor .env values must not enable this workspace.
    monkeypatch.setenv("PUBLIC_API_KEY", "test-only-inherited-placeholder")
    monkeypatch.setenv("PUBLIC_ACCOUNT_ID", "test-only-inherited-placeholder")
    (tmp_path / ".env").write_text("PUBLIC_API_KEY=test-placeholder\nPUBLIC_ACCOUNT_ID=test-placeholder\n")
    data_dir = tmp_path / "isolated"
    with service(data_dir) as client:
        status, _ = client.request("get_settings", authorized=False)
        assert status == 401
        status, _ = client.request("__import__")
        assert status == 400
        with urllib.request.urlopen(f"http://127.0.0.1:{client.port}/health") as response:
            health = strict_json(response.read())
        assert health == {"ok": True, "service": "PublicGexDashboard", "schema_version": 2, "demo": False}
        runtime = client.call("get_runtime_info")
        assert runtime["collector"]["running"] is False
        assert runtime["ninjatrader"]["running"] is True
        assert runtime["startup_results"]["collector"]["status"] == "blocked"
        assert runtime["startup_results"]["ninjatrader"]["status"] == "started"
        with socket.create_connection(("127.0.0.1", runtime["ninjatrader"]["port"]), timeout=2):
            pass
        assert runtime["credentials_configured"] is False
        assert runtime["event_bridge"]["port"] not in {5005, 5010}
        assert client.call("get_symbols") == ["SPY", "QQQ", "IWM", "SPX", "NDX"]
        assert "error" in client.call("get_dashboard_data", ["SPX"])
        assert client.request("start_collector")[0] == 400
        assert client.call("save_settings", [{"theme": "light"}])["ok"] is True
        assert client.call("get_settings")["theme"] == "light"
        assert client.call("get_events") == []
        assert (data_dir / "settings.json").exists()
        assert not (tmp_path / "gex_data.db").exists()


def test_demo_serves_all_analytical_views_and_blocks_live_outputs(tmp_path):
    data_dir = tmp_path / "demo"
    with service(data_dir, demo=True) as client:
        assert client.call("get_runtime_info")["demo"] is True
        assert len(client.call("get_symbols")) == 5
        assert "SYNTHETIC" in client.call("get_backend_status")["run_message"]
        for method, arguments in (("get_dashboard_data", ["SPX"]), ("get_decision_workspace", ["SPX"]),
                                  ("get_market_overview", []), ("get_trace_data", ["SPX"]),
                                  ("get_edge_lab", [{"symbol": "SPX"}]), ("get_trade_setups", ["SPX"])):
            result = client.call(method, arguments)
            assert "error" not in result, method
        assert client.call("get_decision_workspace", ["SPX"])["schema_version"] == 2
        assert len(client.call("get_edge_lab", [{"symbol": "SPX"}])["opportunities"]) > 20
        profile = client.call("get_one_off_profiles")[0]
        assert client.call("get_one_off_profile", [profile["snapshot_id"]])["profile"]
        assert client.call("get_journal_entries")
        assert client.call("get_weekly_journal_review")["entries"]
        assert client.request("start_collector")[0] == 400
        assert client.request("start_ninjatrader")[0] == 400
        assert client.call("build_one_off_profile", ["SPX", "2026-12-18"])["ok"] is False
        assert (data_dir / "SYNTHETIC_DEMO.json").exists()
    result = subprocess.run([sys.executable, "-B", str(SERVICE), "--data-dir", str(data_dir)], input="", capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "separate data directories" in result.stderr


def test_stdin_eof_stops_service_and_private_event_socket(tmp_path):
    with service(tmp_path / "lifetime") as client:
        runtime = client.call("get_runtime_info")
        ports = [runtime["event_bridge"]["port"], runtime["ninjatrader"]["port"]]
        client.process.stdin.close()
        client.process.wait(timeout=10)
        for port in ports:
            with socket.socket() as connection:
                connection.settimeout(0.5)
                assert connection.connect_ex(("127.0.0.1", port)) != 0


def test_collector_stops_on_owner_eof_without_credentials_or_api_calls(tmp_path):
    # Missing workspace credentials stop each collection before SDK creation.
    from workspace import WorkspaceLease
    WorkspaceLease(tmp_path / "collector", False).close()
    environment = os.environ.copy()
    environment.update({"PUBLICGEX_EVENT_TOKEN": "test-owner-token", "PUBLICGEX_EVENT_PORT": "65534"})
    process = subprocess.Popen([sys.executable, "-B", str(SERVICE), "--collector", "--data-dir", str(tmp_path / "collector")],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=environment)
    try:
        process.stdin.close()
        process.wait(timeout=10)
        assert process.returncode == 0
        assert not (tmp_path / "collector" / "gex_data.db").exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()
        process.stderr.close()


def test_paths_cannot_escape_private_data_root(tmp_path):
    from runtime_paths import require_data_path

    with pytest.raises(ValueError, match="inside"):
        require_data_path("../../escaped.db")


def test_unowned_database_and_legacy_source_folder_are_untouched(tmp_path):
    for name, source_folder in (("unknown-database", False), ("legacy-application", True)):
        directory = tmp_path / name
        directory.mkdir()
        database = directory / "gex_data.db"
        before = b"Untouched legacy database fixture"
        database.write_bytes(before)
        if source_folder:
            (directory / "publicData.py").write_text("# legacy application fixture")
        result = subprocess.run([sys.executable, "-B", str(SERVICE), "--data-dir", str(directory)], input="", capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert result.stdout == ""
        assert database.read_bytes() == before
        assert not (directory / ".publicgex-workspace.json").exists()
        assert not (directory / ".publicgex-instance.lock").exists()


def test_second_owner_is_rejected_and_closed_workspace_reopens(tmp_path):
    directory = tmp_path / "exclusive"
    with service(directory) as first:
        result = subprocess.run([sys.executable, "-B", str(SERVICE), "--data-dir", str(directory)], input="", capture_output=True, text=True, timeout=10)
        assert result.returncode != 0
        assert "already open" in result.stderr
        assert result.stdout == ""
        assert first.call("get_runtime_info")["mode"] == "live"
    with service(directory) as reopened:
        assert reopened.call("get_symbols") == ["SPY", "QQQ", "IWM", "SPX", "NDX"]
