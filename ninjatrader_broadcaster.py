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

from gex_levels import level_net_gex, level_side_gex, level_strength

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

def _clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))

def _clean_regime_label(label: str) -> str:
    return (
        (label or "NEUTRAL")
        .replace("ðŸŸ¢ ", "")
        .replace("ðŸŸ¡ ", "")
        .replace("ðŸ”´ ", "")
        .replace("âšª ", "")
        .replace("WEAK ", "")
        .replace("LOW CONFIDENCE ", "")
        .strip()
    )

def _vote_label(score: float) -> str:
    if score > 0.20:
        return "CALL"
    if score < -0.20:
        return "PUT"
    return "WAIT"

def _format_pct(value: float) -> str:
    return f"{round((value or 0) * 100)}%"

def _symbol_component(components: List[dict], symbol: str) -> dict:
    return next((c for c in components if c.get("symbol") == symbol), {})

def _strike_key(level: dict) -> Optional[float]:
    try:
        strike = float(level.get("strike") or 0)
    except (TypeError, ValueError):
        return None

    if strike <= 0:
        return None
    return round(strike, 6)

def _ninjatrader_levels_for_symbol(overview_data: dict, symbol: str) -> List[dict]:
    key_levels_by_symbol = overview_data.get("gamma_levels", {}) or {}
    all_levels_by_symbol = overview_data.get("cockpit_levels", {}) or {}
    key_levels = key_levels_by_symbol.get(symbol, []) or []
    all_levels = all_levels_by_symbol.get(symbol, []) or key_levels
    has_explicit_key_set = symbol in key_levels_by_symbol
    key_strikes = {
        strike
        for strike in (_strike_key(level) for level in key_levels)
        if strike is not None
    }

    annotated_levels = []
    for level in all_levels:
        strike = _strike_key(level)
        annotated = dict(level)
        annotated["is_key_level"] = (not has_explicit_key_set) or strike in key_strikes
        annotated_levels.append(annotated)

    return annotated_levels

def _component_trend_score(component: dict) -> float:
    if "trend_score" in component:
        return _clamp(float(component.get("trend_score") or 0))

    spot = float(component.get("spot") or 0)
    flip = float(component.get("flip_strike") or 0)
    if spot <= 0 or flip <= 0:
        return 0

    return _clamp((spot - flip) / (flip * 0.006))

def _nearest_significant(levels: List[dict], spot: float, direction: str, sign: int) -> Optional[dict]:
    max_abs_gex = max((abs(level_side_gex(level, sign)) for level in levels), default=0)
    threshold = max_abs_gex * 0.20
    candidates = []

    for level in levels:
        strike = float(level.get("strike") or 0)
        gex = level_side_gex(level, sign)
        if strike <= 0 or abs(gex) < threshold:
            continue
        if direction == "above" and strike <= spot:
            continue
        if direction == "below" and strike >= spot:
            continue
        if gex == 0 or (1 if gex > 0 else -1) != sign:
            continue
        candidates.append({"strike": strike, "gex": gex})

    candidates.sort(key=lambda level: abs(level["strike"] - spot))
    return candidates[0] if candidates else None

def _room_score(level: Optional[dict], spot: float) -> float:
    if not level or spot <= 0:
        return 0.35

    dist_pct = abs(float(level["strike"]) - spot) / spot
    if dist_pct < 0.0035:
        return -0.45
    if dist_pct < 0.0075:
        return -0.15
    if dist_pct > 0.015:
        return 0.35
    return 0.10

def _classify_dealer(score: float) -> str:
    if score < -0.20:
        return "Short Gamma"
    if score > 0.20:
        return "Long Gamma"
    return "Mixed Gamma"

def _classify_liquidity(score: float) -> str:
    if score < -0.20:
        return "Downside Open"
    if score > 0.20:
        return "Upside Open"
    return "Balanced"

def _build_market_vote(overview_data: dict) -> dict:
    traders = overview_data.get("compass_traders") or overview_data.get("compass") or {}
    whale = overview_data.get("compass_whale") or {}

    if not whale:
        score = _clamp(float(traders.get("y_score") or 0))
        confidence = float(traders.get("confidence") if traders.get("confidence") is not None else 1)
        return {
            "score": score,
            "detail": f"Traders {_vote_label(score)} | confidence {_format_pct(confidence)}",
        }

    trader_confidence = float(traders.get("confidence") or 0)
    whale_confidence = float(whale.get("confidence") or 0)
    total_confidence = trader_confidence + whale_confidence or 1
    score = _clamp(
        (
            (float(traders.get("y_score") or 0) * trader_confidence)
            + (float(whale.get("y_score") or 0) * whale_confidence)
        ) / total_confidence
    )
    avg_confidence = (trader_confidence + whale_confidence) / 2

    return {
        "score": score,
        "detail": f"Traders {_vote_label(float(traders.get('y_score') or 0))} / Whale {_vote_label(float(whale.get('y_score') or 0))} | confidence {_format_pct(avg_confidence)}",
    }

def _local_net_gex(component: dict, levels: List[dict], spot: float) -> float:
    effective_gex = component.get("effective_gex")
    if effective_gex not in (None, ""):
        return float(effective_gex or 0)

    if levels and spot > 0:
        return sum(
            level_net_gex(level)
            for level in levels
            if abs(float(level.get("strike") or 0) - spot) / spot <= 0.02
        )

    return float(component.get("net_gex") or 0)

def _build_dealer_vote(component: dict, levels: List[dict]) -> dict:
    spot = float(component.get("spot") or 0)
    flip = float(component.get("flip_strike") or 0)
    local_net = _local_net_gex(component, levels, spot)
    direction = _clamp((spot - flip) / (flip * 0.006)) if spot > 0 and flip > 0 else 0
    gamma_multiplier = 1.0 if local_net < 0 else 0.45
    score = _clamp(direction * gamma_multiplier)
    dealer_state = "short gamma momentum" if local_net < 0 else "long gamma compression"
    flip_quality = component.get("flip_quality") or "unknown"
    position = "above" if spot >= flip and flip > 0 else "below" if flip > 0 else "near"

    return {
        "score": score,
        "label": _classify_dealer(score),
        "detail": f"{dealer_state}; spot {position} flip ({flip_quality})",
        "local_net": local_net,
    }

def _build_liquidity_vote(levels: List[dict], spot: float) -> dict:
    if not levels or spot <= 0:
        return {"score": 0, "label": "Balanced", "detail": "No liquidity profile"}

    upside_wall = _nearest_significant(levels, spot, "above", 1)
    downside_wall = _nearest_significant(levels, spot, "below", 1)
    upside_accel = _nearest_significant(levels, spot, "above", -1)
    downside_accel = _nearest_significant(levels, spot, "below", -1)
    call_room = _room_score(upside_wall, spot) + (0.15 if upside_accel else 0)
    put_room = _room_score(downside_wall, spot) + (0.15 if downside_accel else 0)
    score = _clamp(call_room - put_room)
    above_text = f"upside wall {upside_wall['strike']:.0f}" if upside_wall else "upside open"
    below_text = f"downside wall {downside_wall['strike']:.0f}" if downside_wall else "downside open"

    return {
        "score": score,
        "label": _classify_liquidity(score),
        "detail": f"{above_text}; {below_text}",
    }

def _nearest_any_significant(levels: List[dict], spot: float, direction: int) -> Optional[dict]:
    max_abs_gex = max((level_strength(level) for level in levels), default=0)
    threshold = max_abs_gex * 0.18
    candidates = []

    for level in levels:
        strike = float(level.get("strike") or 0)
        gex = level_net_gex(level)
        if strike <= 0 or level_strength(level) < threshold:
            continue
        if direction == 1 and strike <= spot:
            continue
        if direction == -1 and strike >= spot:
            continue
        candidates.append({"strike": strike, "gex": gex})

    candidates.sort(key=lambda level: abs(level["strike"] - spot))
    return candidates[0] if candidates else None

def _fallback_target(spot: float, direction: int, expansion: bool) -> float:
    move_pct = 0.012 if expansion else 0.006
    return spot * (1 + (direction * move_pct))

def _choose_price_objective(levels: List[dict], spot: float, direction: int, expansion: bool, flip: float) -> float:
    if not levels:
        return _fallback_target(spot, direction, expansion)

    if not expansion and flip > 0 and ((direction == 1 and flip > spot) or (direction == -1 and flip < spot)):
        return flip

    preferred_sign = -1 if expansion else 1
    preferred = _nearest_significant(levels, spot, "above" if direction == 1 else "below", preferred_sign)
    any_level = preferred or _nearest_any_significant(levels, spot, direction)
    return float(any_level["strike"]) if any_level else _fallback_target(spot, direction, expansion)

def _choose_invalidation(levels: List[dict], spot: float, direction: int, flip: float) -> float:
    opposite_direction = "below" if direction == 1 else "above"
    wall = _nearest_significant(levels, spot, opposite_direction, 1) or _nearest_any_significant(levels, spot, -direction)
    if wall:
        return float(wall["strike"])

    if flip > 0 and ((direction == 1 and flip < spot) or (direction == -1 and flip > spot)):
        return flip

    return spot * (1 - (direction * 0.006))

def _build_trade_plan(
    component: dict,
    levels: List[dict],
    final_score: float,
    market_vote: dict,
    overview_data: dict,
) -> dict:
    if not component or abs(final_score) < 0.22:
        return {
            "target": None,
            "invalidation": None,
            "description": "Bias is too mixed for a price objective.",
        }

    spot = float(component.get("spot") or 0)
    flip = float(component.get("flip_strike") or 0)
    if spot <= 0:
        return {
            "target": None,
            "invalidation": None,
            "description": "Bias is too mixed for a price objective.",
        }

    local_net = _local_net_gex(component, levels, spot)
    traders = overview_data.get("compass_traders") or {}
    whale = overview_data.get("compass_whale") or {}
    market_vol = (float(traders.get("x_score") or 0) + float(whale.get("x_score") or 0)) / 2
    direction = 1 if final_score > 0 else -1
    is_expansion = (
        local_net < 0
        or market_vol < -0.15
        or ((market_vote["score"] > 0) == (direction > 0) and abs(market_vote["score"]) > 0.65)
    )
    target = _choose_price_objective(levels, spot, direction, is_expansion, flip)
    invalidation = _choose_invalidation(levels, spot, direction, flip)
    description = (
        f"Target follows open liquidity in the {'upside' if direction == 1 else 'downside'} direction."
        if is_expansion
        else "Target is a reversion move toward flip or the next stabilizing gamma level."
    )

    return {
        "target": round(target, 4) if target > 0 else None,
        "invalidation": round(invalidation, 4) if invalidation > 0 else None,
        "description": description,
    }

def _dashboard_payload_for_symbol(symbol: str, overview_data: dict) -> dict:
    components = overview_data.get("components", [])
    component = _symbol_component(components, symbol)
    compass = overview_data.get("compass", {})
    whale = overview_data.get("compass_whale", compass)
    key_levels_by_symbol = overview_data.get("gamma_levels", {}) or {}
    if symbol in key_levels_by_symbol:
        levels = key_levels_by_symbol.get(symbol, []) or []
    else:
        levels = overview_data.get("cockpit_levels", {}).get(symbol, [])

    spot = float(component.get("spot") or 0)
    flip = float(component.get("flip_strike") or 0)
    market_vote = _build_market_vote(overview_data)
    dealer_vote = _build_dealer_vote(component, levels)
    liquidity_vote = _build_liquidity_vote(levels, spot)
    raw_score = _clamp(
        (market_vote["score"] * 0.45)
        + (dealer_vote["score"] * 0.35)
        + (liquidity_vote["score"] * 0.20)
    )
    agreement = len([
        score for score in [market_vote["score"], dealer_vote["score"], liquidity_vote["score"]]
        if (score > 0) == (raw_score > 0) and abs(score) > 0.20 and raw_score != 0
    ])
    confidence_penalty = float(component.get("confidence") or 0.5)
    final_score = raw_score * _clamp(0.65 + (agreement * 0.12), 0.65, 1) * confidence_penalty
    confidence = confidence_penalty

    abs_score = abs(final_score)
    bias = "WAIT"
    if abs_score >= 0.22:
        bias = "CALL BIAS" if final_score > 0 else "PUT BIAS"

    whale_label = whale.get("label") or _vote_label(float(whale.get("y_score") or 0))
    plan = _build_trade_plan(component, levels, final_score, market_vote, overview_data)
    agreement_text = "Inputs are mixed; require price confirmation." if agreement < 2 else "Inputs are aligned enough for directional context."
    context = f"{agreement_text} {plan['description']}".strip()

    return {
        "dashboard_symbol": symbol,
        "dashboard_bias": bias,
        "dashboard_bias_score": round(final_score, 4),
        "dashboard_confidence": round(confidence, 4),
        "dashboard_target": plan["target"],
        "dashboard_invalidation": plan["invalidation"],
        "dashboard_flip": round(flip, 4) if flip > 0 else None,
        "dashboard_context": context,
        "dashboard_market": f"Market: {_vote_label(market_vote['score'])}",
        "dashboard_dealer": f"Dealer: {dealer_vote['label']}",
        "dashboard_liquidity": f"Liquidity: {liquidity_vote['label']}",
        "dashboard_whale": f"Whale: {whale_label}",
        "dashboard_market_detail": market_vote["detail"],
        "dashboard_dealer_detail": dealer_vote["detail"],
        "dashboard_liquidity_detail": liquidity_vote["detail"],
    }

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
        
        label = compass.get("label", "NEUTRAL")
        confidence = compass.get("confidence_label")
        if not confidence:
            confidence = "LOW" if compass.get("confidence", 1) < 0.60 else "HIGH"

        ndx_dashboard = _dashboard_payload_for_symbol("NDX", overview_data)
        spx_dashboard = _dashboard_payload_for_symbol("SPX", overview_data)
        generic_dashboard = ndx_dashboard if ndx_data else spx_dashboard
        
        # Build payload with all index prices
        payload = {
            "type": "REGIME_UPDATE",
            "timestamp": datetime.now().isoformat(),
            "regime": label.replace("🟢 ", "").replace("🟡 ", "").replace("🔴 ", "").replace("⚪ ", "").replace("WEAK ", "").strip(),
            "regime_code": extract_regime_code(label),
            "confidence": confidence,
            "confidence_score": round(compass.get("confidence", 0), 4),
            "x_score": round(compass.get("x_score", 0), 4),
            "y_score": round(compass.get("y_score", 0), 4),
            "strategy": compass.get("strategy", ""),
            # SPY data
            "spot_spy": spy_data.get("spot", 0),
            "flip_spy": spy_data.get("flip_strike", 0),
            "net_gex_spy": spy_data.get("net_gex", 0),
            # SPX data (for ES charts)
            "spot_spx": spx_data.get("spot", 0),
            "flip_spx": spx_data.get("flip_strike", 0),
            # NDX data (for NQ charts)
            "spot_ndx": ndx_data.get("spot", 0),
            "flip_ndx": ndx_data.get("flip_strike", 0),
            "accel_ndx": round(ndx_data.get("acceleration", 0), 2),
            "accel_spx": round(spx_data.get("acceleration", 0), 2),
            # NinjaTrader gets every level, with the existing filter marked as key.
            "gamma_levels_ndx": _ninjatrader_levels_for_symbol(overview_data, "NDX"),
            "gamma_levels_spx": _ninjatrader_levels_for_symbol(overview_data, "SPX"),
        }
        payload["regime"] = payload["regime"].replace("LOW CONFIDENCE ", "").strip()
        payload["regime"] = _clean_regime_label(label)
        payload.update(generic_dashboard)
        for symbol, dashboard in (("ndx", ndx_dashboard), ("spx", spx_dashboard)):
            for key, value in dashboard.items():
                payload[f"{key}_{symbol}"] = value

        try:
            from signal_performance import update_signal_performance_for_payload
            payload = update_signal_performance_for_payload(payload)
        except Exception as e:
            logger.warning(f"Signal performance update failed: {e}")
        
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
