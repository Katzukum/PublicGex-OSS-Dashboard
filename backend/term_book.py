"""Observational term book. Nothing here changes a 0DTE snapshot or a decision."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from math import isfinite

from models import EstimatedFlowPoll, GexTermContract, GexTermSnapshot, OiDaily

OI_CAPTURE_HOUR_ET = 9
OI_CAPTURE_MINUTE_ET = 35


def select_expirations(listed: list[date], today: date, ahead: int = 4) -> list[date]:
    future = sorted({value for value in listed if value >= today})
    selected = future[:1 + ahead]
    monthly = next((value for value in future if value.weekday() == 4 and 15 <= value.day <= 21 and value not in selected), None)
    if monthly:
        selected.append(monthly)
    return selected


def _number(value):
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if isfinite(result) else None


def contract_gex(gamma, oi, spot, side) -> float | None:
    gamma, oi, spot = _number(gamma), _number(oi), _number(spot)
    if gamma is None or oi is None or spot is None or gamma < 0 or oi < 0 or spot <= 0:
        return None
    sign = -1 if "PUT" in str(side).upper() else 1
    return sign * gamma * oi * 100 * spot * spot * .01


def atm_straddle(rows: list[dict], spot: float) -> float | None:
    strikes = sorted({row["strike_price"] for row in rows})
    if not strikes:
        return None
    nearest = min(strikes, key=lambda strike: abs(strike - spot))
    prices = {}
    for row in rows:
        if row["strike_price"] != nearest:
            continue
        bid, ask = _number(row.get("bid")), _number(row.get("ask"))
        if bid is not None and ask is not None and 0 < bid <= ask:
            prices[row["option_type"]] = (bid + ask) / 2
    return prices.get("CALL") + prices.get("PUT") if "CALL" in prices and "PUT" in prices else None


def merged_strike_book(index_rows: list[dict], etf_rows: list[dict], index_spot: float, etf_spot: float) -> list[dict]:
    """Map ETF strikes to the nearest index grid, preserving dollar GEX."""
    grid = sorted({float(row["strike_price"]) for row in index_rows})
    if not grid or index_spot <= 0 or etf_spot <= 0:
        return []
    totals = defaultdict(float)
    for row in index_rows:
        totals[float(row["strike_price"])] += float(row["gex_value"])
    for row in etf_rows:
        mapped = float(row["strike_price"]) * index_spot / etf_spot
        strike = min(grid, key=lambda candidate: abs(candidate - mapped))
        totals[strike] += float(row["gex_value"])
    return [{"strike_price": strike, "gex_value": totals[strike]} for strike in sorted(totals)]


def oi_change(today: dict, yesterday: dict) -> dict:
    """Unknown for any contract absent on either session."""
    result = {}
    for osi, current in today.items():
        prior = yesterday.get(osi)
        if prior is None or current.get("open_interest") is None or prior.get("open_interest") is None:
            result[osi] = None
            continue
        delta = int(current["open_interest"]) - int(prior["open_interest"])
        result[osi] = {"change": delta, "prior_volume": prior.get("volume"),
                       "estimate": "opened" if delta > 0 else "closed" if delta < 0 else "unchanged"}
    return result


def persist_term(session, snapshot, expiration: date, rows: list[dict]):
    calls = sum(row["gex_value"] for row in rows if row["option_type"] == "CALL")
    puts = sum(row["gex_value"] for row in rows if row["option_type"] == "PUT")
    term = GexTermSnapshot(snapshot_id=snapshot.id, symbol=snapshot.symbol,
                           captured_at=snapshot.timestamp, expiration_date=expiration,
                           is_combined=False, call_gex=calls, put_gex=puts, net_gex=calls + puts,
                           atm_straddle=atm_straddle(rows, snapshot.spot_price), contract_count=len(rows))
    session.add(term)
    session.flush()
    session.bulk_save_objects([GexTermContract(term_snapshot_id=term.id, **row) for row in rows])
    return term


def persist_daily_oi(session, snapshot, rows: list[dict], eastern_now: datetime):
    if (eastern_now.hour, eastern_now.minute) < (OI_CAPTURE_HOUR_ET, OI_CAPTURE_MINUTE_ET):
        return
    existing = {osi for (osi,) in session.query(OiDaily.osi_symbol).filter(
        OiDaily.session_date == eastern_now.date(),
        OiDaily.osi_symbol.in_([row["osi_symbol"] for row in rows])).all()}
    for row in rows:
        if row.get("open_interest") is None or row["osi_symbol"] in existing:
            continue
        session.add(OiDaily(session_date=eastern_now.date(), symbol=snapshot.symbol,
                            osi_symbol=row["osi_symbol"], strike_price=row["strike_price"],
                            option_type=row["option_type"], open_interest=row["open_interest"],
                            volume=row.get("volume"), captured_at=snapshot.timestamp))
        existing.add(row["osi_symbol"])


def term_view(session, snapshot_id: int, symbol: str, spot: float, zero_day_levels: dict) -> dict:
    """Return separate 0DTE, combined and mapped-book observations."""
    from levels import build_levels
    terms = (session.query(GexTermSnapshot)
             .filter(GexTermSnapshot.snapshot_id == snapshot_id,
                     GexTermSnapshot.is_combined.is_(False))
             .order_by(GexTermSnapshot.expiration_date).all())
    if not terms:
        return {"status": "unavailable", "source": "Public.com", "zero_day_levels": zero_day_levels}
    profiles = {}
    all_rows = []
    for term in terms:
        rows = session.query(GexTermContract).filter(GexTermContract.term_snapshot_id == term.id).all()
        profile = [{"strike_price": row.strike_price, "option_type": row.option_type,
                    "gex_value": row.gex_value} for row in rows]
        profiles[str(term.expiration_date)] = {"call_gex": term.call_gex, "put_gex": term.put_gex,
                                               "net_gex": term.net_gex, "atm_straddle": term.atm_straddle,
                                               "contracts": term.contract_count}
        all_rows.extend(profile)
    per_strike = defaultdict(float)
    for row in all_rows:
        per_strike[row["strike_price"]] += row["gex_value"]
    # The combined crossing is an observational cumulative proxy, never a modeled sweep.
    running = 0.0
    crossings = []
    previous = None
    for strike in sorted(per_strike):
        before = running
        running += per_strike[strike]
        if previous is not None and before * running <= 0:
            crossings.append(strike)
        previous = strike
    proxy = min(crossings, key=lambda strike: abs(strike - spot)) if crossings else None
    first = next(iter(profiles.values()))
    combined = build_levels({"spot_price": spot, "flip_strike": proxy}, all_rows,
                            None, first["atm_straddle"])
    combined["pin_strike"] = zero_day_levels.get("pin_strike")
    combined["pin_gross_share_near_spot"] = zero_day_levels.get("pin_gross_share_near_spot")
    next_term = list(profiles.values())[1] if len(profiles) > 1 else None
    ratio = (first["atm_straddle"] / next_term["atm_straddle"]
             if next_term and first["atm_straddle"] is not None and next_term["atm_straddle"] else None)
    mapped = None
    etf_symbol = {"SPX": "SPY", "NDX": "QQQ"}.get(symbol)
    if etf_symbol:
        from models import GexSnapshot
        etf_snapshot = (session.query(GexSnapshot).filter(GexSnapshot.symbol == etf_symbol,
                                                        GexSnapshot.timestamp <= terms[0].captured_at)
                        .order_by(GexSnapshot.timestamp.desc()).first())
        if etf_snapshot and abs((terms[0].captured_at - etf_snapshot.timestamp).total_seconds()) <= 120:
            etf_terms = (session.query(GexTermSnapshot)
                         .filter(GexTermSnapshot.snapshot_id == etf_snapshot.id,
                                 GexTermSnapshot.is_combined.is_(False)).all())
            etf_rows = [row for term in etf_terms for row in session.query(GexTermContract)
                        .filter(GexTermContract.term_snapshot_id == term.id).all()]
            mapped = merged_strike_book(all_rows,
                                        [{"strike_price": row.strike_price, "gex_value": row.gex_value}
                                         for row in etf_rows], spot, etf_snapshot.spot_price)
    from market_context import stored_timestamp_to_eastern
    current_day = stored_timestamp_to_eastern(terms[0].captured_at).date()
    daily_rows = (session.query(OiDaily).filter(OiDaily.symbol == symbol,
                                               OiDaily.session_date <= current_day)
                  .order_by(OiDaily.session_date.desc()).all())
    available_days = sorted({row.session_date for row in daily_rows}, reverse=True)
    today_rows = {row.osi_symbol: {"open_interest": row.open_interest, "volume": row.volume,
                                   "strike_price": row.strike_price} for row in daily_rows
                  if available_days and row.session_date == available_days[0]}
    prior_rows = {row.osi_symbol: {"open_interest": row.open_interest, "volume": row.volume}
                  for row in daily_rows if len(available_days) > 1 and row.session_date == available_days[1]}
    changes = oi_change(today_rows, prior_rows) if prior_rows else {}
    oi_by_strike = defaultdict(lambda: {"change": 0, "contracts": 0, "prior_volume": 0})
    for osi, value in changes.items():
        if value is None:
            continue
        item = oi_by_strike[today_rows[osi]["strike_price"]]
        item["change"] += value["change"]
        item["contracts"] += 1
        item["prior_volume"] += value["prior_volume"] or 0
    flow_rows = session.query(EstimatedFlowPoll).filter(
        EstimatedFlowPoll.symbol == symbol, EstimatedFlowPoll.session_date == current_day).all()
    observed = sum(row.observed_volume or 0 for row in flow_rows)
    classified = sum(row.classified_volume or 0 for row in flow_rows)
    flow_values = [row.estimated_flow for row in flow_rows if row.estimated_flow is not None]
    flow = {"label": "estimated flow, low confidence",
            "estimated_flow": sum(flow_values) if flow_values else None,
            "coverage": classified / observed if observed else None,
            "classified_volume": classified, "observed_volume": observed}
    return {"status": "observational", "source": "Public.com", "expirations": profiles,
            "zero_day_levels": zero_day_levels, "combined_levels": combined,
            "merged_index_etf_strikes": mapped,
            "merged_etf_symbol": etf_symbol,
            "oi_change_estimate_by_strike": dict(oi_by_strike),
            "estimated_flow": flow,
            "zero_to_next_straddle_ratio": ratio,
            "decision_source": "0DTE until matched-distance holdout validation"}
