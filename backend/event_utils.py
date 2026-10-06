"""Collector events reach only the private runtime's authenticated ephemeral socket."""
from __future__ import annotations

import json
import logging
import os
import socket

logger = logging.getLogger(__name__)


def send_message(message: dict) -> None:
    port = int(os.environ.get("PUBLICGEX_EVENT_PORT", "0"))
    token = os.environ.get("PUBLICGEX_EVENT_TOKEN", "")
    if not port or not token:
        return
    try:
        payload = json.dumps({"token": token, "event": message}, allow_nan=False).encode("utf-8")
        with socket.create_connection(("127.0.0.1", port), timeout=3) as client:
            client.sendall(payload)
    except (OSError, ValueError):
        logger.warning("Private event delivery failed")


def send_event(event_type: str, payload: dict) -> None:
    send_message({"type": event_type, "payload": payload})
