"""Repeated launch recognizes its service without claiming unrelated listeners."""
from contextlib import contextmanager
import errno
import json
import logging
import socketserver
import threading

import pytest

pytest.importorskip("scipy")
from prediction import service as startup


@contextmanager
def running_server(server):
    with server:
        worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        worker.start()
        try:
            yield server.server_address[1]
        finally:
            server.shutdown()
            worker.join(timeout=2)
            assert not worker.is_alive()


@contextmanager
def response_server(response):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            self.request.settimeout(2)
            request = json.loads(self.rfile.readline(4096))
            value = response(request)
            if isinstance(value, bytes):
                wire = value
            else:
                wire = json.dumps(value).encode() + b"\n"
            self.wfile.write(wire)

    class Server(socketserver.ThreadingTCPServer):
        daemon_threads = True

    with running_server(Server(("127.0.0.1", 0), Handler)) as port:
        yield port


def legacy_hello(request):
    return {
        "schema_version": 2,
        "type": "STATUS",
        "instrument": request["instrument"],
        "history_policy": request["history_policy"],
        "feed_label": request["feed_label"],
        "series_id": "",
        "status": "WAITING_FOR_HISTORY",
        "message": "Connected; upload the chart's completed history then HISTORY_END",
        "server_time_utc": "2026-09-30T14:30:00Z",
    }


def test_repeated_launch_recognizes_matching_service_without_mutating_data(tmp_path, caplog):
    data_dir = tmp_path / "active"
    with startup.PredictionCoordinator(data_dir) as coordinator:
        with running_server(startup.ForecastServer(coordinator, 0)) as port:
            with caplog.at_level(logging.INFO):
                assert startup.main(["--port", str(port), "--data-dir", str(data_dir)]) == 0
            # Repeated launch must leave the original listener available.
            assert startup.probe_existing_service(port) == {"data_dir": str(data_dir.resolve())}
            assert coordinator.services == {}
            assert coordinator.current == {}
            assert not any(path.is_file() for path in data_dir.rglob("*"))
    assert "already running" in caplog.text.lower()


def test_existing_service_with_different_data_directory_is_not_reused(tmp_path, caplog):
    with startup.PredictionCoordinator(tmp_path / "active") as coordinator:
        with running_server(startup.ForecastServer(coordinator, 0)) as port:
            assert startup.main(["--port", str(port), "--data-dir", str(tmp_path / "requested")]) == 1
            assert startup.probe_existing_service(port) is not None
    assert "already running" not in caplog.text.lower() or "different" in caplog.text.lower()


def test_unrelated_listener_is_reported_as_conflict(tmp_path, caplog):
    with response_server(lambda _: b"An unrelated local service\n") as port:
        assert startup.probe_existing_service(port) is None
        assert startup.main(["--port", str(port), "--data-dir", str(tmp_path)]) == 1
    assert str(port) in caplog.text
    assert "traceback" not in caplog.text.lower()


def test_legacy_service_probe_is_read_only_and_recognized():
    requests = []

    def respond(request):
        requests.append(request)
        return legacy_hello(request)

    with response_server(respond) as port:
        assert startup.probe_existing_service(port) == {"data_dir": None}
    assert len(requests) == 1
    assert requests[0]["type"] == "HELLO"
    assert requests[0]["schema_version"] == 2
    assert requests[0]["feed_label"].startswith("LauncherProbe")


def test_legacy_service_can_satisfy_default_launch_with_unverified_directory_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(startup, "DEFAULT_DATA_DIR", tmp_path)
    with response_server(legacy_hello) as port:
        with caplog.at_level(logging.INFO):
            assert startup.main(["--port", str(port)]) == 0
    assert "already running" in caplog.text.lower()
    assert "verif" in caplog.text.lower()


def test_legacy_service_cannot_satisfy_custom_data_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(startup, "DEFAULT_DATA_DIR", tmp_path / "default")
    with response_server(legacy_hello) as port:
        assert startup.main(["--port", str(port), "--data-dir", str(tmp_path / "custom")]) == 1


@pytest.mark.parametrize("field,value", [
    ("feed_label", "A different client"),
    ("server_time_utc", "not a timestamp"),
    ("service_name", "unrelated.service"),
])
def test_probe_does_not_accept_invalid_or_wrong_identity_response(field, value):
    def respond(request):
        reply = legacy_hello(request)
        reply[field] = value
        return reply

    with response_server(respond) as port:
        assert startup.probe_existing_service(port) is None


def test_non_address_in_use_bind_failure_does_not_probe(tmp_path, monkeypatch, caplog):
    def fail_bind(*args, **kwargs):
        raise OSError(errno.EACCES, "Access denied to this port")

    def unexpected_probe(*args, **kwargs):
        pytest.fail("Only an address-in-use failure should inspect an existing listener")

    monkeypatch.setattr(startup, "ForecastServer", fail_bind)
    monkeypatch.setattr(startup, "probe_existing_service", unexpected_probe)
    assert startup.main(["--data-dir", str(tmp_path)]) == 1
    assert "traceback" not in caplog.text.lower()


def test_report_does_not_bind_or_probe(tmp_path, monkeypatch, capsys):
    def unexpected_network(*args, **kwargs):
        pytest.fail("Report mode must not open or inspect a listener")

    monkeypatch.setattr(startup, "ForecastServer", unexpected_network)
    monkeypatch.setattr(startup, "probe_existing_service", unexpected_network)
    assert startup.main(["--data-dir", str(tmp_path), "--report"]) == 0
    assert json.loads(capsys.readouterr().out) == {"series": []}
