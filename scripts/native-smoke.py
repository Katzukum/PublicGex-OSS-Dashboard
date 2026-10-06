"""Exercise frozen live/demo startup using fresh, isolated data and real HTTP RPC."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import queue
import socket
import subprocess
import tempfile
import threading
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_SYMBOLS = ["SPY", "QQQ", "IWM", "SPX", "NDX"]


def exercise_mode(binary: Path, test_root: Path, *, demo: bool) -> None:
    mode = "demo" if demo else "live"
    with tempfile.TemporaryDirectory(prefix=f"frozen-{mode}-", dir=test_root) as directory:
        data_dir = Path(directory).resolve()
        assert data_dir.is_relative_to(test_root.resolve())
        if not demo:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                integration_port = probe.getsockname()[1]
            (data_dir / "settings.json").write_text(json.dumps({"ninjatrader_port": integration_port}))
        log = open(data_dir / "stderr.log", "w", encoding="utf-8")
        command = [str(binary), "--data-dir", str(data_dir), "--port", "0"]
        if demo:
            command.append("--demo")
        process = subprocess.Popen(command,
                                   stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log,
                                   text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            lines: queue.Queue[str] = queue.Queue()
            threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True).start()
            ready = json.loads(lines.get(timeout=60))
            assert ready["type"] == "ready"
            assert Path(ready["data_dir"]).resolve() == data_dir
            marker = json.loads((data_dir / ".publicgex-workspace.json").read_text(encoding="utf-8"))
            assert marker["application_id"] == "com.publicgex.dashboard.isolated"
            assert marker["mode"] == mode
            assert ready["port"] not in {5005, 5010, 15010}
            opener = build_opener(ProxyHandler({}))

            def rpc(method, arguments=None, token=None):
                body = json.dumps({"method": method, "args": arguments or []}).encode()
                request = Request(f"http://127.0.0.1:{ready['port']}/rpc", data=body,
                                  headers={"Content-Type": "application/json", "Authorization": "Bearer " + (token or ready["token"])})
                with opener.open(request, timeout=30) as response:
                    return json.load(response)["result"]

            try:
                rpc("get_symbols", token="incorrect-token")
                raise AssertionError("Unauthenticated request was accepted")
            except HTTPError as error:
                assert error.code == 401
            settings = rpc("get_settings")
            assert settings["symbols"] == DEFAULT_SYMBOLS, "Frozen default symbol universe differs"
            assert rpc("get_symbols") == DEFAULT_SYMBOLS, "Configured instruments must appear before collection"
            runtime = rpc("get_runtime_info")
            assert runtime["mode"] == mode
            assert runtime["collector"]["running"] is False
            assert runtime["ninjatrader"]["running"] is (not demo)
            assert settings["auto_start_collector"] is True
            assert settings["auto_start_ninjatrader"] is True
            if demo:
                overview = rpc("get_market_overview")
                assert overview.get("schema_version") == 2
                workspace = rpc("get_decision_workspace", ["SPX"])
                assert workspace.get("schema_version") == 2
                positioning = workspace["dashboard"]["positioning"]
                assert positioning["model"] == "oi_activity_v1"
                assert positioning["dealer_direction"] == "unknown"
                assert positioning["strikes"]
                assert positioning["coverage"]["valid_activity_contracts"] > 0
                assert all(row["gross_oi_gex"] >= 0 for row in positioning["strikes"])
                trace = rpc("get_trace_data", ["SPX"])
                assert trace["positioning"]["as_of"] == trace["timestamp"]
                assert rpc("get_trace_dates", ["SPX"])
            else:
                assert settings["weights"] == {"SPY": 0.5, "QQQ": 0.3, "IWM": 0.2}
                expected_basket = {"SPX": 0.45, "NDX": 0.35, "IWM": 0.2}
                assert settings["weights_index_basket"] == expected_basket
                assert settings["weights_whale"] == expected_basket
                assert settings["maximum_risk_dollars"] == 500
                assert settings["fees_per_contract"] == 1.25
                persisted = json.loads((data_dir / "settings.json").read_text(encoding="utf-8"))
                assert persisted["ninjatrader_port"] == settings["ninjatrader_port"]
                assert runtime["startup_results"]["collector"]["status"] == "blocked"
                assert runtime["startup_results"]["ninjatrader"]["status"] == "started"
                with socket.create_connection(("127.0.0.1", integration_port), timeout=2):
                    pass
                assert not (data_dir / "SYNTHETIC_DEMO.json").exists()
                assert rpc("get_trace_dates", ["SPX"]) == []
            assert rpc("shutdown")["ok"]
            process.wait(timeout=20)
            assert process.returncode == 0, f"Frozen service exit code: {process.returncode}"
            assert process.stdout.read() == "", "Only the ready handshake may use stdout"
            if not demo:
                with socket.socket() as connection:
                    connection.settimeout(.5)
                    assert connection.connect_ex(("127.0.0.1", integration_port)) != 0
            print(f"Frozen {mode} passed: five default instruments, isolated workspace, authenticated RPC, startup policy, graceful shutdown.")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()
            log.close()


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sidecar", type=Path)
    args = parser.parse_args()
    binary = args.sidecar or next((root / "src-tauri" / "binaries").glob("publicgex-backend-*.exe"))
    test_root = root / ".test-data"
    test_root.mkdir(exist_ok=True)
    exercise_mode(binary, test_root, demo=False)
    exercise_mode(binary, test_root, demo=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
