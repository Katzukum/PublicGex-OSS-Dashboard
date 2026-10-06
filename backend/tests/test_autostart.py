"""Live startup policy without contacting a market-data account."""
import pytest

from app_settings import DEFAULT_SETTINGS, validate_settings
from native_runtime import NativeRuntime


def runtime_with_stubs(monkeypatch, *, credentials=True, demo=False, **preferences):
    runtime = NativeRuntime()
    runtime.demo = demo
    monkeypatch.setattr("app_settings.read_settings", lambda: DEFAULT_SETTINGS | preferences)
    monkeypatch.setattr(runtime, "credentials_configured", lambda: credentials)
    calls = []
    def started(name, *args):
        calls.append((name, args))
        return {"ok": True, "message": name + " started"}
    monkeypatch.setattr(runtime, "start_collector", lambda: started("collector"))
    monkeypatch.setattr(runtime, "start_ninjatrader", lambda port: started("ninjatrader", port))
    return runtime, calls


def test_live_starts_both_once_on_original_indicator_port(monkeypatch):
    runtime, calls = runtime_with_stubs(monkeypatch)
    runtime.start_integrations()
    runtime.start_integrations()
    assert calls == [("ninjatrader", (5010,)), ("collector", ())]
    assert all(result["status"] == "started" for result in runtime.startup_results.values())


def test_missing_credentials_leaves_bridge_running_and_explains_collector(monkeypatch):
    runtime, calls = runtime_with_stubs(monkeypatch, credentials=False)
    runtime.start_integrations()
    assert calls == [("ninjatrader", (5010,))]
    assert runtime.startup_results["collector"]["status"] == "blocked"
    assert ".env" in runtime.startup_results["collector"]["message"]


@pytest.mark.parametrize("preferences", [
    {"demo": True}, {"auto_start_collector": False, "auto_start_ninjatrader": False},
])
def test_demo_or_disabled_preferences_start_neither(monkeypatch, preferences):
    runtime, calls = runtime_with_stubs(monkeypatch, **preferences)
    runtime.start_integrations()
    assert calls == []
    assert all(result["status"] == "disabled" for result in runtime.startup_results.values())


def test_busy_bridge_does_not_prevent_collector_start(monkeypatch):
    runtime, calls = runtime_with_stubs(monkeypatch)
    def busy(port):
        raise ValueError("Port unavailable")
    monkeypatch.setattr(runtime, "start_ninjatrader", busy)
    runtime.start_integrations()
    assert calls == [("collector", ())]
    assert runtime.startup_results["ninjatrader"] == {"status": "error", "message": "Port unavailable"}


@pytest.mark.parametrize("patch", [
    {"auto_start_collector": "false"}, {"auto_start_ninjatrader": 1},
    {"ninjatrader_port": 5005},
    {"ninjatrader_port": 1023}, {"ninjatrader_port": 65536}, {"ninjatrader_port": 15010.5},
])
def test_reject_invalid_startup_settings(patch):
    with pytest.raises(ValueError):
        validate_settings(patch)


@pytest.mark.parametrize("port", [5010, 15010, 16000])
def test_original_and_custom_indicator_ports_are_valid(port):
    assert validate_settings({"ninjatrader_port": port})["ninjatrader_port"] == port


def test_startup_respects_custom_port(monkeypatch):
    runtime, calls = runtime_with_stubs(monkeypatch, ninjatrader_port=16000)
    runtime.start_integrations()
    assert calls == [("ninjatrader", (16000,)), ("collector", ())]


def test_busy_listener_is_not_shared_or_closed():
    import socket
    from ninjatrader_broadcaster import broadcaster

    assert not broadcaster.running
    # Use only a disposable port; never probe or bind the user's real 5010.
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen(1)
        port = other.getsockname()[1]
        assert broadcaster.start_server(port) is False
        assert not broadcaster.running
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            accepted, _ = other.accept()
            accepted.close()
