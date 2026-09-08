"""Serialize Eel's WebSocket writes so large replies cannot interleave frames.

Eel 0.18 spawns one greenlet per call and gevent-websocket's sendall can
yield midway through a frame. Concurrent sends then corrupt the stream.
Keep this small compatibility adapter covered by a real WebSocket test.
"""

import logging
import json
import math
from gevent.lock import Semaphore


def _finite_values(value):
    """Pandas represents missing quotes as NaN; browser JSON requires null."""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {key: _finite_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_values(item) for item in value]
    return value


def browser_json(value):
    return json.dumps(_finite_values(value), allow_nan=False, default=lambda _value: None)


def install_serialized_transport(eel_module):
    if getattr(eel_module, '_opengamma_serialized_transport', False):
        return
    send_lock = Semaphore(1)

    def serialized_send(ws, message):
        with send_lock:
            if getattr(ws, 'closed', False):
                return
            try:
                ws.send(message)
            except Exception:
                # A partially sent frame cannot safely be retried on this socket.
                logging.getLogger(__name__).warning('Dashboard connection closed during send', exc_info=True)
                try:
                    ws.close()
                except Exception:
                    pass

    eel_module._repeated_send = serialized_send
    eel_module._safe_json = browser_json
    eel_module._opengamma_serialized_transport = True
