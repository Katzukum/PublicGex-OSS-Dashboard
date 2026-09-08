from datetime import datetime, timedelta, timezone

import pytest

from execution_quotes import candidate_from_persisted_quotes, get_strategy_market_quote


NOW = datetime(2026, 8, 14, 14, 30, tzinfo=timezone.utc)


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get_strategy_quote(self, request, account_id=None):
        self.calls.append((request, account_id))
        return self.response


def response(bid=1.0, ask=1.1, size=10, timestamp=NOW):
    return {
        "bid": bid,
        "ask": ask,
        "mark": (bid + ask) / 2 if bid is not None and ask is not None else None,
        "strategyLegs": [
            {
                "instrument": {"symbol": "SPX260814C06000000"},
                "quote": {
                    "bid": 2.0,
                    "ask": 2.1,
                    "bidSize": size,
                    "askSize": size,
                    "timestamp": timestamp.isoformat(),
                },
            }
        ],
    }


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ({"bid": None, "ask": 1.0, "mark": None}, "missing strategy market"),
        (response(bid=1.2, ask=1.0), "crossed strategy market"),
        (response(bid=0.0, ask=1.0), "zero bid"),
        (response(bid=0.5, ask=1.5), "strategy spread too wide"),
        (response(timestamp=NOW - timedelta(seconds=60)), "stale quote"),
    ],
)
def test_rejected_quote_paths(payload, reason):
    result = get_strategy_market_quote(FakeClient(payload), object(), now=NOW)
    assert result["status"] == "REJECTED"
    assert result["liquidity_grade"] == "REJECTED"
    assert reason in result["reason"]


@pytest.mark.parametrize(
    ("bid", "ask", "size", "grade"),
    [(1.0, 1.1, 10, "A"), (1.0, 1.2, 3, "B"), (1.0, 1.3, 1, "C")],
)
def test_liquidity_grades_and_conservative_economics(bid, ask, size, grade):
    client = FakeClient(response(bid=bid, ask=ask, size=size))
    result = get_strategy_market_quote(
        client,
        {"read_only": True},
        account_id="acct",
        now=NOW,
        structure_width=5,
        fees_per_contract=1.25,
    )
    assert client.calls == [({"read_only": True}, "acct")]
    assert result["status"] == "EXECUTABLE"
    assert result["liquidity_grade"] == grade
    assert result["entry_debit"] == ask
    assert result["exit_credit"] == bid
    assert result["max_loss"] == pytest.approx(ask * 100 + 1.25)
    assert result["max_reward"] == pytest.approx(500 - result["max_loss"])


def test_missing_timestamp_is_visible_but_not_fabricated():
    payload = response()
    payload["strategyLegs"][0]["quote"]["timestamp"] = None
    result = get_strategy_market_quote(FakeClient(payload), object(), now=NOW)
    assert result["age_seconds"] is None
    assert result["warnings"] == ["missing source timestamp"]


def _leg(strike, bid, ask, size=10, timestamp=NOW):
    return {
        "strike_price": strike,
        "option_type": "CALL",
        "bid": bid,
        "ask": ask,
        "bid_size": size,
        "ask_size": size,
        "bid_timestamp": timestamp,
        "ask_timestamp": timestamp,
    }


def test_persisted_spread_quote_is_executable_and_sized_conservatively():
    idea = {"status": "ready", "kind": "Debit Spread", "side": "CALL", "long_strike": 100, "short_strike": 105, "width": 5, "estimated_debit": 1.1}
    result = candidate_from_persisted_quotes(
        idea,
        [_leg(100, 2.0, 2.1), _leg(105, 1.0, 1.1)],
        symbol="SPX",
        now=NOW,
        maximum_risk=500,
        fees_per_contract=1,
    )
    assert result["status"] == "EXECUTABLE"
    assert result["quote"]["ask"] == 1.1
    assert result["quote"]["bid"] == 0.9
    assert result["max_loss_dollars"] == pytest.approx(111)
    assert result["contracts"] == 4
    assert result["settlement_type"] == "CASH"


def test_persisted_candidate_never_sizes_modeled_or_rejected_market():
    idea = {"status": "ready", "kind": "Debit Spread", "side": "CALL", "long_strike": 100, "short_strike": 105, "width": 5, "estimated_debit": 1.1}
    modeled = candidate_from_persisted_quotes(idea, [_leg(100, 2, 2.1)], symbol="SPX", now=NOW)
    assert modeled["status"] == "MODELED_ONLY"
    assert modeled["contracts"] is None

    rejected = candidate_from_persisted_quotes(
        idea,
        [_leg(100, 2, 2.1, timestamp=NOW - timedelta(minutes=1)), _leg(105, 1, 1.1, timestamp=NOW - timedelta(minutes=1))],
        symbol="SPX",
        now=NOW,
    )
    assert rejected["status"] == "REJECTED"
    assert rejected["contracts"] is None


def test_persisted_candidate_never_displays_negative_quote_age():
    idea = {"status": "ready", "kind": "Debit Spread", "side": "CALL", "long_strike": 100, "short_strike": 105, "width": 5, "estimated_debit": 1.1}
    small_clock_skew = candidate_from_persisted_quotes(
        idea,
        [_leg(100, 2, 2.1, timestamp=NOW + timedelta(seconds=2)), _leg(105, 1, 1.1, timestamp=NOW + timedelta(seconds=2))],
        symbol="SPY",
        now=NOW,
    )
    assert small_clock_skew["quote"]["age_seconds"] == 0

    future = candidate_from_persisted_quotes(
        idea,
        [_leg(100, 2, 2.1, timestamp=NOW + timedelta(seconds=30)), _leg(105, 1, 1.1, timestamp=NOW + timedelta(seconds=30))],
        symbol="SPY",
        now=NOW,
    )
    assert future["status"] == "REJECTED"
    assert future["quote"]["age_seconds"] == -30
    assert future["reason"] == "leg quote timestamp is in the future"
