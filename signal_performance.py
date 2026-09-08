import json
import logging
import random
import re
from bisect import bisect_left, bisect_right
from collections import defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import selectinload

from models import GexSnapshot, SignalEvent, SignalOutcome, get_session_factory


logger = logging.getLogger(__name__)

OUTCOME_HORIZONS_MINUTES = (15, 30, 60)
PRIMARY_HORIZON_MINUTES = 30
MIN_EXACT_SAMPLE = 8
SIGNAL_SYMBOLS = ("NDX", "SPX")
_configured_session_factory = None

DASHBOARD_FIELDS = (
    "dashboard_symbol",
    "dashboard_bias",
    "dashboard_bias_score",
    "dashboard_confidence",
    "dashboard_data_quality",
    "dashboard_scenario_id",
    "dashboard_scenario_type",
    "dashboard_target",
    "dashboard_invalidation",
    "dashboard_flip",
    "dashboard_context",
    "dashboard_market",
    "dashboard_dealer",
    "dashboard_liquidity",
    "dashboard_whale",
    "dashboard_index_basket",
)


def parse_datetime(value) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None

    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None)
    except ValueError:
        return None


def _clean_label(value: str) -> str:
    text = str(value or "").strip()
    text = re.sub(r"^(Market|Dealer|Liquidity|Whale|Index Basket):\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("LOW CONFIDENCE ", "")
    text = text.replace("LOW DATA QUALITY ", "")
    text = re.sub(r"\s+", " ", text)
    return text.upper() or "UNKNOWN"


def _bias_family(value: str) -> str:
    text = str(value or "").upper()
    if "CALL" in text:
        return "CALL"
    if "PUT" in text:
        return "PUT"
    return "WAIT"


def _direction_from_bias(value: str) -> int:
    family = _bias_family(value)
    if family == "CALL":
        return 1
    if family == "PUT":
        return -1
    return 0


def _score_bucket(score) -> str:
    try:
        abs_score = abs(float(score or 0))
    except (TypeError, ValueError):
        abs_score = 0

    if abs_score >= 0.55:
        return "STRONG"
    if abs_score >= 0.22:
        return "ACTIVE"
    return "WAIT"


def setup_keys(symbol: str, dashboard: dict, regime: str = "") -> tuple[str, str]:
    bias = _bias_family(dashboard.get("dashboard_bias"))
    score_bucket = _score_bucket(dashboard.get("dashboard_bias_score"))
    regime_text = _clean_label(regime)
    dealer = _clean_label(dashboard.get("dashboard_dealer"))
    liquidity = _clean_label(dashboard.get("dashboard_liquidity"))

    exact_key = "|".join([symbol, bias, score_bucket, regime_text, dealer, liquidity])
    direction_key = "|".join([symbol, bias])
    return exact_key, direction_key


def _float_or_none(value):
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _payload_value(payload: dict, symbol: str, key: str):
    suffix_key = f"{key}_{symbol.lower()}"
    if suffix_key in payload:
        return payload.get(suffix_key)
    if str(payload.get("dashboard_symbol") or "").upper() == symbol:
        return payload.get(key)
    return None


def dashboard_from_payload(payload: dict, symbol: str) -> Optional[dict]:
    dashboard = {key: _payload_value(payload, symbol, key) for key in DASHBOARD_FIELDS}
    dashboard["dashboard_symbol"] = dashboard.get("dashboard_symbol") or symbol
    dashboard["spot"] = _float_or_none(payload.get(f"spot_{symbol.lower()}"))
    dashboard["regime"] = payload.get("regime", "")
    dashboard["timestamp"] = payload.get("timestamp")

    if not dashboard.get("dashboard_bias") or not dashboard.get("spot"):
        return None

    return dashboard


def configure_engine(engine=None) -> None:
    """Use the application's initialized engine for future short-lived sessions."""

    global _configured_session_factory
    _configured_session_factory = get_session_factory(engine) if engine is not None else None


def _session_or_new(session=None):
    if session is not None:
        return session, False
    session_factory = _configured_session_factory or get_session_factory()
    return session_factory(), True


def record_signal_payload(payload: dict, session=None, symbols=SIGNAL_SYMBOLS) -> int:
    """Persist only decision-state transitions, not routine refreshes."""

    db, owns_session = _session_or_new(session)
    try:
        emitted_at = parse_datetime(payload.get("timestamp")) or datetime.now()
        created = 0

        for symbol in symbols:
            dashboard = dashboard_from_payload(payload, symbol)
            if not dashboard:
                continue

            setup_key, direction_key = setup_keys(symbol, dashboard, payload.get("regime", ""))
            state_key = str(dashboard.get("dashboard_scenario_id") or setup_key)
            previous = (
                db.query(SignalEvent)
                .filter(SignalEvent.symbol == symbol)
                .order_by(SignalEvent.emitted_at.desc(), SignalEvent.id.desc())
                .first()
            )
            if previous and previous.state_key == state_key:
                continue
            data_quality = _float_or_none(
                dashboard.get("dashboard_data_quality")
                if dashboard.get("dashboard_data_quality") is not None
                else dashboard.get("dashboard_confidence")
            )
            bias = _bias_family(dashboard.get("dashboard_bias"))
            target = _float_or_none(dashboard.get("dashboard_target"))
            invalidation = _float_or_none(dashboard.get("dashboard_invalidation"))
            is_opportunity = bias != "WAIT" and (data_quality or 0) >= 0.45 and target is not None and invalidation is not None
            event = SignalEvent(
                emitted_at=emitted_at,
                symbol=symbol,
                spot_price=float(dashboard["spot"]),
                regime=_clean_label(payload.get("regime")),
                bias=bias,
                bias_score=_float_or_none(dashboard.get("dashboard_bias_score")),
                confidence=data_quality,
                data_quality=data_quality,
                edge_probability=None,
                target=target,
                invalidation=invalidation,
                flip=_float_or_none(dashboard.get("dashboard_flip")),
                market_state=_clean_label(dashboard.get("dashboard_market")),
                dealer_state=_clean_label(dashboard.get("dashboard_dealer")),
                liquidity_state=_clean_label(dashboard.get("dashboard_liquidity")),
                whale_state=_clean_label(dashboard.get("dashboard_index_basket") or dashboard.get("dashboard_whale")),
                state_key=state_key,
                scenario_type=dashboard.get("dashboard_scenario_type"),
                scenario_id=dashboard.get("dashboard_scenario_id"),
                session_date=emitted_at.date(),
                event_tags_json="[]",
                liquidity_grade=None,
                is_opportunity=is_opportunity,
                setup_key=setup_key,
                direction_key=direction_key,
                payload_json=json.dumps(payload, default=str),
            )
            db.add(event)
            created += 1

        if owns_session:
            db.commit()
        return created
    except Exception:
        if owns_session:
            db.rollback()
        raise
    finally:
        if owns_session:
            db.close()


def _target_hit(direction: int, spot: float, target: Optional[float]) -> bool:
    if direction > 0:
        return target is not None and spot >= target
    if direction < 0:
        return target is not None and spot <= target
    return False


def _invalidation_hit(direction: int, spot: float, invalidation: Optional[float]) -> bool:
    if direction > 0:
        return invalidation is not None and spot <= invalidation
    if direction < 0:
        return invalidation is not None and spot >= invalidation
    return False


def _build_outcome(
    event: SignalEvent,
    horizon_minutes: int,
    snapshot_rows,
    snapshot_times,
) -> Optional[SignalOutcome]:
    emitted_at = parse_datetime(event.emitted_at)
    if not emitted_at or not event.spot_price:
        return None

    horizon_at = emitted_at + timedelta(minutes=horizon_minutes)
    end_index = bisect_left(snapshot_times, horizon_at)
    if end_index >= len(snapshot_rows):
        return None

    start_index = bisect_right(snapshot_times, emitted_at)
    end_row = snapshot_rows[end_index]
    path_rows = snapshot_rows[start_index:end_index + 1]
    if not path_rows:
        return None

    direction = _direction_from_bias(event.bias)
    start_spot = float(event.spot_price or 0)
    end_spot = float(end_row.spot_price or 0)
    if start_spot <= 0 or end_spot <= 0:
        return None

    target_hit_at = None
    invalidation_hit_at = None
    directed_path = []

    for row in path_rows:
        spot = float(row.spot_price or 0)
        if spot <= 0:
            continue

        if direction != 0:
            directed_path.append((spot - start_spot) * direction)

        row_time = parse_datetime(row.timestamp)
        if not target_hit_at and _target_hit(direction, spot, event.target):
            target_hit_at = row_time
        if not invalidation_hit_at and _invalidation_hit(direction, spot, event.invalidation):
            invalidation_hit_at = row_time

    move_points = end_spot - start_spot
    move_pct = move_points / start_spot
    directional_move = move_points * direction if direction else None

    target_first = bool(
        target_hit_at and (not invalidation_hit_at or target_hit_at <= invalidation_hit_at)
    )
    invalidation_first = bool(
        invalidation_hit_at and (not target_hit_at or invalidation_hit_at < target_hit_at)
    )

    if direction == 0:
        is_win = None
        outcome_label = "wait"
        max_favorable = None
        max_adverse = None
    else:
        max_favorable = max([0, *directed_path]) if directed_path else 0
        max_adverse = max([0, *(-value for value in directed_path)]) if directed_path else 0
        if target_first:
            is_win = True
            outcome_label = "target"
        elif invalidation_first:
            is_win = False
            outcome_label = "invalidation"
        elif directional_move and directional_move > 0:
            is_win = True
            outcome_label = "directional"
        else:
            is_win = False
            outcome_label = "against"

    return SignalOutcome(
        signal_event_id=event.id,
        horizon_minutes=horizon_minutes,
        labeled_at=datetime.now(),
        horizon_at=horizon_at,
        observed_at=parse_datetime(end_row.timestamp),
        end_spot=end_spot,
        move_points=move_points,
        move_pct=move_pct,
        directional_move_points=directional_move,
        max_favorable_points=max_favorable,
        max_adverse_points=max_adverse,
        hit_target=bool(target_hit_at),
        hit_invalidation=bool(invalidation_hit_at),
        target_first=target_first,
        invalidation_first=invalidation_first,
        is_win=is_win,
        outcome_label=outcome_label,
    )


def label_due_outcomes(session=None, horizons=OUTCOME_HORIZONS_MINUTES, limit: int = 2000) -> int:
    """Create due labels with one snapshot-window query per event symbol."""

    horizons = tuple(horizons)
    db, owns_session = _session_or_new(session)
    try:
        outcome_counts = (
            db.query(
                SignalOutcome.signal_event_id.label("signal_event_id"),
                func.count(SignalOutcome.id).label("outcome_count"),
            )
            .filter(SignalOutcome.horizon_minutes.in_(horizons))
            .group_by(SignalOutcome.signal_event_id)
            .subquery()
        )
        events = (
            db.query(SignalEvent)
            .options(selectinload(SignalEvent.outcomes))
            .outerjoin(outcome_counts, SignalEvent.id == outcome_counts.c.signal_event_id)
            .filter(func.coalesce(outcome_counts.c.outcome_count, 0) < len(horizons))
            .order_by(SignalEvent.emitted_at.asc())
            .limit(limit)
            .all()
        )
        labeled = 0
        events_by_symbol = defaultdict(list)
        for event in events:
            events_by_symbol[event.symbol].append(event)

        for symbol, symbol_events in events_by_symbol.items():
            emitted_times = [parse_datetime(event.emitted_at) for event in symbol_events]
            emitted_times = [value for value in emitted_times if value is not None]
            if not emitted_times:
                continue

            snapshot_rows = (
                db.query(GexSnapshot)
                .filter(GexSnapshot.symbol == symbol)
                .filter(GexSnapshot.timestamp > min(emitted_times))
                .order_by(GexSnapshot.timestamp.asc())
                .all()
            )
            snapshot_rows = [row for row in snapshot_rows if parse_datetime(row.timestamp) is not None]
            snapshot_times = [parse_datetime(row.timestamp) for row in snapshot_rows]
            if not snapshot_rows:
                continue

            latest_time = snapshot_times[-1]
            for event in symbol_events:
                emitted_at = parse_datetime(event.emitted_at)
                if not emitted_at:
                    continue

                existing = {outcome.horizon_minutes for outcome in event.outcomes}
                for horizon in horizons:
                    if horizon in existing or latest_time < emitted_at + timedelta(minutes=horizon):
                        continue

                    outcome = _build_outcome(event, horizon, snapshot_rows, snapshot_times)
                    if outcome:
                        db.add(outcome)
                        labeled += 1

        if owns_session:
            db.commit()
        return labeled
    except Exception:
        if owns_session:
            db.rollback()
        raise
    finally:
        if owns_session:
            db.close()


def independent_outcomes(rows, horizon_minutes: int):
    """Greedily retain non-overlapping opportunities per symbol and horizon."""
    selected = []
    next_allowed = {}
    ordered = sorted(rows, key=lambda row: parse_datetime(row.signal.emitted_at) or datetime.min)
    for row in ordered:
        event_time = parse_datetime(row.signal.emitted_at)
        if event_time is None:
            continue
        symbol = row.signal.symbol
        if event_time < next_allowed.get(symbol, datetime.min):
            continue
        selected.append(row)
        next_allowed[symbol] = event_time + timedelta(minutes=horizon_minutes)
    return selected


def _clustered_interval(rows, value_getter, samples=300):
    by_day = defaultdict(list)
    for row in rows:
        day = row.signal.session_date or parse_datetime(row.signal.emitted_at).date()
        value = value_getter(row)
        if value is not None:
            by_day[day].append(float(value))
    days = list(by_day)
    if len(days) < 2:
        return None
    rng = random.Random(42)
    estimates = []
    for _ in range(samples):
        sampled = [rng.choice(days) for _ in days]
        values = [value for day in sampled for value in by_day[day]]
        estimates.append(sum(values) / len(values))
    estimates.sort()
    return [estimates[int(0.025 * (len(estimates) - 1))], estimates[int(0.975 * (len(estimates) - 1))]]


def _stats_from_rows(rows, fallback_label: str, horizon_minutes: int) -> dict:
    rows = independent_outcomes(rows, horizon_minutes)
    sample_size = len(rows)
    if not rows:
        return {
            "horizon_minutes": horizon_minutes,
            "sample_size": 0,
            "independent_opportunities": 0,
            "unique_days": 0,
            "evidence_status": "INSUFFICIENT",
            "wins": 0,
            "win_rate": None,
            "median_move_points": None,
            "median_favorable_points": None,
            "median_adverse_points": None,
            "sample_label": fallback_label,
            "summary": f"{horizon_minutes}m no labeled samples",
        }

    wins = sum(1 for row in rows if row.is_win is True)
    win_denominator = sum(1 for row in rows if row.is_win is not None)
    win_rate = wins / win_denominator if win_denominator else None
    move_values = [
        row.directional_move_points
        if row.directional_move_points is not None
        else row.move_points
        for row in rows
    ]
    favorable_values = [row.max_favorable_points for row in rows if row.max_favorable_points is not None]
    adverse_values = [row.max_adverse_points for row in rows if row.max_adverse_points is not None]
    after_cost_values = [value - 0.10 for value in move_values if value is not None]
    median_move = median(move_values) if move_values else None
    median_favorable = median(favorable_values) if favorable_values else None
    median_adverse = median(adverse_values) if adverse_values else None
    win_text = "--" if win_rate is None else f"{round(win_rate * 100)}%"
    move_text = "--" if median_move is None else f"{median_move:+.0f}"
    unique_days = len({row.signal.session_date or parse_datetime(row.signal.emitted_at).date() for row in rows})
    holdout_count = max(1, sample_size // 5)
    holdout = after_cost_values[-holdout_count:]
    holdout_expectancy = sum(holdout) / len(holdout) if holdout else None
    after_cost_expectancy = sum(after_cost_values) / len(after_cost_values) if after_cost_values else None
    if sample_size < 20 or unique_days < 10:
        evidence_status = "INSUFFICIENT"
    elif sample_size < 50 or unique_days < 20 or not (holdout_expectancy is not None and holdout_expectancy > 0):
        evidence_status = "EMERGING"
    else:
        evidence_status = "CALIBRATED"

    return {
        "horizon_minutes": horizon_minutes,
        "sample_size": sample_size,
        "independent_opportunities": sample_size,
        "unique_days": unique_days,
        "evidence_status": evidence_status,
        "wins": wins,
        "win_rate": win_rate,
        "median_move_points": median_move,
        "median_favorable_points": median_favorable,
        "median_adverse_points": median_adverse,
        "after_cost_expectancy": after_cost_expectancy,
        "holdout_expectancy": holdout_expectancy,
        "win_rate_interval": _clustered_interval(rows, lambda row: 1 if row.is_win else 0 if row.is_win is False else None),
        "target_first_rate": sum(1 for row in rows if row.target_first) / sample_size,
        "invalidation_first_rate": sum(1 for row in rows if row.invalidation_first) / sample_size,
        "sample_label": fallback_label,
        "summary": f"{horizon_minutes}m {win_text} n={sample_size} med {move_text}",
    }


def _query_outcome_rows(session, symbol: str, key_name: str, key_value: str, horizon_minutes: int):
    signal_attr = getattr(SignalEvent, key_name)
    return (
        session.query(SignalOutcome)
        .join(SignalEvent)
        .filter(SignalEvent.symbol == symbol)
        .filter(signal_attr == key_value)
        .filter(SignalEvent.is_opportunity.is_(True))
        .filter(SignalOutcome.horizon_minutes == horizon_minutes)
        .order_by(SignalOutcome.observed_at.desc())
        .limit(500)
        .all()
    )


def edge_stats_for_dashboard(dashboard: dict, regime: str = "", session=None) -> dict:
    """Return empirical stats for a dashboard signal, with bias fallback."""

    symbol = str(dashboard.get("dashboard_symbol") or "").upper()
    if not symbol:
        return {"symbol": "", "horizons": [], "primary": {}}

    setup_key, direction_key = setup_keys(symbol, dashboard, regime)
    db, owns_session = _session_or_new(session)
    try:
        horizons = []
        for horizon in OUTCOME_HORIZONS_MINUTES:
            exact_rows = _query_outcome_rows(db, symbol, "setup_key", setup_key, horizon)
            if len(exact_rows) >= MIN_EXACT_SAMPLE:
                stats = _stats_from_rows(exact_rows, "exact setup", horizon)
            else:
                fallback_rows = _query_outcome_rows(db, symbol, "direction_key", direction_key, horizon)
                stats = _stats_from_rows(fallback_rows, "symbol+bias", horizon)
                stats["exact_sample_size"] = len(exact_rows)
            horizons.append(stats)

        primary = next(
            (item for item in horizons if item["horizon_minutes"] == PRIMARY_HORIZON_MINUTES),
            horizons[0] if horizons else {},
        )
        return {
            "symbol": symbol,
            "setup_key": setup_key,
            "direction_key": direction_key,
            "horizons": horizons,
            "primary": primary,
        }
    finally:
        if owns_session:
            db.close()


def flatten_primary_edge_stats(stats: dict) -> dict:
    primary = stats.get("primary") or {}
    return {
        "dashboard_edge_horizon": primary.get("horizon_minutes"),
        "dashboard_edge_sample": primary.get("sample_size", 0),
        "dashboard_edge_win_rate": primary.get("win_rate"),
        "dashboard_edge_median_move": primary.get("median_move_points"),
        "dashboard_edge_median_favorable": primary.get("median_favorable_points"),
        "dashboard_edge_median_adverse": primary.get("median_adverse_points"),
        "dashboard_edge_source": primary.get("sample_label", ""),
        "dashboard_edge_summary": primary.get("summary", ""),
    }


def update_signal_performance_for_payload(payload: dict, session=None) -> dict:
    """Attach edge stats and record the broadcast within one session boundary."""

    db, owns_session = _session_or_new(session)
    try:
        edge_by_symbol = {}
        for symbol in SIGNAL_SYMBOLS:
            dashboard = dashboard_from_payload(payload, symbol)
            if not dashboard:
                continue
            stats = edge_stats_for_dashboard(
                dashboard,
                payload.get("regime", ""),
                session=db,
            )
            edge_by_symbol[symbol] = stats

            flattened = flatten_primary_edge_stats(stats)
            for key, value in flattened.items():
                payload[f"{key}_{symbol.lower()}"] = value

            if str(payload.get("dashboard_symbol") or "").upper() == symbol:
                payload.update(flattened)

        payload["dashboard_edge_stats"] = edge_by_symbol
        record_signal_payload(payload, session=db)
        if owns_session:
            db.commit()
    except Exception as exc:
        if owns_session:
            db.rollback()
        logger.warning("Signal performance update failed: %s", exc)
    finally:
        if owns_session:
            db.close()

    return payload
