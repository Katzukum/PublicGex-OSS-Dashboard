import json
import logging
import re
from datetime import datetime, timedelta
from statistics import median
from typing import Optional

from sqlalchemy import func

from models import GexSnapshot, SignalEvent, SignalOutcome, get_session_factory


logger = logging.getLogger(__name__)

OUTCOME_HORIZONS_MINUTES = (15, 30, 60)
PRIMARY_HORIZON_MINUTES = 30
MIN_EXACT_SAMPLE = 8
SIGNAL_SYMBOLS = ("NDX", "SPX")

DASHBOARD_FIELDS = (
    "dashboard_symbol",
    "dashboard_bias",
    "dashboard_bias_score",
    "dashboard_confidence",
    "dashboard_target",
    "dashboard_invalidation",
    "dashboard_flip",
    "dashboard_context",
    "dashboard_market",
    "dashboard_dealer",
    "dashboard_liquidity",
    "dashboard_whale",
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
    text = re.sub(r"^(Market|Dealer|Liquidity|Whale):\s*", "", text, flags=re.IGNORECASE)
    text = text.replace("LOW CONFIDENCE ", "")
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


def _session_or_new(session=None):
    if session is not None:
        return session, False
    return get_session_factory()(), True


def record_signal_payload(payload: dict, session=None, symbols=SIGNAL_SYMBOLS) -> int:
    """Persist one signal event per symbol from a broadcast payload."""

    db, owns_session = _session_or_new(session)
    try:
        emitted_at = parse_datetime(payload.get("timestamp")) or datetime.now()
        created = 0

        for symbol in symbols:
            dashboard = dashboard_from_payload(payload, symbol)
            if not dashboard:
                continue

            setup_key, direction_key = setup_keys(symbol, dashboard, payload.get("regime", ""))
            event = SignalEvent(
                emitted_at=emitted_at,
                symbol=symbol,
                spot_price=float(dashboard["spot"]),
                regime=_clean_label(payload.get("regime")),
                bias=_bias_family(dashboard.get("dashboard_bias")),
                bias_score=_float_or_none(dashboard.get("dashboard_bias_score")),
                confidence=_float_or_none(dashboard.get("dashboard_confidence")),
                target=_float_or_none(dashboard.get("dashboard_target")),
                invalidation=_float_or_none(dashboard.get("dashboard_invalidation")),
                flip=_float_or_none(dashboard.get("dashboard_flip")),
                market_state=_clean_label(dashboard.get("dashboard_market")),
                dealer_state=_clean_label(dashboard.get("dashboard_dealer")),
                liquidity_state=_clean_label(dashboard.get("dashboard_liquidity")),
                whale_state=_clean_label(dashboard.get("dashboard_whale")),
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


def _latest_snapshot_time(session, symbol: str) -> Optional[datetime]:
    value = (
        session.query(func.max(GexSnapshot.timestamp))
        .filter(GexSnapshot.symbol == symbol)
        .scalar()
    )
    return parse_datetime(value)


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


def _build_outcome(session, event: SignalEvent, horizon_minutes: int) -> Optional[SignalOutcome]:
    emitted_at = parse_datetime(event.emitted_at)
    if not emitted_at or not event.spot_price:
        return None

    horizon_at = emitted_at + timedelta(minutes=horizon_minutes)
    end_row = (
        session.query(GexSnapshot)
        .filter(GexSnapshot.symbol == event.symbol)
        .filter(GexSnapshot.timestamp >= horizon_at)
        .order_by(GexSnapshot.timestamp.asc())
        .first()
    )
    if not end_row:
        return None

    path_rows = (
        session.query(GexSnapshot)
        .filter(GexSnapshot.symbol == event.symbol)
        .filter(GexSnapshot.timestamp > emitted_at)
        .filter(GexSnapshot.timestamp <= end_row.timestamp)
        .order_by(GexSnapshot.timestamp.asc())
        .all()
    )
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
    """Create outcome labels for signals whose future snapshots are available."""

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
            .outerjoin(outcome_counts, SignalEvent.id == outcome_counts.c.signal_event_id)
            .filter(func.coalesce(outcome_counts.c.outcome_count, 0) < len(horizons))
            .order_by(SignalEvent.emitted_at.asc())
            .limit(limit)
            .all()
        )
        labeled = 0

        for event in events:
            latest_time = _latest_snapshot_time(db, event.symbol)
            if not latest_time:
                continue

            emitted_at = parse_datetime(event.emitted_at)
            if not emitted_at:
                continue

            existing = {outcome.horizon_minutes for outcome in event.outcomes}
            for horizon in horizons:
                if horizon in existing or latest_time < emitted_at + timedelta(minutes=horizon):
                    continue

                outcome = _build_outcome(db, event, horizon)
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


def _stats_from_rows(rows, fallback_label: str, horizon_minutes: int) -> dict:
    sample_size = len(rows)
    if not rows:
        return {
            "horizon_minutes": horizon_minutes,
            "sample_size": 0,
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
    median_move = median(move_values) if move_values else None
    median_favorable = median(favorable_values) if favorable_values else None
    median_adverse = median(adverse_values) if adverse_values else None
    win_text = "--" if win_rate is None else f"{round(win_rate * 100)}%"
    move_text = "--" if median_move is None else f"{median_move:+.0f}"

    return {
        "horizon_minutes": horizon_minutes,
        "sample_size": sample_size,
        "wins": wins,
        "win_rate": win_rate,
        "median_move_points": median_move,
        "median_favorable_points": median_favorable,
        "median_adverse_points": median_adverse,
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


def update_signal_performance_for_payload(payload: dict) -> dict:
    """Label due outcomes, attach current edge stats, then log the emitted signal."""

    try:
        label_due_outcomes()
    except Exception as exc:
        logger.warning("Signal outcome labeling failed: %s", exc)

    edge_by_symbol = {}
    for symbol in SIGNAL_SYMBOLS:
        dashboard = dashboard_from_payload(payload, symbol)
        if not dashboard:
            continue
        stats = edge_stats_for_dashboard(dashboard, payload.get("regime", ""))
        edge_by_symbol[symbol] = stats

        flattened = flatten_primary_edge_stats(stats)
        for key, value in flattened.items():
            payload[f"{key}_{symbol.lower()}"] = value

        if str(payload.get("dashboard_symbol") or "").upper() == symbol:
            payload.update(flattened)

    payload["dashboard_edge_stats"] = edge_by_symbol

    try:
        record_signal_payload(payload)
    except Exception as exc:
        logger.warning("Signal event logging failed: %s", exc)

    return payload
