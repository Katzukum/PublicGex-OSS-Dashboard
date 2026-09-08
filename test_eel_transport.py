import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import time
from types import SimpleNamespace

import gevent
import pytest

from eel_transport import install_serialized_transport


def test_cooperative_writes_never_interleave_and_installation_is_idempotent():
    chunks = []

    class Socket:
        closed = False

        def send(self, message):
            chunks.append(message + ':start')
            gevent.sleep(0)
            chunks.append(message + ':end')

    bridge = SimpleNamespace()
    install_serialized_transport(bridge)
    send = bridge._repeated_send
    install_serialized_transport(bridge)
    assert bridge._repeated_send is send
    ws = Socket()
    gevent.joinall([gevent.spawn(send, ws, str(index)) for index in range(6)], raise_error=True)
    assert chunks == [part for index in range(6) for part in (f'{index}:start', f'{index}:end')]


def test_failed_partial_frame_is_closed_not_retried():
    class Socket:
        closed = False
        attempts = 0

        def send(self, _message):
            self.attempts += 1
            raise OSError('partial send')

        def close(self):
            self.closed = True

    bridge = SimpleNamespace()
    install_serialized_transport(bridge)
    ws = Socket()
    bridge._repeated_send(ws, 'payload')
    assert ws.closed
    assert ws.attempts == 1


@pytest.mark.skipif(not shutil.which('node'), reason='Node is needed for WebSocket integration')
def test_real_eel_socket_survives_concurrent_large_replies():
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
        port = probe.getsockname()[1]
    root = Path(__file__).resolve().parent
    server_code = """
import eel, sys
from eel_transport import install_serialized_transport
eel.init('web')
install_serialized_transport(eel)
@eel.expose
def bulk(): return 'x' * 7000000
@eel.expose
def ping(): return 'pong'
eel.start('index.html', mode=None, host='127.0.0.1', port=int(sys.argv[1]), close_callback=lambda *args: None)
"""
    process = subprocess.Popen([sys.executable, '-u', '-c', server_code, str(port)], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        deadline = time.monotonic() + 8
        while True:
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=.2):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError('Test server did not start')
                time.sleep(.05)
        client = """
const assert = require('node:assert/strict');
const ws = new WebSocket(`ws://127.0.0.1:${process.argv[1]}/eel?page=transport-test`);
const pending = new Set([1,2,3,4,5,6]);
ws.onopen = () => { for (const call of pending) ws.send(JSON.stringify({call,name:call % 2 ? 'bulk':'ping',args:[]})); };
ws.onmessage = event => {
  const reply = JSON.parse(event.data);
  assert.equal(reply.status, 'ok');
  assert.ok(pending.delete(reply.return));
  assert.equal(reply.value.length, reply.return % 2 ? 7000000 : 4);
  if (!pending.size) { ws.close(); process.exit(0); }
};
ws.onerror = () => process.exit(2);
setTimeout(() => process.exit(3), 12000);
"""
        result = subprocess.run(['node', '-e', client, str(port)], cwd=root, capture_output=True, text=True, timeout=18)
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        process.terminate()
        process.communicate(timeout=5)
