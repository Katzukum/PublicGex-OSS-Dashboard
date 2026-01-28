"""NinjaTrader Regime Broadcaster (Server Mode).

This module implements a TCP Server that acts as a bridge between the
Python analysis backend and NinjaTrader C# indicators.

Attributes:
    NT_PORT (int): Default TCP port (5010) for NinjaTrader connections.
"""

import socket
import json
import logging
import threading
import time
from datetime import datetime
from typing import Optional, List

logger = logging.getLogger(__name__)

# Default port for NinjaTrader communication
NT_PORT = 5010

class NinjaBroadcaster:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(NinjaBroadcaster, cls).__new__(cls)
            cls._instance.clients = []
            cls._instance.lock = threading.Lock()
            cls._instance.running = False
            cls._instance.server_socket = None
        return cls._instance

    def start_server(self, port=NT_PORT):
        """Starts the TCP Server in a background thread."""
        if self.running:
            return
            
        self.running = True
        thread = threading.Thread(target=self._server_loop, args=(port,), daemon=True)
        thread.start()
        print(f"[NinjaBroadcaster] Server started on port {port}")
        logger.info(f"NinjaBroadcaster Server started on port {port}")

    def _server_loop(self, port):
        try:
            self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.server_socket.bind(('0.0.0.0', port))
            self.server_socket.listen(10) # Backlog of 10
            
            while self.running:
                try:
                    client_sock, addr = self.server_socket.accept()
                    print(f"[NinjaBroadcaster] Client connected: {addr}")
                    
                    with self.lock:
                        self.clients.append(client_sock)
                except Exception as e:
                    if self.running:
                        logger.error(f"Accept error: {e}")
                        time.sleep(1)
        except Exception as e:
            logger.critical(f"Server loop failed: {e}")

    def broadcast(self, payload: dict) -> None:
        """Sends JSON data to all connected NinjaTrader clients.

        Iterates through the list of active client sockets and sends the
        newline-delimited JSON message. Handles disconnection cleanup automatically.

        Args:
            payload: A dictionary containing the regime or market data to send.
        """
        with self.lock:
            if not self.clients:
                # No clients connected
                return

        json_msg = json.dumps(payload) + "\n"
        encoded_msg = json_msg.encode('utf-8')
        
        to_remove = []
        
        with self.lock:
            for client in self.clients:
                try:
                    client.sendall(encoded_msg)
                except Exception as e:
                    logger.warning(f"Client disconnected during send: {e}")
                    to_remove.append(client)
            
            # Clean up disconnected clients
            for dead_client in to_remove:
                if dead_client in self.clients:
                    self.clients.remove(dead_client)
                    try:
                        dead_client.close()
                    except:
                        pass
                        
        print(f"[NinjaBroadcaster] Sent update to {len(self.clients)} charts.")

# Global instance
broadcaster = NinjaBroadcaster()

def start_server(port=NT_PORT):
    broadcaster.start_server(port)

# Regime code mapping for NinjaScript integer parsing
REGIME_CODES = {
    "GRIND UP": 1,
    "MELT UP": 2,
    "SUPPORT / CHOP": 3,
    "CRASH / FLUSH": 4,
}

def extract_regime_code(label: str) -> int:
    """Extract numeric regime code from label string."""
    for key, code in REGIME_CODES.items():
        if key in label.upper():
            return code
    return 0  # Unknown

def send_regime_update(overview_data: dict, port: int = NT_PORT) -> bool:
    """Broadcasts a full market regime update to connected NinjaTrader clients.

    Transforms the internal 'overview_data' structure into a flat, parseable
    JSON payload expected by the NinjaTrader 'OpenGamma' indicator.

    Args:
        overview_data: The comprehensive market overview dictionary generated
            by appy.py or publicData.py. Must contain 'compass' and 'components'.
        port: The TCP port to broadcast to (default: 5010).

    Returns:
        bool: True if broadcast was submitted successfully (even if no clients blocked).
              False if payload preparation failed.
    """
    try:
        compass = overview_data.get("compass", {})
        components = overview_data.get("components", [])
        
        # Extract data for each important symbol
        spy_data = next((c for c in components if c.get("symbol") == "SPY"), {})
        spx_data = next((c for c in components if c.get("symbol") == "SPX"), {})
        ndx_data = next((c for c in components if c.get("symbol") == "NDX"), {})
        
        # Determine confidence from label
        label = compass.get("label", "NEUTRAL")
        confidence = "LOW" if "WEAK" in label else "HIGH"
        
        # Build payload with all index prices
        payload = {
            "type": "REGIME_UPDATE",
            "timestamp": datetime.now().isoformat(),
            "regime": label.replace("🟢 ", "").replace("🟡 ", "").replace("🔴 ", "").replace("⚪ ", "").replace("WEAK ", "").strip(),
            "regime_code": extract_regime_code(label),
            "confidence": confidence,
            "x_score": round(compass.get("x_score", 0), 4),
            "y_score": round(compass.get("y_score", 0), 4),
            "strategy": compass.get("strategy", ""),
            # SPY data
            "spot_spy": spy_data.get("spot", 0),
            "flip_spy": spy_data.get("flip_strike", 0),
            "net_gex_spy": spy_data.get("net_gex", 0),
            # SPX data (for ES charts)
            "spot_spx": spx_data.get("spot", 0),
            # NDX data (for NQ charts)
            "spot_ndx": ndx_data.get("spot", 0),
            # Gamma levels for S/R lines
            "gamma_levels_ndx": overview_data.get("gamma_levels", {}).get("NDX", []),
            "gamma_levels_spx": overview_data.get("gamma_levels", {}).get("SPX", []),
        }
        
        # Use simple global broadcaster to send
        broadcaster.broadcast(payload)
        return True
        
    except Exception as e:
        logger.error(f"Failed to prepare update: {e}")
        return False

if __name__ == "__main__":
    # Test
    logging.basicConfig(level=logging.INFO)
    start_server()
    print("Server started. Waiting for clients...")
    while True:
        time.sleep(1)
