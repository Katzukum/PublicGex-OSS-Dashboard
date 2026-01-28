import socket
import json
import logging

# Configure logging for this module
logger = logging.getLogger(__name__)

def send_event(event_type: str, payload: dict, port: int = 5005) -> None:
    """Sends a JSON event to the local notification server.

    Used by external scripts (like publicData.py) to push updates to the
    main Dashboard backend (appy.py).

    Args:
        event_type: A string identifier for the event (e.g., 'magnet_change').
        payload: A dictionary containing the event data.
        port: The TCP port of the local event server (default: 5005).

    Returns:
        None

    Raises:
        ConnectionRefusedError: If the Dashboard backend is not running.
    """
    try:
        message = {
            "type": event_type,
            "payload": payload
        }
        json_msg = json.dumps(message)
        
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(5) # Increased timeout to 5s to handle load
            s.connect(('127.0.0.1', port))
            s.sendall(json_msg.encode('utf-8'))
            
    except ConnectionRefusedError:
        logger.warning(f"Could not connect to Event Server on port {port}. Is the Dashboard open?")
    except Exception as e:
        logger.error(f"Failed to send event {event_type}: {e}")
