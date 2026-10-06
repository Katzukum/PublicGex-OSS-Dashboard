"""Authenticated loopback JSON RPC sidecar. One ready line; diagnostics only on stderr."""
from __future__ import annotations

import argparse
import atexit
import hmac
import json
import os
from pathlib import Path
import secrets
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DOMAIN_METHODS = (
    "get_symbols", "get_settings", "save_settings", "get_backend_status",
    "get_dashboard_data", "get_decision_workspace", "get_edge_lab",
    "create_journal_entry", "update_journal_entry", "delete_journal_entry",
    "get_journal_entries", "get_weekly_journal_review", "get_trace_dates", "get_trace_data",
    "build_one_off_profile", "get_one_off_profiles", "get_one_off_profile",
    "get_trade_setups", "get_market_overview",
)
MAX_BODY = 1024 * 1024


def main(argv=None):
    parser = argparse.ArgumentParser(description="PublicGexDashboard isolated analysis service")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--demo", action="store_true", help="Seed synthetic data in an empty isolated workspace; never contact the market-data API")
    parser.add_argument("--collector", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if not args.data_dir.is_absolute():
        parser.error("--data-dir must be an absolute path")
    data_dir = args.data_dir.resolve()
    from workspace import WorkspaceLease, inspect_workspace
    try:
        if args.collector:
            inspect_workspace(data_dir, False, collector=True)
            if not os.environ.get("PUBLICGEX_EVENT_TOKEN") or not os.environ.get("PUBLICGEX_EVENT_PORT"):
                raise ValueError("Collectors must be started by their owning application.")
            lease = None
        else:
            lease = WorkspaceLease(data_dir, args.demo)
            atexit.register(lease.close)
    except ValueError as exc:
        parser.error(str(exc))
    os.environ["PUBLICGEX_DATA_DIR"] = str(data_dir)
    os.environ["PUBLICGEX_DEMO"] = "1" if args.demo else "0"
    os.chdir(data_dir)
    handshake_output = sys.stdout
    sys.stdout = sys.stderr

    if args.collector:
        collector_stopped = threading.Event()

        def collector_lifetime():
            try:
                while os.read(sys.stdin.fileno(), 1):
                    pass
            except (OSError, AttributeError):
                pass
            collector_stopped.set()
            # A blocked provider call cannot outlive its owning application.
            # SQLite rolls back an incomplete transaction if this deadline is needed.
            import time
            time.sleep(3)
            os._exit(0)

        threading.Thread(target=collector_lifetime, name="collector-owner", daemon=True).start()
        from publicData import run_loop
        run_loop(stop_event=collector_stopped)
        return

    from serialization import browser_json
    import appy
    from native_runtime import RPC_LOCK, get_runtime
    runtime = get_runtime()
    runtime.initialize()
    if args.demo:
        from demo_data import seed_demo
        seed_demo(appy.engine)
    token = secrets.token_urlsafe(32)
    stopped = threading.Event()

    def stop():
        stopped.set()

    methods = {name: getattr(appy, name) for name in DOMAIN_METHODS}
    methods.update({
        "get_runtime_info": runtime.info,
        "get_events": runtime.get_events,
        "start_collector": runtime.start_collector,
        "stop_collector": runtime.stop_collector,
        "start_ninjatrader": runtime.start_ninjatrader,
        "stop_ninjatrader": runtime.stop_ninjatrader,
    })

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def log_message(self, _format, *_args):
            pass  # Request arguments and credentials must never enter access logs.

        def send_json(self, status, payload):
            body = browser_json(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            self.close_connection = True

        def do_GET(self):
            if self.path != "/health":
                self.send_json(404, {"error": "Not found"})
                return
            self.send_json(200, {"ok": True, "service": "PublicGexDashboard", "schema_version": 2, "demo": runtime.demo})

        def do_POST(self):
            if self.path != "/rpc":
                self.send_json(404, {"error": "Not found"})
                return
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                self.send_json(401, {"error": "Unauthorized"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY:
                    raise ValueError("Request body must be between 1 byte and 1 MiB")
                payload = json.loads(self.rfile.read(size), parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("Non-finite JSON is not supported")))
                if not isinstance(payload, dict) or set(payload) - {"method", "args"}:
                    raise ValueError("Expected an RPC object with method and args")
                method, arguments = payload.get("method"), payload.get("args", [])
                if not isinstance(method, str) or not isinstance(arguments, list):
                    raise ValueError("method must be a string and args must be an array")
                if method == "shutdown":
                    self.send_json(200, {"result": {"ok": True}})
                    stop()
                    return
                if method not in methods:
                    raise ValueError("Unknown RPC method")
                with RPC_LOCK:
                    result = methods[method](*arguments)
                self.send_json(200, {"result": result})
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                self.send_json(400, {"error": str(exc)})
            except Exception:
                self.send_json(500, {"error": "The analysis request failed. Check the workspace state and retry."})

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.daemon_threads = True
    server_thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, name="json-rpc", daemon=True)
    server_thread.start()
    handshake_output.write(browser_json({"type": "ready", "port": server.server_port, "token": token, "data_dir": str(data_dir)}) + "\n")
    handshake_output.flush()

    def watch_stdin():
        try:
            while os.read(sys.stdin.fileno(), 1):
                pass
        except (OSError, AttributeError):
            pass
        stop()

    threading.Thread(target=watch_stdin, name="host-lifetime", daemon=True).start()
    for signal_name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, signal_name):
            signal.signal(getattr(signal, signal_name), lambda *_: stop())
    try:
        stopped.wait()
    finally:
        server.shutdown()
        server.server_close()
        runtime.close()
        lease.close()


if __name__ == "__main__":
    main()
