"""Own only this application's collector, private event socket, and database."""
from __future__ import annotations

import hmac
import json
import os
import queue
import secrets
import socket
import subprocess
import sys
import threading
from datetime import datetime

from dotenv import dotenv_values

from runtime_paths import DATA_DIR, SOURCE_ROOT, data_path

RPC_LOCK = threading.RLock()
_runtime = None


class NativeRuntime:
    def __init__(self):
        self.demo = os.environ.get("PUBLICGEX_DEMO") == "1"
        self.initialized = False
        self.closed = False
        self.collector = None
        self.event_socket = None
        self.event_port = None
        self.event_token = secrets.token_urlsafe(32)
        self.event_thread = None
        self.stop_event = threading.Event()
        self.ninjatrader_port = None
        self.settings_initialization = None
        self.startup_results = {}
        self.startup_attempted = False

    def initialize(self):
        import appy
        from models import initialize_database, schema_is_current
        from signal_performance import configure_engine

        with RPC_LOCK:
            if self.initialized:
                return appy.engine
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            from app_settings import initialize_settings
            self.settings_initialization = initialize_settings()
            appy.engine = initialize_database(reset_old_schema=False)
            appy.DB_SCHEMA_CURRENT = schema_is_current()
            configure_engine(appy.engine)
            appy._clear_overview_cache()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.bind(("127.0.0.1", 0))
            listener.listen(8)
            listener.settimeout(0.25)
            self.event_socket = listener
            self.event_port = listener.getsockname()[1]
            self.event_thread = threading.Thread(target=self._events, name="private-collector-events", daemon=True)
            appy.event_thread = self.event_thread
            self.initialized = True
            self.event_thread.start()
            self.start_integrations()
            return appy.engine

    def start_integrations(self):
        """Start this workspace's live integrations once; keep setup failures visible."""
        if self.startup_attempted:
            return
        self.startup_attempted = True
        from app_settings import read_settings
        settings = read_settings()
        for name, enabled in (("ninjatrader", settings["auto_start_ninjatrader"]),
                              ("collector", settings["auto_start_collector"])):
            if self.demo or not enabled:
                self.startup_results[name] = {"status": "disabled", "message":
                    "Live integrations are disabled in demo mode." if self.demo else "Automatic startup is disabled in Settings."}
                continue
            if name == "collector" and not self.credentials_configured():
                self.startup_results[name] = {"status": "blocked", "message":
                    "Add PUBLIC_API_KEY and PUBLIC_ACCOUNT_ID to this workspace's .env file, then start the collector or reopen the app."}
                continue
            try:
                result = self.start_ninjatrader(settings["ninjatrader_port"]) if name == "ninjatrader" else self.start_collector()
                self.startup_results[name] = {"status": "started", "message": result["message"]}
            except Exception as error:
                # Neither a busy integration port nor a provider setup failure
                # should prevent the user opening Settings to correct it.
                self.startup_results[name] = {"status": "error", "message": str(error)}

    def _events(self):
        import appy

        while not self.stop_event.is_set():
            try:
                client, _ = self.event_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                with client:
                    client.settimeout(1)
                    parts = []
                    size = 0
                    while size <= 1024 * 1024:
                        part = client.recv(65536)
                        if not part:
                            break
                        parts.append(part)
                        size += len(part)
                    if size > 1024 * 1024:
                        continue
                    packet = json.loads(b"".join(parts))
                if not isinstance(packet, dict) or not hmac.compare_digest(str(packet.get("token", "")), self.event_token):
                    continue
                message = packet.get("event")
                if not isinstance(message, dict) or not isinstance(message.get("type"), str):
                    continue
                with RPC_LOCK:
                    if self.stop_event.is_set():
                        break
                    with appy._event_health_lock:
                        appy._last_event_at = datetime.now()
                        appy._last_event_type = message["type"]
                        appy._event_count += 1
                    appy._clear_overview_cache()
                    if message["type"] == "MARKET_UPDATE":
                        from ninjatrader_broadcaster import broadcaster, build_regime_payload
                        from decision_alerts import recent_alerts
                        from models import get_session_factory
                        overview = appy.get_market_overview()
                        if not overview.get("error"):
                            Session = get_session_factory(appy.engine)
                            with Session() as session:
                                overview["alerts"] = recent_alerts(session)
                            message["data"] = overview
                            # Signal calibration is a domain operation, independent of
                            # whether a trading-chart client opted into network output.
                            payload = build_regime_payload(overview)
                            if payload and self.ninjatrader_port:
                                broadcaster.broadcast(payload)
                    message["received_at"] = datetime.now().isoformat()
                    try:
                        appy._frontend_events.put_nowait(message)
                    except queue.Full:
                        appy._frontend_events.get_nowait()
                        appy._frontend_events.put_nowait(message)
            except (OSError, ValueError, TypeError):
                continue
            except Exception:
                print("Private collector event processing failed", file=sys.stderr)

    def credentials_configured(self):
        values = dotenv_values(data_path(".env"))
        return bool((values.get("PUBLIC_API_KEY") or "").strip() and (values.get("PUBLIC_ACCOUNT_ID") or "").strip())

    def require_live_credentials(self):
        if self.demo:
            raise ValueError("Demo mode uses synthetic data and cannot contact the market-data API. Open a separate live workspace to collect data.")
        if not self.credentials_configured():
            raise ValueError("Add PUBLIC_API_KEY and PUBLIC_ACCOUNT_ID to this workspace's .env file before collecting data.")

    def info(self):
        from ninjatrader_broadcaster import broadcaster

        running = bool(self.collector and self.collector.poll() is None)
        if self.collector and not running and self.startup_results.get("collector", {}).get("status") == "started":
            self.startup_results["collector"] = {"status": "error", "message":
                "Collector exited. Check this workspace's logs and credentials, then restart it in Settings."}
        return {
            "schema_version": 2,
            "mode": "demo" if self.demo else "live",
            "demo": self.demo,
            "data_dir": str(DATA_DIR),
            "credentials_path": str(data_path(".env")),
            "credentials_configured": self.credentials_configured(),
            "collector": {"running": running, "pid": self.collector.pid if running else None},
            "ninjatrader": {"running": broadcaster.running, "port": self.ninjatrader_port if broadcaster.running else None},
            "event_bridge": {"running": bool(self.event_thread and self.event_thread.is_alive()), "port": self.event_port},
            "python_version": sys.version.split()[0],
            "settings_initialization": self.settings_initialization,
            "startup_results": self.startup_results,
        }

    def start_collector(self):
        self.require_live_credentials()
        if self.collector and self.collector.poll() is None:
            return {"ok": True, "message": "Collector is already running.", "runtime": self.info()}
        if getattr(sys, "frozen", False):
            command = [sys.executable, "--collector", "--data-dir", str(DATA_DIR)]
        else:
            command = [sys.executable, str(SOURCE_ROOT / "service.py"), "--collector", "--data-dir", str(DATA_DIR)]
        environment = os.environ.copy()
        environment.update({"PUBLICGEX_DATA_DIR": str(DATA_DIR), "PUBLICGEX_EVENT_PORT": str(self.event_port), "PUBLICGEX_EVENT_TOKEN": self.event_token})
        self.collector = subprocess.Popen(command, cwd=DATA_DIR, env=environment, stdin=subprocess.PIPE,
                                          stdout=sys.stderr, stderr=sys.stderr,
                                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        import appy
        appy.collector_process = self.collector
        self.startup_results["collector"] = {"status": "started", "message": "Collector started in this workspace."}
        return {"ok": True, "message": "Collector started in this workspace.", "runtime": self.info()}

    def stop_collector(self):
        if self.collector and self.collector.poll() is None:
            if self.collector.stdin:
                self.collector.stdin.close()
            try:
                self.collector.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.collector.terminate()
                self.collector.wait(timeout=5)
        self.startup_results["collector"] = {"status": "stopped", "message": "Collector stopped."}
        return {"ok": True, "message": "Collector stopped.", "runtime": self.info()}

    def start_ninjatrader(self, port=5010):
        from ninjatrader_broadcaster import broadcaster, start_server

        if self.demo:
            raise ValueError("NinjaTrader is disabled in demo mode so synthetic levels cannot reach trading charts.")
        port = int(port)
        if not 1024 <= port <= 65535 or port == 5005:
            raise ValueError("Choose a port from 1024 to 65535, excluding collector event port 5005.")
        if broadcaster.running and self.ninjatrader_port != port:
            raise ValueError("Stop the current NinjaTrader bridge before changing its port.")
        if not start_server(port, "127.0.0.1"):
            raise ValueError(f"NinjaTrader port {port} is already in use or unavailable. Stop the other broadcaster before starting this bridge, or choose another port in Settings and the indicator.")
        self.ninjatrader_port = port
        self.startup_results["ninjatrader"] = {"status": "started", "message": f"NinjaTrader bridge listening on 127.0.0.1:{port}."}
        return {"ok": True, "message": self.startup_results["ninjatrader"]["message"], "runtime": self.info()}

    def stop_ninjatrader(self):
        from ninjatrader_broadcaster import stop_server

        stop_server()
        self.ninjatrader_port = None
        self.startup_results["ninjatrader"] = {"status": "stopped", "message": "NinjaTrader bridge stopped."}
        return {"ok": True, "message": "NinjaTrader bridge stopped.", "runtime": self.info()}

    def get_events(self, limit=64):
        import appy

        result = []
        for _ in range(max(1, min(int(limit), 256))):
            try:
                result.append(appy._frontend_events.get_nowait())
            except queue.Empty:
                break
        return result

    def close(self):
        if self.closed:
            return
        self.closed = True
        self.stop_event.set()
        self.stop_collector()
        self.stop_ninjatrader()
        if self.event_socket:
            self.event_socket.close()
        if self.event_thread and self.event_thread is not threading.current_thread():
            self.event_thread.join(timeout=2)
        if self.initialized:
            import appy
            from signal_performance import configure_engine
            with RPC_LOCK:
                if appy.engine:
                    appy.engine.dispose()
                appy.engine = None
                appy.DB_SCHEMA_CURRENT = False
                configure_engine(None)
            self.initialized = False


def get_runtime():
    global _runtime
    if _runtime is None:
        _runtime = NativeRuntime()
    return _runtime
