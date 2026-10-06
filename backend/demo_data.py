"""Explicit, deterministic synthetic fixtures; never calls a market-data provider."""
from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from statistics import NormalDist

from sqlalchemy import select

from models import (CollectionRun, GexSnapshot, RawOptionGreek, SignalEvent, SignalOutcome,
                    TradeJournalEntry, get_session_factory, initialize_database)
from runtime_paths import data_path


def seed_demo(engine):
    marker = data_path("SYNTHETIC_DEMO.json")
    Session = get_session_factory(engine)
    with Session() as session:
        existing = session.scalar(select(GexSnapshot.id).limit(1))
    if existing:
        if not marker.exists():
            raise ValueError("Demo mode requires an empty workspace. Existing market data will not be mixed with synthetic data.")
        return
    now = datetime.now().replace(second=0, microsecond=0)
    universe = {"SPY": (610.0, 1.0), "QQQ": (540.0, 1.0), "IWM": (225.0, 1.0), "SPX": (6100.0, 5.0), "NDX": (22000.0, 25.0)}
    settings = {
        "symbols": list(universe), "refresh_interval": 10, "theme": "dark",
        "weights": {"SPY": 0.5, "QQQ": 0.3, "IWM": 0.2},
        "weights_whale": {"SPX": 0.45, "NDX": 0.35, "IWM": 0.2},
        "maximum_risk_dollars": 500, "fees_per_contract": 1.25,
    }
    if not data_path("settings.json").exists():
        data_path("settings.json").write_text(json.dumps(settings, indent=2), encoding="utf-8")
    normal = NormalDist()
    with Session.begin() as session:
        for step in range(61):
            timestamp = now - timedelta(minutes=2 * (60 - step))
            run = CollectionRun(started_at=timestamp, finished_at=timestamp, status="success",
                                message="SYNTHETIC DEMO — no market data", symbols_requested=json.dumps(list(universe)),
                                symbols_succeeded=json.dumps(list(universe)), symbols_failed="[]", symbols_skipped="[]")
            session.add(run)
            session.flush()
            for index, (symbol, (anchor, increment)) in enumerate(universe.items()):
                spot = anchor + increment * (math.sin(step / 7 + index) * 1.1 + step / 80)
                raw = []
                for offset in range(-12, 13):
                    strike = anchor + offset * increment
                    volatility = 0.0045 if symbol in {"SPX", "NDX"} else 0.008
                    d1 = math.log(spot / strike) / volatility + volatility / 2
                    gamma = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi) / (spot * volatility)
                    for side in ("CALL", "PUT"):
                        call = side == "CALL"
                        delta = normal.cdf(d1) - (0 if call else 1)
                        wall_center = 4 if call else -4
                        oi = int(400 + 3800 * math.exp(-((offset - wall_center) / 2.8) ** 2) + 500 * (1 + math.sin(offset + step / 8)))
                        premium = max(0.1, (max(spot - strike, 0) if call else max(strike - spot, 0)) + spot * volatility * math.exp(-abs(offset) / 7) * 0.45)
                        spread = min(0.2, premium * 0.06)
                        raw.append(dict(timestamp=timestamp, symbol=symbol, expiration_date=now.date(),
                            osi_symbol=f"DEMO-{symbol}-{strike:g}-{'C' if call else 'P'}", strike_price=strike, option_type=side,
                            delta=delta, gamma=gamma, open_interest=oi, underlying_price=spot,
                            gex_value=gamma * oi * 100 * spot * spot * 0.01 * (1 if call else -1),
                            bid=round(premium-spread/2, 4), ask=round(premium+spread/2, 4), mid_price=round(premium, 4), last_price=round(premium,4),
                            bid_size=20, ask_size=25, volume=120, implied_volatility=0.20,
                            bid_timestamp=timestamp, ask_timestamp=timestamp, last_timestamp=timestamp))
                call_gex = sum(row["gex_value"] for row in raw if row["option_type"] == "CALL")
                put_gex = sum(row["gex_value"] for row in raw if row["option_type"] == "PUT")
                snapshot = GexSnapshot(collection_run_id=run.id, timestamp=timestamp, symbol=symbol, spot_price=spot,
                    total_net_gex=call_gex+put_gex, total_call_gex=call_gex, total_put_gex=put_gex,
                    max_call_gex_strike=anchor+4*increment, max_put_gex_strike=anchor-4*increment,
                    flip_strike=anchor, regime="SYNTHETIC DEMO", effective_gex=call_gex+put_gex,
                    total_gamma=sum(row["gamma"]*row["open_interest"]*100 for row in raw), total_theta=-10000)
                session.add(snapshot)
                session.flush()
                session.add_all([RawOptionGreek(snapshot_id=snapshot.id, **row) for row in raw])
        for symbol in ("SPX", "NDX"):
            anchor, increment = universe[symbol]
            for index in range(48):
                timestamp = (now - timedelta(days=1 + index // 2)).replace(hour=10 if index % 2 else 13, minute=0)
                win = index % 5 < 3
                event = SignalEvent(emitted_at=timestamp, symbol=symbol, spot_price=anchor, regime="MELT UP",
                    bias="CALL", bias_score=0.45, confidence=0.85, data_quality=0.95, edge_probability=0.58,
                    target=anchor+increment*3, invalidation=anchor-increment*2, flip=anchor,
                    state_key=f"SYNTHETIC|{index}", scenario_type="UPSIDE_EXPANSION", scenario_id=f"DEMO-{symbol}-{index}",
                    session_date=timestamp.date(), event_tags_json='["SYNTHETIC_DEMO"]', liquidity_grade="A", is_opportunity=True,
                    setup_key=f"{symbol}|SYNTHETIC|{index}", direction_key=f"{symbol}|CALL", payload_json='{"synthetic":true}')
                session.add(event)
                session.flush()
                for horizon in (15, 30, 60):
                    move = increment * (1.6 if win else -1.2)
                    session.add(SignalOutcome(signal_event_id=event.id, horizon_minutes=horizon, labeled_at=timestamp+timedelta(minutes=horizon),
                        horizon_at=timestamp+timedelta(minutes=horizon), observed_at=timestamp+timedelta(minutes=horizon),
                        end_spot=anchor+move, move_points=move, move_pct=move/anchor, directional_move_points=move,
                        max_favorable_points=increment*2.2, max_adverse_points=increment*0.7,
                        hit_target=win, hit_invalidation=not win, target_first=win, invalidation_first=not win, is_win=win,
                        outcome_label="target" if win else "invalidation"))
        session.add(TradeJournalEntry(created_at=now, updated_at=now, symbol="SPX", session_date=now.date(),
            scenario_type="UPSIDE_EXPANSION", regime="SYNTHETIC DEMO", event_tags_json='["SYNTHETIC_DEMO"]',
            liquidity_grade="A", contracts=1, entry_price=1.2, exit_price=1.7, entry_time=now-timedelta(minutes=45),
            exit_time=now-timedelta(minutes=15), fees=2.5, pnl=47.5, slippage=0, underlying_mfe=8, underlying_mae=2,
            exit_reason="Synthetic target", adhered_to_plan=True, notes="Synthetic demo entry; not an actual trade."))
    one_off = initialize_database(reset_old_schema=False, db_path=data_path("one_off_gex_data.db"))
    OneSession = get_session_factory(one_off)
    with Session() as source, OneSession.begin() as destination:
        original = source.scalars(select(GexSnapshot).where(GexSnapshot.symbol == "SPX").order_by(GexSnapshot.timestamp.desc()).limit(1)).one()
        run = CollectionRun(started_at=now, finished_at=now, status="success", message="SYNTHETIC DEMO one-off")
        destination.add(run)
        destination.flush()
        values = {column.name: getattr(original, column.name) for column in GexSnapshot.__table__.columns if column.name not in {"id", "collection_run_id"}}
        snapshot = GexSnapshot(collection_run_id=run.id, **values)
        destination.add(snapshot)
        destination.flush()
        for row in original.raw_options:
            values = {column.name: getattr(row, column.name) for column in RawOptionGreek.__table__.columns if column.name not in {"id", "snapshot_id"}}
            destination.add(RawOptionGreek(snapshot_id=snapshot.id, **values))
    one_off.dispose()
    marker.write_text(json.dumps({"synthetic": True, "created_at": now.isoformat(), "description": "Synthetic UI demonstration only; not actual prices, forecasts, or trading results."}, indent=2), encoding="utf-8")
