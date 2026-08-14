import socket
import time

import pytest

from ninjatrader_broadcaster import MAX_CLIENTS, broadcaster, start_server, stop_server


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture(autouse=True)
def clean_broadcaster():
    stop_server()
    yield
    stop_server()


def test_server_defaults_to_loopback_and_can_stop_and_restart():
    port = _free_port()
    assert start_server(port)
    assert broadcaster.server_socket.getsockname()[0] == "127.0.0.1"

    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    assert _wait_until(lambda: len(broadcaster.clients) == 1)
    stop_server()
    assert not broadcaster.running
    assert broadcaster.clients == []
    client.close()

    assert start_server(port)
    assert broadcaster.running


def test_bind_failure_leaves_server_retryable():
    port = _free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", port))
    blocker.listen(1)
    try:
        assert not start_server(port)
        assert not broadcaster.running
        assert broadcaster.server_socket is None
    finally:
        blocker.close()

    assert start_server(port)
    assert broadcaster.running


def test_server_caps_clients_and_closes_rejections():
    port = _free_port()
    assert start_server(port)
    clients = [socket.create_connection(("127.0.0.1", port), timeout=2) for _ in range(MAX_CLIENTS + 1)]
    try:
        assert _wait_until(lambda: len(broadcaster.clients) == MAX_CLIENTS)
        time.sleep(0.1)
        assert len(broadcaster.clients) == MAX_CLIENTS
    finally:
        for client in clients:
            client.close()


def test_broadcast_does_not_hold_client_lock_during_send():
    class LockCheckingClient:
        def __init__(self):
            self.lock_was_free = False

        def sendall(self, _payload):
            self.lock_was_free = broadcaster.lock.acquire(blocking=False)
            if self.lock_was_free:
                broadcaster.lock.release()

        def close(self):
            pass

    client = LockCheckingClient()
    with broadcaster.lock:
        broadcaster.clients.append(client)

    broadcaster.broadcast({"type": "TEST"})

    assert client.lock_was_free
