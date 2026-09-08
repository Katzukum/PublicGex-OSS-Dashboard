import argparse
import csv
import sqlite3
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from math import exp, pi
from pathlib import Path
from statistics import NormalDist
from typing import Iterable, Optional
from option_math import charm_exposure as _shared_charm_exposure, charm_per_year as _shared_charm_per_year, infer_total_vol


DEFAULT_DB_PATH = Path("gex_data.db")
DEFAULT_OUTPUT_PATH = Path("gamma_butterfly_backtest.csv")
DEFAULT_SYMBOL = "SPX"
DEFAULT_START_TIME = "14:00"
DEFAULT_END_TIME = "15:00"
DEFAULT_ENTRY_TIME = "15:00"
DEFAULT_SETTLEMENT_TIME = "16:00"
DEFAULT_PIT_WINDOW = 6
DEFAULT_PIT_WALL_COUNT = 3
SECONDS_PER_YEAR = 365 * 24 * 60 * 60

CSV_FIELDS = [
    "trade_date",
    "symbol",
    "center_method",
    "hybrid_score_at_center",
    "hybrid_gex_weight",
    "hybrid_charm_weight",
    "pit_score_at_center",
    "pit_window",
    "pit_wall_count",
    "pit_left_wall_strike",
    "pit_left_wall_gex",
    "pit_right_wall_strike",
    "pit_right_wall_gex",
    "entry_snapshot_id",
    "entry_time",
    "entry_spot",
    "expiration_date",
    "center_strike",
    "lower_strike",
    "upper_strike",
    "width",
    "net_gex_at_center",
    "call_gex_at_center",
    "put_gex_at_center",
    "net_charm_at_center",
    "call_charm_at_center",
    "put_charm_at_center",
    "pricing_side",
    "estimated_debit_points",
    "estimated_debit_dollars",
    "lower_breakeven",
    "upper_breakeven",
    "settlement_snapshot_id",
    "settlement_time",
    "settlement_spot",
    "settlement_payoff_points",
    "pnl_points",
    "pnl_dollars",
    "return_on_debit",
    "settled_in_tent",
    "settled_profitable",
    "hit_profit_zone",
    "hit_center",
    "hit_center_tolerance",
    "min_distance_to_center",
    "min_distance_time",
    "max_intrinsic_payoff_points",
    "max_intrinsic_pnl_points",
    "path_low",
    "path_high",
    "path_rows",
]

NORMAL = NormalDist()


@dataclass(frozen=True)
class Snapshot:
    id: int
    timestamp: datetime
    symbol: str
    spot_price: float


@dataclass(frozen=True)
class PricedFly:
    side: str
    debit: float


def _parse_datetime(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text).replace(tzinfo=None)
    except ValueError:
        return None


def _parse_time(value: str) -> time:
    text = str(value or "").strip()
    for fmt in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Invalid time {value!r}; use HH:MM or HH:MM:SS")


def _connect_readonly(db_path: Path):
    return sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)


def _snapshot_from_row(row: sqlite3.Row) -> Optional[Snapshot]:
    timestamp = _parse_datetime(row["timestamp"])
    spot = float(row["spot_price"] or 0)
    if not timestamp or spot <= 0:
        return None
    return Snapshot(
        id=int(row["id"]),
        timestamp=timestamp,
        symbol=str(row["symbol"]).upper(),
        spot_price=spot,
    )


def _snapshot_rows(conn, symbol: str) -> list[Snapshot]:
    query = """
        SELECT id, timestamp, symbol, spot_price
        FROM gex_snapshots
        WHERE symbol = ?
          AND spot_price IS NOT NULL
          AND spot_price > 0
        ORDER BY timestamp ASC, id ASC
    """
    rows = []
    for row in conn.execute(query, (symbol.upper(),)):
        snapshot = _snapshot_from_row(row)
        if snapshot:
            rows.append(snapshot)
    return rows


def _candidate_entry_snapshots(
    snapshots: Iterable[Snapshot],
    start_time: time,
    end_time: time,
) -> list[Snapshot]:
    return [
        snapshot
        for snapshot in snapshots
        if start_time <= snapshot.timestamp.time() <= end_time
    ]


def _seconds_from_midnight(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _seconds_between_times(left: time, right: time) -> int:
    return abs(_seconds_from_midnight(left) - _seconds_from_midnight(right))


def _daily_entry_snapshots(
    candidates: Iterable[Snapshot],
    entry_time: time,
    max_entry_gap_minutes: int,
) -> list[Snapshot]:
    by_date: dict[object, list[Snapshot]] = {}
    for snapshot in candidates:
        by_date.setdefault(snapshot.timestamp.date(), []).append(snapshot)

    selected = []
    max_gap_seconds = max_entry_gap_minutes * 60
    for rows in by_date.values():
        best = min(
            rows,
            key=lambda row: (
                _seconds_between_times(row.timestamp.time(), entry_time),
                row.timestamp,
            ),
        )
        if _seconds_between_times(best.timestamp.time(), entry_time) <= max_gap_seconds:
            selected.append(best)

    return sorted(selected, key=lambda row: row.timestamp)


def _raw_option_rows(conn, snapshot_id: int) -> list[dict]:
    query = """
        SELECT
            expiration_date,
            osi_symbol,
            strike_price,
            option_type,
            delta,
            gamma,
            open_interest,
            underlying_price,
            gex_value
        FROM raw_option_greeks
        WHERE snapshot_id = ?
          AND strike_price IS NOT NULL
        ORDER BY strike_price ASC, option_type ASC
    """
    return [dict(row) for row in conn.execute(query, (snapshot_id,))]


def _strike_key(value: float) -> float:
    return round(float(value), 6)


def _option_side(row: dict) -> Optional[str]:
    option_type = str(row.get("option_type") or "").upper()
    if "CALL" in option_type:
        return "CALL"
    if "PUT" in option_type:
        return "PUT"
    return None


def _years_to_expiration(row: dict, timestamp: datetime, settlement_time: time) -> float:
    expiration_text = row.get("expiration_date")
    try:
        expiration_date = datetime.fromisoformat(str(expiration_text)).date()
    except (TypeError, ValueError):
        expiration_date = timestamp.date()

    expiration_at = datetime.combine(expiration_date, settlement_time)
    seconds = max((expiration_at - timestamp).total_seconds(), 1)
    return seconds / SECONDS_PER_YEAR


def _d1_total_vol(row: dict, spot: float, side: str) -> Optional[tuple[float, float]]:
    implied, reason = infer_total_vol({**row, "option_type": side, "underlying_price": spot}, spot)
    return None if reason else (implied["d1"], implied["total_vol"])


def _charm_per_year(row: dict, spot: float, timestamp: datetime, settlement_time: time) -> Optional[float]:
    return _shared_charm_per_year(row, spot, timestamp, settlement_time)


def _charm_exposure(row: dict, spot: float, timestamp: datetime, settlement_time: time) -> float:
    return _shared_charm_exposure(row, spot, timestamp, settlement_time) or 0.0


def _strike_summary(
    raw_rows: Iterable[dict],
    spot: Optional[float] = None,
    timestamp: Optional[datetime] = None,
    settlement_time: Optional[time] = None,
) -> dict[float, dict]:
    summary: dict[float, dict] = {}
    for row in raw_rows:
        strike = _strike_key(row.get("strike_price") or 0)
        if strike <= 0:
            continue

        side = _option_side(row)
        gex = float(row.get("gex_value") or 0)
        charm = (
            _charm_exposure(row, float(spot), timestamp, settlement_time)
            if spot and timestamp and settlement_time
            else 0.0
        )
        data = summary.setdefault(
            strike,
            {
                "strike": strike,
                "net_gex": 0.0,
                "call_gex": 0.0,
                "put_gex": 0.0,
                "net_charm": 0.0,
                "call_charm": 0.0,
                "put_charm": 0.0,
                "hybrid_score": 0.0,
                "pit_score": 0.0,
                "pit_left_wall_strike": "",
                "pit_left_wall_gex": "",
                "pit_right_wall_strike": "",
                "pit_right_wall_gex": "",
                "expiration_dates": set(),
            },
        )
        data["net_gex"] += gex
        data["net_charm"] += charm
        if side == "PUT":
            data["put_gex"] += gex
            data["put_charm"] += charm
        elif side == "CALL":
            data["call_gex"] += gex
            data["call_charm"] += charm

        expiration_date = row.get("expiration_date")
        if expiration_date:
            data["expiration_dates"].add(str(expiration_date))

    return summary


def _dominant_gamma_strike(summary: dict[float, dict]) -> Optional[dict]:
    if not summary:
        return None
    return max(summary.values(), key=lambda item: abs(item["net_gex"]))


def _normalized_weights(gex_weight: float, charm_weight: float) -> tuple[float, float]:
    gex_weight = max(float(gex_weight), 0.0)
    charm_weight = max(float(charm_weight), 0.0)
    total = gex_weight + charm_weight
    if total <= 0:
        raise ValueError("At least one hybrid weight must be positive")
    return gex_weight / total, charm_weight / total


def _score_hybrid_levels(
    summary: dict[float, dict],
    gex_weight: float,
    charm_weight: float,
) -> None:
    gex_weight, charm_weight = _normalized_weights(gex_weight, charm_weight)
    max_gex = max((abs(item["net_gex"]) for item in summary.values()), default=0.0)
    max_charm = max((abs(item["net_charm"]) for item in summary.values()), default=0.0)

    for item in summary.values():
        gex_component = abs(item["net_gex"]) / max_gex if max_gex else 0.0
        charm_component = abs(item["net_charm"]) / max_charm if max_charm else 0.0
        item["hybrid_score"] = gex_weight * gex_component + charm_weight * charm_component


def _score_gamma_pit_levels(summary: dict[float, dict], pit_window: int) -> None:
    rows = sorted(summary.values(), key=lambda item: item["strike"])
    if len(rows) < 3:
        return

    pit_window = max(int(pit_window), 1)
    max_abs_gex = max((abs(item["net_gex"]) for item in rows), default=0.0)
    if max_abs_gex <= 0:
        return

    for index, center in enumerate(rows):
        left_rows = rows[max(0, index - pit_window):index]
        right_rows = rows[index + 1:index + 1 + pit_window]
        if not left_rows or not right_rows:
            continue

        left_wall = max(left_rows, key=lambda item: abs(item["net_gex"]))
        right_wall = max(right_rows, key=lambda item: abs(item["net_gex"]))
        center_abs = abs(center["net_gex"])
        left_abs = abs(left_wall["net_gex"])
        right_abs = abs(right_wall["net_gex"])
        if left_abs <= center_abs or right_abs <= center_abs:
            continue

        weaker_wall = min(left_abs, right_abs)
        stronger_wall = max(left_abs, right_abs)
        depth = (weaker_wall - center_abs) / max_abs_gex
        balance = weaker_wall / stronger_wall if stronger_wall else 0.0
        center["pit_score"] = depth * balance
        center["pit_left_wall_strike"] = left_wall["strike"]
        center["pit_left_wall_gex"] = left_wall["net_gex"]
        center["pit_right_wall_strike"] = right_wall["strike"]
        center["pit_right_wall_gex"] = right_wall["net_gex"]


def _score_gamma_wall_pit_levels(summary: dict[float, dict], pit_wall_count: int) -> None:
    rows = sorted(summary.values(), key=lambda item: item["strike"])
    if len(rows) < 3:
        return

    max_abs_gex = max((abs(item["net_gex"]) for item in rows), default=0.0)
    if max_abs_gex <= 0:
        return

    by_strike = {item["strike"]: item for item in rows}
    major_walls = sorted(
        rows,
        key=lambda item: abs(item["net_gex"]),
        reverse=True,
    )[: max(int(pit_wall_count), 2)]
    major_walls = sorted(major_walls, key=lambda item: item["strike"])

    for left_wall, right_wall in zip(major_walls, major_walls[1:]):
        between = [
            item
            for item in rows
            if left_wall["strike"] < item["strike"] < right_wall["strike"]
        ]
        if not between:
            continue

        center = min(between, key=lambda item: abs(item["net_gex"]))
        center_abs = abs(center["net_gex"])
        left_abs = abs(left_wall["net_gex"])
        right_abs = abs(right_wall["net_gex"])
        weaker_wall = min(left_abs, right_abs)
        stronger_wall = max(left_abs, right_abs)
        if weaker_wall <= center_abs:
            continue

        depth = (weaker_wall - center_abs) / max_abs_gex
        balance = weaker_wall / stronger_wall if stronger_wall else 0.0
        score = depth * balance
        if score <= center.get("pit_score", 0.0):
            continue

        center["pit_score"] = score
        center["pit_left_wall_strike"] = left_wall["strike"]
        center["pit_left_wall_gex"] = left_wall["net_gex"]
        center["pit_right_wall_strike"] = right_wall["strike"]
        center["pit_right_wall_gex"] = right_wall["net_gex"]

        # Keep references in the original summary map fresh for callers that
        # access by strike rather than via the row object.
        by_strike[center["strike"]].update(center)


def _center_strike(summary: dict[float, dict], center_method: str) -> Optional[dict]:
    if center_method == "gex":
        return _dominant_gamma_strike(summary)
    if center_method == "hybrid":
        candidates = [item for item in summary.values() if item.get("hybrid_score")]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item["hybrid_score"])
    if center_method == "gamma-pit":
        candidates = [item for item in summary.values() if item.get("pit_score")]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item["pit_score"])
    if center_method == "gamma-pit-walls":
        candidates = [item for item in summary.values() if item.get("pit_score")]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item["pit_score"])

    metric_by_method = {
        "charm": "net_charm",
        "call-charm": "call_charm",
        "put-charm": "put_charm",
    }
    metric = metric_by_method.get(center_method)
    if metric is None:
        raise ValueError(f"Unsupported center method: {center_method}")

    candidates = [item for item in summary.values() if item.get(metric)]
    if not candidates:
        return None
    return max(candidates, key=lambda item: abs(item[metric]))


def _available_symmetric_width(
    center: float,
    strikes: Iterable[float],
    requested_width: Optional[float],
) -> Optional[float]:
    strike_set = {_strike_key(strike) for strike in strikes}
    center = _strike_key(center)

    if requested_width is not None:
        width = _strike_key(requested_width)
        if _strike_key(center - width) in strike_set and _strike_key(center + width) in strike_set:
            return width
        return None

    widths = sorted(
        {
            abs(strike - center)
            for strike in strike_set
            if strike != center
        }
    )
    for width in widths:
        if _strike_key(center - width) in strike_set and _strike_key(center + width) in strike_set:
            return width
    return None


def _rows_by_strike_and_side(raw_rows: Iterable[dict]) -> dict[tuple[float, str], dict]:
    lookup = {}
    for row in raw_rows:
        strike = _strike_key(row.get("strike_price") or 0)
        side = _option_side(row)
        if side:
            lookup[(strike, side)] = row
    return lookup


def _normal_pdf(value: float) -> float:
    return (1 / (2 * pi) ** 0.5) * exp(-0.5 * value * value)


def _greek_implied_option_price(row: dict, spot: float, side: str) -> Optional[float]:
    if spot <= 0:
        return None

    try:
        strike = float(row.get("strike_price") or 0)
    except (TypeError, ValueError):
        return None

    if strike <= 0:
        return None

    implied = _d1_total_vol(row, spot, side)
    if implied is None:
        return None

    d1, total_vol = implied
    d2 = d1 - total_vol
    if side == "CALL":
        return spot * NORMAL.cdf(d1) - strike * NORMAL.cdf(d2)
    return strike * NORMAL.cdf(-d2) - spot * NORMAL.cdf(-d1)


def _intrinsic_option_price(row: dict, spot: float, side: str) -> Optional[float]:
    try:
        strike = float(row.get("strike_price") or 0)
    except (TypeError, ValueError):
        return None

    if side == "CALL":
        return max(spot - strike, 0)
    if side == "PUT":
        return max(strike - spot, 0)
    return None


def _leg_price(row: dict, spot: float, side: str, price_method: str) -> Optional[float]:
    if price_method == "greeks":
        return _greek_implied_option_price(row, spot, side)
    if price_method == "intrinsic":
        return _intrinsic_option_price(row, spot, side)
    raise ValueError(f"Unsupported price method: {price_method}")


def _priced_fly_for_side(
    lookup: dict[tuple[float, str], dict],
    lower: float,
    center: float,
    upper: float,
    spot: float,
    side: str,
    price_method: str,
    min_debit: float,
) -> Optional[PricedFly]:
    rows = [
        lookup.get((_strike_key(lower), side)),
        lookup.get((_strike_key(center), side)),
        lookup.get((_strike_key(upper), side)),
    ]
    if any(row is None for row in rows):
        return None

    lower_price = _leg_price(rows[0], spot, side, price_method)
    center_price = _leg_price(rows[1], spot, side, price_method)
    upper_price = _leg_price(rows[2], spot, side, price_method)
    if None in (lower_price, center_price, upper_price):
        return None

    debit = lower_price - 2 * center_price + upper_price
    if debit <= min_debit:
        return None
    return PricedFly(side=side, debit=debit)


def _price_fly(
    raw_rows: Iterable[dict],
    lower: float,
    center: float,
    upper: float,
    spot: float,
    side: str,
    price_method: str,
    min_debit: float,
) -> Optional[PricedFly]:
    lookup = _rows_by_strike_and_side(raw_rows)
    if side == "AUTO":
        if center > spot:
            requested_sides = ("CALL",)
        elif center < spot:
            requested_sides = ("PUT",)
        else:
            requested_sides = ("CALL", "PUT")
    else:
        requested_sides = (side,)
    priced = [
        item
        for item in (
            _priced_fly_for_side(
                lookup,
                lower,
                center,
                upper,
                spot,
                requested_side,
                price_method,
                min_debit,
            )
            for requested_side in requested_sides
        )
        if item is not None
    ]

    if not priced:
        return None
    if len(priced) == 1:
        return priced[0]

    average_debit = sum(item.debit for item in priced) / len(priced)
    return PricedFly(
        side="+".join(item.side for item in priced) + "_AVG",
        debit=average_debit,
    )


def _butterfly_payoff(spot: float, lower: float, center: float, upper: float) -> float:
    return (
        max(spot - lower, 0)
        - 2 * max(spot - center, 0)
        + max(spot - upper, 0)
    )


def _target_snapshot(
    snapshots: Iterable[Snapshot],
    trade_date,
    target_time: time,
    max_gap_minutes: int,
) -> Optional[Snapshot]:
    target_dt = datetime.combine(trade_date, target_time)
    max_gap = timedelta(minutes=max_gap_minutes)
    candidates = [
        snapshot
        for snapshot in snapshots
        if snapshot.timestamp.date() == trade_date
        and abs(snapshot.timestamp - target_dt) <= max_gap
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda row: (abs(row.timestamp - target_dt), row.timestamp))


def _path_snapshots(
    snapshots: Iterable[Snapshot],
    start_at: datetime,
    end_at: datetime,
) -> list[Snapshot]:
    return [
        snapshot
        for snapshot in snapshots
        if start_at <= snapshot.timestamp <= end_at
    ]


def _build_trade_row(
    conn,
    entry: Snapshot,
    all_snapshots: list[Snapshot],
    settlement_time: time,
    max_settlement_gap_minutes: int,
    requested_width: Optional[float],
    center_method: str,
    gex_weight: float,
    charm_weight: float,
    pit_window: int,
    pit_wall_count: int,
    side: str,
    price_method: str,
    min_debit: float,
    hit_center_tolerance: float,
) -> Optional[dict]:
    raw_rows = _raw_option_rows(conn, entry.id)
    summary = _strike_summary(
        raw_rows,
        spot=entry.spot_price,
        timestamp=entry.timestamp,
        settlement_time=settlement_time,
    )
    _score_hybrid_levels(summary, gex_weight, charm_weight)
    if center_method == "gamma-pit-walls":
        _score_gamma_wall_pit_levels(summary, pit_wall_count)
    else:
        _score_gamma_pit_levels(summary, pit_window)
    center_data = _center_strike(summary, center_method)
    if not center_data:
        return None

    center = _strike_key(center_data["strike"])
    width = _available_symmetric_width(center, summary.keys(), requested_width)
    if width is None:
        return None

    lower = _strike_key(center - width)
    upper = _strike_key(center + width)
    priced_fly = _price_fly(
        raw_rows,
        lower,
        center,
        upper,
        entry.spot_price,
        side=side,
        price_method=price_method,
        min_debit=min_debit,
    )
    if priced_fly is None:
        return None

    settlement = _target_snapshot(
        all_snapshots,
        entry.timestamp.date(),
        settlement_time,
        max_settlement_gap_minutes,
    )
    if settlement is None or settlement.timestamp <= entry.timestamp:
        return None

    path = _path_snapshots(all_snapshots, entry.timestamp, settlement.timestamp)
    if not path:
        return None

    debit = priced_fly.debit
    lower_breakeven = lower + debit
    upper_breakeven = upper - debit
    settlement_payoff = _butterfly_payoff(settlement.spot_price, lower, center, upper)
    pnl_points = settlement_payoff - debit

    path_spots = [snapshot.spot_price for snapshot in path]
    path_payoffs = [_butterfly_payoff(spot, lower, center, upper) for spot in path_spots]
    min_distance_snapshot = min(path, key=lambda row: abs(row.spot_price - center))
    max_intrinsic_payoff = max(path_payoffs)

    expiration_dates = sorted(center_data.get("expiration_dates") or [])
    expiration_date = expiration_dates[0] if expiration_dates else ""

    return {
        "trade_date": entry.timestamp.date().isoformat(),
        "symbol": entry.symbol,
        "center_method": center_method,
        "hybrid_score_at_center": center_data["hybrid_score"],
        "hybrid_gex_weight": _normalized_weights(gex_weight, charm_weight)[0],
        "hybrid_charm_weight": _normalized_weights(gex_weight, charm_weight)[1],
        "pit_score_at_center": center_data["pit_score"],
        "pit_window": pit_window,
        "pit_wall_count": pit_wall_count,
        "pit_left_wall_strike": center_data["pit_left_wall_strike"],
        "pit_left_wall_gex": center_data["pit_left_wall_gex"],
        "pit_right_wall_strike": center_data["pit_right_wall_strike"],
        "pit_right_wall_gex": center_data["pit_right_wall_gex"],
        "entry_snapshot_id": entry.id,
        "entry_time": entry.timestamp.isoformat(sep=" "),
        "entry_spot": entry.spot_price,
        "expiration_date": expiration_date,
        "center_strike": center,
        "lower_strike": lower,
        "upper_strike": upper,
        "width": width,
        "net_gex_at_center": center_data["net_gex"],
        "call_gex_at_center": center_data["call_gex"],
        "put_gex_at_center": center_data["put_gex"],
        "net_charm_at_center": center_data["net_charm"],
        "call_charm_at_center": center_data["call_charm"],
        "put_charm_at_center": center_data["put_charm"],
        "pricing_side": priced_fly.side,
        "estimated_debit_points": debit,
        "estimated_debit_dollars": debit * 100,
        "lower_breakeven": lower_breakeven,
        "upper_breakeven": upper_breakeven,
        "settlement_snapshot_id": settlement.id,
        "settlement_time": settlement.timestamp.isoformat(sep=" "),
        "settlement_spot": settlement.spot_price,
        "settlement_payoff_points": settlement_payoff,
        "pnl_points": pnl_points,
        "pnl_dollars": pnl_points * 100,
        "return_on_debit": pnl_points / debit if debit else "",
        "settled_in_tent": lower < settlement.spot_price < upper,
        "settled_profitable": pnl_points > 0,
        "hit_profit_zone": any(lower_breakeven <= spot <= upper_breakeven for spot in path_spots),
        "hit_center": abs(min_distance_snapshot.spot_price - center) <= hit_center_tolerance,
        "hit_center_tolerance": hit_center_tolerance,
        "min_distance_to_center": abs(min_distance_snapshot.spot_price - center),
        "min_distance_time": min_distance_snapshot.timestamp.isoformat(sep=" "),
        "max_intrinsic_payoff_points": max_intrinsic_payoff,
        "max_intrinsic_pnl_points": max_intrinsic_payoff - debit,
        "path_low": min(path_spots),
        "path_high": max(path_spots),
        "path_rows": len(path),
    }


def iter_gamma_butterfly_backtest(
    db_path=DEFAULT_DB_PATH,
    symbol=DEFAULT_SYMBOL,
    start_time=DEFAULT_START_TIME,
    end_time=DEFAULT_END_TIME,
    entry_time=DEFAULT_ENTRY_TIME,
    settlement_time=DEFAULT_SETTLEMENT_TIME,
    width: Optional[float] = None,
    center_method="gex",
    gex_weight=0.7,
    charm_weight=0.3,
    pit_window=DEFAULT_PIT_WINDOW,
    pit_wall_count=DEFAULT_PIT_WALL_COUNT,
    side="AUTO",
    price_method="greeks",
    one_per_day=True,
    max_entry_gap_minutes=10,
    max_settlement_gap_minutes=10,
    min_debit=0.0,
    hit_center_tolerance=0.5,
):
    db_path = Path(db_path)
    if not db_path.exists():
        raise FileNotFoundError(f"Database not found: {db_path}")

    start_time_obj = _parse_time(start_time)
    end_time_obj = _parse_time(end_time)
    entry_time_obj = _parse_time(entry_time)
    settlement_time_obj = _parse_time(settlement_time)
    symbol = str(symbol).upper()
    center_method = str(center_method).lower()
    side = str(side).upper()
    price_method = str(price_method).lower()

    if center_method not in {
        "gex",
        "charm",
        "call-charm",
        "put-charm",
        "hybrid",
        "gamma-pit",
        "gamma-pit-walls",
    }:
        raise ValueError(
            "--center-method must be gex, charm, call-charm, put-charm, hybrid, gamma-pit, or gamma-pit-walls"
        )
    _normalized_weights(gex_weight, charm_weight)
    if pit_window < 1:
        raise ValueError("--pit-window must be at least 1")
    if pit_wall_count < 2:
        raise ValueError("--pit-wall-count must be at least 2")
    if side not in {"AUTO", "CALL", "PUT"}:
        raise ValueError("--side must be auto, call, or put")
    if price_method not in {"greeks", "intrinsic"}:
        raise ValueError("--price-method must be greeks or intrinsic")

    conn = _connect_readonly(db_path)
    try:
        conn.row_factory = sqlite3.Row
        all_snapshots = _snapshot_rows(conn, symbol)
        candidates = _candidate_entry_snapshots(all_snapshots, start_time_obj, end_time_obj)
        if one_per_day:
            entries = _daily_entry_snapshots(candidates, entry_time_obj, max_entry_gap_minutes)
        else:
            entries = candidates

        for entry in entries:
            row = _build_trade_row(
                conn=conn,
                entry=entry,
                all_snapshots=all_snapshots,
                settlement_time=settlement_time_obj,
                max_settlement_gap_minutes=max_settlement_gap_minutes,
                requested_width=width,
                center_method=center_method,
                gex_weight=gex_weight,
                charm_weight=charm_weight,
                pit_window=pit_window,
                pit_wall_count=pit_wall_count,
                side=side,
                price_method=price_method,
                min_debit=min_debit,
                hit_center_tolerance=hit_center_tolerance,
            )
            if row:
                yield row
    finally:
        conn.close()


def _write_csv(rows: list[dict], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _print_summary(rows: list[dict], output_path: Path) -> None:
    trades = len(rows)
    if trades == 0:
        print(f"No eligible trades found. Wrote empty CSV to {output_path}")
        return

    settled_wins = sum(1 for row in rows if row["settled_profitable"])
    profit_zone_hits = sum(1 for row in rows if row["hit_profit_zone"])
    center_hits = sum(1 for row in rows if row["hit_center"])
    total_pnl = sum(float(row["pnl_points"]) for row in rows)
    avg_pnl = total_pnl / trades
    avg_debit = sum(float(row["estimated_debit_points"]) for row in rows) / trades
    total_dollars = total_pnl * 100

    print(f"Wrote {trades} trades to {output_path}")
    print(f"Settled profitable: {settled_wins}/{trades} ({_percent(settled_wins / trades)})")
    print(f"Hit profit zone intraday: {profit_zone_hits}/{trades} ({_percent(profit_zone_hits / trades)})")
    print(f"Hit center intraday: {center_hits}/{trades} ({_percent(center_hits / trades)})")
    print(f"Average debit: {avg_debit:.2f} points")
    print(f"Total P/L: {total_pnl:+.2f} points ({total_dollars:+.0f} dollars per 1-lot)")
    print(f"Average P/L: {avg_pnl:+.2f} points per trade")


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "Backtest 0DTE butterflies around the largest absolute GEX strike using "
            "stored snapshots and Greek-implied theoretical leg prices."
        )
    )
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to gex_data.db")
    parser.add_argument("--output", default=str(DEFAULT_OUTPUT_PATH), help="CSV output path")
    parser.add_argument("--symbol", default=DEFAULT_SYMBOL, help="Symbol to test, default SPX")
    parser.add_argument("--start-time", default=DEFAULT_START_TIME, help="Entry window start, HH:MM")
    parser.add_argument("--end-time", default=DEFAULT_END_TIME, help="Entry window end, HH:MM")
    parser.add_argument("--entry-time", default=DEFAULT_ENTRY_TIME, help="Daily entry target, HH:MM")
    parser.add_argument("--settlement-time", default=DEFAULT_SETTLEMENT_TIME, help="Settlement target, HH:MM")
    parser.add_argument("--width", type=float, default=None, help="Butterfly wing width; default auto")
    parser.add_argument(
        "--center-method",
        choices=("gex", "charm", "call-charm", "put-charm", "hybrid", "gamma-pit", "gamma-pit-walls"),
        default="gex",
        help="Choose butterfly center from GEX, charm, hybrid, a local pit, or a pit between major gamma walls",
    )
    parser.add_argument(
        "--gex-weight",
        type=float,
        default=0.7,
        help="Hybrid GEX weight, normalized with --charm-weight",
    )
    parser.add_argument(
        "--charm-weight",
        type=float,
        default=0.3,
        help="Hybrid charm weight, normalized with --gex-weight",
    )
    parser.add_argument(
        "--pit-window",
        type=int,
        default=DEFAULT_PIT_WINDOW,
        help="Number of strikes to scan on each side for gamma-pit walls",
    )
    parser.add_argument(
        "--pit-wall-count",
        type=int,
        default=DEFAULT_PIT_WALL_COUNT,
        help="Number of largest absolute GEX strikes used by gamma-pit-walls",
    )
    parser.add_argument(
        "--side",
        choices=("auto", "call", "put"),
        default="auto",
        help=(
            "Price with call fly, put fly, or auto-select calls when center is "
            "above spot and puts when center is below spot"
        ),
    )
    parser.add_argument(
        "--price-method",
        choices=("greeks", "intrinsic"),
        default="greeks",
        help="Use Greek-implied theoretical prices or intrinsic-only snapshot prices",
    )
    parser.add_argument(
        "--all-snapshots",
        action="store_true",
        help="Create a trade for every snapshot in the window instead of one near entry-time per day",
    )
    parser.add_argument("--max-entry-gap-minutes", type=int, default=10)
    parser.add_argument("--max-settlement-gap-minutes", type=int, default=10)
    parser.add_argument("--min-debit", type=float, default=0.0, help="Skip flies at or below this debit")
    parser.add_argument(
        "--hit-center-tolerance",
        type=float,
        default=0.5,
        help="Points from center strike counted as an intraday center hit",
    )
    return parser.parse_args()


def main():
    args = parse_args()
    rows = list(
        iter_gamma_butterfly_backtest(
            db_path=Path(args.db),
            symbol=args.symbol,
            start_time=args.start_time,
            end_time=args.end_time,
            entry_time=args.entry_time,
            settlement_time=args.settlement_time,
            width=args.width,
            center_method=args.center_method,
            gex_weight=args.gex_weight,
            charm_weight=args.charm_weight,
            pit_window=args.pit_window,
            pit_wall_count=args.pit_wall_count,
            side=args.side,
            price_method=args.price_method,
            one_per_day=not args.all_snapshots,
            max_entry_gap_minutes=args.max_entry_gap_minutes,
            max_settlement_gap_minutes=args.max_settlement_gap_minutes,
            min_debit=args.min_debit,
            hit_center_tolerance=args.hit_center_tolerance,
        )
    )
    output_path = Path(args.output)
    _write_csv(rows, output_path)
    _print_summary(rows, output_path)


if __name__ == "__main__":
    main()
