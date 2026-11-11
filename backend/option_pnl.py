"""Conservative, read-only option structure marks from stored two-sided quotes."""

from __future__ import annotations

from datetime import timedelta
import hashlib
from math import isfinite

from models import GexSnapshot, RawOptionGreek

OPTION_MULTIPLIER = 100
MAX_QUOTE_AGE_SECONDS = 120


def _quote(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) and number > 0 else None


def select_structure(rows, bias: str, scenario: str, spot: float, target: float | None):
    """Return (OSI, signed quantity) legs, or None when the structure is unavailable."""
    if target is None or spot <= 0:
        return None
    if scenario in {"PIN", "PIN_MEAN_REVERSION"}:
        side = "CALL" if target >= spot else "PUT"
        strikes = sorted({float(row.strike_price) for row in rows if side in str(row.option_type).upper()})
        if len(strikes) < 3:
            return None
        center = min(strikes, key=lambda strike: abs(strike - target))
        widths = sorted({strike - center for strike in strikes if strike > center and 2 * center - strike in strikes})
        if not widths:
            return None
        width = widths[0]
        desired = [(center - width, 1), (center, -2), (center + width, 1)]
    else:
        side = "CALL" if bias == "CALL" else "PUT" if bias == "PUT" else None
        if side is None:
            return None
        candidates = sorted({float(row.strike_price) for row in rows if side in str(row.option_type).upper()})
        if len(candidates) < 2:
            return None
        long_strike = min(candidates, key=lambda strike: abs(strike - spot))
        shorts = [strike for strike in candidates if (strike > long_strike if side == "CALL" else strike < long_strike)]
        if not shorts:
            return None
        short_strike = min(shorts, key=lambda strike: abs(strike - target))
        desired = [(long_strike, 1), (short_strike, -1)]
    legs = []
    for strike, quantity in desired:
        match = next((row for row in rows if float(row.strike_price) == strike and side in str(row.option_type).upper()), None)
        if match is None or not match.osi_symbol:
            return None
        legs.append((match.osi_symbol, quantity))
    return legs


def mark_structure(legs, entry_rows, exit_rows, fees_per_contract: float):
    """Pay ask/bid on entry and cross the spread again on exit; return dollar P&L/R."""
    entry = {row.osi_symbol: row for row in entry_rows}
    exit_ = {row.osi_symbol: row for row in exit_rows}
    opening = closing = 0.0
    contracts = 0
    for osi, quantity in legs:
        before, after = entry.get(osi), exit_.get(osi)
        if before is None or after is None:
            return None, None, "missing_leg"
        prices = [_quote(before.bid), _quote(before.ask), _quote(after.bid), _quote(after.ask)]
        if any(price is None for price in prices) or prices[0] > prices[1] or prices[2] > prices[3]:
            return None, None, "missing_two_sided_quote"
        opening += quantity * (prices[1] if quantity > 0 else prices[0])
        closing += quantity * (prices[2] if quantity > 0 else prices[3])
        contracts += abs(quantity)
    if opening <= 0:
        return None, None, "invalid_debit"
    pnl = OPTION_MULTIPLIER * (closing - opening) - 2 * contracts * fees_per_contract
    strikes = [float(entry[osi].strike_price) for osi, _ in legs]
    terminal_spots = [0.0, *strikes, max(strikes) * 2]
    terminal_values = []
    for terminal_spot in terminal_spots:
        payoff = 0.0
        for osi, quantity in legs:
            leg = entry[osi]
            if "PUT" in str(leg.option_type).upper():
                payoff += quantity * max(float(leg.strike_price) - terminal_spot, 0.0)
            else:
                payoff += quantity * max(terminal_spot - float(leg.strike_price), 0.0)
        terminal_values.append(payoff)
    maximum_risk = OPTION_MULTIPLIER * (opening - min(terminal_values)) + contracts * fees_per_contract
    if maximum_risk <= 0:
        return None, None, "invalid_maximum_risk"
    return round(pnl, 2), pnl / maximum_risk, None


def score_outcome(session, event, outcome, fees_per_contract: float):
    if outcome.label_version != 2 or not event.is_opportunity:
        return
    entry = (session.query(GexSnapshot)
             .filter(GexSnapshot.symbol == event.symbol, GexSnapshot.timestamp <= event.emitted_at)
             .order_by(GexSnapshot.timestamp.desc()).first())
    exit_at = outcome.option_exit_at or outcome.horizon_at
    exit_snap = (session.query(GexSnapshot)
                 .filter(GexSnapshot.symbol == event.symbol, GexSnapshot.timestamp >= exit_at)
                 .order_by(GexSnapshot.timestamp.asc()).first())
    if (entry is None or exit_snap is None
            or event.emitted_at - entry.timestamp > timedelta(seconds=MAX_QUOTE_AGE_SECONDS)
            or exit_snap.timestamp > exit_at + timedelta(seconds=MAX_QUOTE_AGE_SECONDS)):
        outcome.option_exclusion = "missing_snapshot"
        return
    entry_rows = session.query(RawOptionGreek).filter(RawOptionGreek.snapshot_id == entry.id).all()
    exit_rows = session.query(RawOptionGreek).filter(RawOptionGreek.snapshot_id == exit_snap.id).all()
    legs = select_structure(entry_rows, event.bias, event.scenario_type or "", float(event.spot_price or 0), event.target)
    if not legs:
        outcome.option_exclusion = "missing_structure"
        return
    outcome.option_pnl, outcome.option_pnl_r, outcome.option_exclusion = mark_structure(
        legs, entry_rows, exit_rows, fees_per_contract)
    if outcome.option_pnl_r is None:
        return
    # Use only completed snapshots on the same session and in the same ET bucket.
    from edge_lab import _time_bucket
    from market_context import stored_timestamp_to_eastern
    session_date = stored_timestamp_to_eastern(event.emitted_at).date()
    bucket = _time_bucket(event.emitted_at)
    candidates = (session.query(GexSnapshot)
                  .filter(GexSnapshot.symbol == event.symbol,
                          GexSnapshot.timestamp < event.emitted_at)
                  .order_by(GexSnapshot.timestamp.asc()).all())
    candidates = [item for item in candidates
                  if stored_timestamp_to_eastern(item.timestamp).date() == session_date
                  and _time_bucket(item.timestamp) == bucket
                  and item.timestamp + timedelta(minutes=outcome.horizon_minutes) <= exit_snap.timestamp]
    if candidates:
        seed = int(hashlib.sha256(f"{event.id}:{outcome.horizon_minutes}".encode()).hexdigest()[:8], 16)
        random_entry = candidates[seed % len(candidates)]
        random_exit = (session.query(GexSnapshot)
                       .filter(GexSnapshot.symbol == event.symbol,
                               GexSnapshot.timestamp >= random_entry.timestamp + timedelta(minutes=outcome.horizon_minutes))
                       .order_by(GexSnapshot.timestamp.asc()).first())
        if random_exit and random_exit.timestamp <= random_entry.timestamp + timedelta(minutes=outcome.horizon_minutes,
                                                                                        seconds=MAX_QUOTE_AGE_SECONDS):
            random_entry_rows = session.query(RawOptionGreek).filter(RawOptionGreek.snapshot_id == random_entry.id).all()
            random_exit_rows = session.query(RawOptionGreek).filter(RawOptionGreek.snapshot_id == random_exit.id).all()
            _, outcome.random_baseline_pnl_r, _ = mark_structure(
                legs, random_entry_rows, random_exit_rows, fees_per_contract)
    prior = (session.query(GexSnapshot)
             .filter(GexSnapshot.symbol == event.symbol,
                     GexSnapshot.timestamp <= event.emitted_at - timedelta(minutes=15))
             .order_by(GexSnapshot.timestamp.desc()).first())
    if prior and prior.spot_price and event.spot_price != prior.spot_price:
        momentum_bias = "CALL" if event.spot_price > prior.spot_price else "PUT"
        distance = abs(float(event.target or event.spot_price) - float(event.spot_price))
        momentum_target = float(event.spot_price) + (distance if momentum_bias == "CALL" else -distance)
        momentum_legs = select_structure(entry_rows, momentum_bias, "MOMENTUM", float(event.spot_price), momentum_target)
        if momentum_legs:
            _, outcome.momentum_baseline_pnl_r, _ = mark_structure(
                momentum_legs, entry_rows, exit_rows, fees_per_contract)
