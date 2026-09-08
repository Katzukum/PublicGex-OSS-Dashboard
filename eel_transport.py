"""Serialize Eel's WebSocket writes so large replies cannot interleave frames.

Eel 0.18 spawns one greenlet per call and gevent-websocket's sendall can
yield midway through a frame. Concurrent sends then corrupt the stream.
Keep this small compatibility adapter covered by a real WebSocket test.
"""

import logging
from gevent.lock import Semaphore


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
    eel_module._opengamma_serialized_transport = True
