"""Low-confidence trade-sign estimate from successive Public.com option quotes."""

from __future__ import annotations

from datetime import datetime

MAX_POLL_GAP_SECONDS = 120
OPTION_MULTIPLIER = 100


def classify_interval(previous: dict, current: dict, spot: float) -> dict:
    """Reject resets and stale gaps; missing trades remain unknown."""
    old_time, new_time = previous.get("timestamp"), current.get("timestamp")
    if not isinstance(old_time, datetime) or not isinstance(new_time, datetime):
        return {"status": "unknown", "classified_volume": 0, "observed_volume": 0, "estimated_flow": None}
    gap = (new_time - old_time).total_seconds()
    old_volume, new_volume = previous.get("volume"), current.get("volume")
    if gap <= 0 or gap > MAX_POLL_GAP_SECONDS or old_volume is None or new_volume is None or new_volume < old_volume:
        return {"status": "excluded", "classified_volume": 0, "observed_volume": 0, "estimated_flow": None}
    change = new_volume - old_volume
    bid, ask = previous.get("bid"), previous.get("ask")
    last, delta = current.get("last_price"), current.get("delta")
    if change == 0:
        return {"status": "no_change", "classified_volume": 0, "observed_volume": 0, "estimated_flow": 0.0}
    if last is None or bid is None or ask is None or delta is None or ask < bid:
        return {"status": "unclassified", "classified_volume": 0, "observed_volume": change, "estimated_flow": None}
    sign = 1 if last >= ask else -1 if last <= bid else 0
    return {"status": "classified" if sign else "unclassified",
            "classified_volume": change if sign else 0, "observed_volume": change,
            "estimated_flow": sign * change * delta * OPTION_MULTIPLIER * spot if sign else None}


def summarize_intervals(intervals: list[dict]) -> dict:
    observed = sum(item["observed_volume"] for item in intervals)
    classified = sum(item["classified_volume"] for item in intervals)
    values = [item["estimated_flow"] for item in intervals if item["estimated_flow"] is not None]
    return {"label": "estimated flow, low confidence", "estimated_flow": sum(values) if values else None,
            "coverage": classified / observed if observed else None,
            "classified_volume": classified, "observed_volume": observed}


class FlowPoller:
    """Four-second observational poller, enabled only after quote capability is measured."""

    POLL_SECONDS = 4
    MAX_CONTRACTS_PER_SYMBOL = 100

    def __init__(self):
        self.previous = {}

    def poll(self, client, session, symbols, account_id, rate_limiter):
        from market_context import stored_timestamp_to_eastern
        from models import EstimatedFlowPoll, GexSnapshot, GexTermSnapshot, RawOptionGreek
        from publicData import get_option_quotes_batch, get_val

        captured = datetime.now()
        for symbol in symbols:
            snapshot = (session.query(GexSnapshot).filter(GexSnapshot.symbol == symbol)
                        .order_by(GexSnapshot.timestamp.desc()).first())
            if not snapshot or snapshot.spot_price is None:
                continue
            term = (session.query(GexTermSnapshot).filter(GexTermSnapshot.snapshot_id == snapshot.id,
                                                         GexTermSnapshot.is_combined.is_(False))
                    .order_by(GexTermSnapshot.expiration_date).first())
            if not term or not term.atm_straddle or term.atm_straddle <= 0:
                continue
            rows = session.query(RawOptionGreek).filter(RawOptionGreek.snapshot_id == snapshot.id).all()
            eligible = [row for row in rows if row.osi_symbol and row.volume is not None
                        and abs(row.strike_price - snapshot.spot_price) <= term.atm_straddle / 2]
            eligible.sort(key=lambda row: abs(row.strike_price - snapshot.spot_price))
            eligible = eligible[:self.MAX_CONTRACTS_PER_SYMBOL]
            if not eligible:
                continue
            quotes = get_option_quotes_batch(client, [row.osi_symbol for row in eligible], account_id, rate_limiter)
            intervals = []
            for row in eligible:
                quote = quotes.get(row.osi_symbol)
                if quote is None:
                    continue
                current = {"timestamp": captured, "volume": get_val(quote, ["volume"]),
                           "last_price": get_val(quote, ["last"]),
                           "delta": row.delta if row.delta is not None and row.delta != 0 else None,
                           "bid": get_val(quote, ["bid"]), "ask": get_val(quote, ["ask"])}
                key = (symbol, row.osi_symbol)
                previous = self.previous.get(key)
                if previous:
                    intervals.append(classify_interval(previous, current, snapshot.spot_price))
                self.previous[key] = current
            summary = summarize_intervals(intervals)
            if not intervals:
                continue
            session.add(EstimatedFlowPoll(symbol=symbol,
                                          session_date=stored_timestamp_to_eastern(captured).date(),
                                          captured_at=captured,
                                          estimated_flow=summary["estimated_flow"],
                                          classified_volume=summary["classified_volume"],
                                          observed_volume=summary["observed_volume"],
                                          coverage=summary["coverage"], label=summary["label"]))
        session.commit()
