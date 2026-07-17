"""Export OpenGamma data in a TradingView Pine-friendly format.

TradingView Pine scripts cannot open a localhost socket or read local files, so
this script turns the latest dashboard database state into strings you can paste
into the OpenGamma TradingView indicator inputs.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from gex_levels import aggregate_gamma_levels


DEFAULT_DB_PATH = REPO_ROOT / "gex_data.db"
DEFAULT_OUTPUT_DIR = REPO_ROOT / "tradingview"


def _connect_readonly(db_path: Path) -> sqlite3.Connection:
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def _latest_signal_payload(conn: sqlite3.Connection, symbol: str) -> dict[str, Any] | None:
    if not _table_exists(conn, "signal_events"):
        return None

    row = conn.execute(
        """
        SELECT payload_json
        FROM signal_events
        WHERE upper(symbol) = ?
          AND payload_json IS NOT NULL
          AND payload_json != ''
        ORDER BY emitted_at DESC, id DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if not row:
        return None

    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def _latest_snapshot_levels(conn: sqlite3.Connection, symbol: str, per_side: int) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    snapshot = conn.execute(
        """
        SELECT id, timestamp, symbol, spot_price, flip_strike, regime
        FROM gex_snapshots
        WHERE upper(symbol) = ?
          AND spot_price IS NOT NULL
          AND spot_price > 0
        ORDER BY timestamp DESC, id DESC
        LIMIT 1
        """,
        (symbol,),
    ).fetchone()
    if not snapshot:
        return [], {}

    rows = conn.execute(
        """
        SELECT strike_price, option_type, gex_value, open_interest
        FROM raw_option_greeks
        WHERE snapshot_id = ?
        ORDER BY strike_price ASC
        """,
        (snapshot["id"],),
    ).fetchall()

    levels = aggregate_gamma_levels([dict(row) for row in rows], spot=snapshot["spot_price"], per_side=per_side)
    for level in levels:
        level["is_key_level"] = True

    metadata = {
        "timestamp": str(snapshot["timestamp"]),
        "spot": snapshot["spot_price"],
        "regime": snapshot["regime"] or "",
        "dashboard_flip": snapshot["flip_strike"],
    }
    return levels, metadata


def _payload_value(payload: dict[str, Any], symbol: str, key: str) -> Any:
    suffix_key = f"{key}_{symbol.lower()}"
    if suffix_key in payload:
        return payload.get(suffix_key)
    if str(payload.get("dashboard_symbol") or "").upper() == symbol:
        return payload.get(key)
    return payload.get(key)


def _levels_from_payload(payload: dict[str, Any], symbol: str) -> list[dict[str, Any]]:
    key = f"gamma_levels_{symbol.lower()}"
    levels = payload.get(key) or []
    return [dict(level) for level in levels if isinstance(level, dict)]


def _format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""

    text = f"{number:.10f}".rstrip("0").rstrip(".")
    return text if text and text != "-0" else "0"


def _level_gex(level: dict[str, Any]) -> float:
    try:
        return float(level.get("gex", level.get("net_gex", 0)) or 0)
    except (TypeError, ValueError):
        return 0.0


def select_levels(levels: list[dict[str, Any]], max_levels: int) -> list[dict[str, Any]]:
    cleaned = []
    for level in levels:
        try:
            strike = float(level.get("strike") or 0)
        except (TypeError, ValueError):
            continue
        if strike <= 0 or abs(_level_gex(level)) <= 1e-9:
            continue
        cleaned.append(level)

    if max_levels <= 0 or len(cleaned) <= max_levels:
        return sorted(cleaned, key=lambda item: float(item.get("strike") or 0))

    key_levels = [level for level in cleaned if bool(level.get("is_key_level", True))]
    extra_levels = [level for level in cleaned if not bool(level.get("is_key_level", True))]
    key_levels.sort(key=lambda level: abs(_level_gex(level)), reverse=True)
    extra_levels.sort(key=lambda level: abs(_level_gex(level)), reverse=True)

    selected = (key_levels + extra_levels)[:max_levels]
    return sorted(selected, key=lambda item: float(item.get("strike") or 0))


def format_level_string(levels: list[dict[str, Any]]) -> str:
    rows = []
    for level in levels:
        strike = _format_number(level.get("strike"))
        gex = _format_number(_level_gex(level))
        if not strike or not gex:
            continue
        key_flag = "1" if bool(level.get("is_key_level", True)) else "0"
        rows.append(f"{strike}:{gex}:{key_flag}")
    return ";".join(rows)


def dashboard_lines(payload: dict[str, Any], symbol: str, fallback: dict[str, Any]) -> list[str]:
    fields = {
        "Bias": _payload_value(payload, symbol, "dashboard_bias"),
        "Bias score": _payload_value(payload, symbol, "dashboard_bias_score"),
        "Confidence": _payload_value(payload, symbol, "dashboard_confidence"),
        "Context": _payload_value(payload, symbol, "dashboard_context"),
        "Market": _payload_value(payload, symbol, "dashboard_market"),
        "Dealer": _payload_value(payload, symbol, "dashboard_dealer"),
        "Liquidity": _payload_value(payload, symbol, "dashboard_liquidity"),
        "Whale": _payload_value(payload, symbol, "dashboard_whale"),
        "Target": _payload_value(payload, symbol, "dashboard_target"),
        "Invalidation": _payload_value(payload, symbol, "dashboard_invalidation"),
        "Flip": _payload_value(payload, symbol, "dashboard_flip") or fallback.get("dashboard_flip"),
        "Regime": payload.get("regime") or fallback.get("regime"),
        "Spot": payload.get(f"spot_{symbol.lower()}") or fallback.get("spot"),
        "Timestamp": payload.get("timestamp") or fallback.get("timestamp"),
    }
    return [f"{key}: {value}" for key, value in fields.items() if value not in (None, "")]


def export_tradingview_data(db_path: Path, symbol: str, per_side: int, max_levels: int, output_dir: Path) -> Path:
    symbol = symbol.upper()
    output_dir.mkdir(parents=True, exist_ok=True)

    with _connect_readonly(db_path) as conn:
        payload = _latest_signal_payload(conn, symbol) or {}
        levels = _levels_from_payload(payload, symbol)
        fallback = {}
        if not levels:
            levels, fallback = _latest_snapshot_levels(conn, symbol, per_side)

    if not levels:
        raise RuntimeError(f"No gamma levels found for {symbol} in {db_path}")

    selected_levels = select_levels(levels, max_levels)
    level_string = format_level_string(selected_levels)
    lines = [
        f"# OpenGamma TradingView export for {symbol}",
        f"# Levels exported: {len(selected_levels)} of {len(levels)}",
        "",
        "Paste this into the Pine indicator's Gamma levels input:",
        level_string,
        "",
        "Optional dashboard inputs:",
        *dashboard_lines(payload, symbol, fallback),
        "",
    ]

    output_path = output_dir / f"OpenGamma_{symbol}_TradingView_inputs.txt"
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export OpenGamma levels for the TradingView Pine indicator.")
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH), help="Path to gex_data.db")
    parser.add_argument("--symbol", default="NDX", choices=["NDX", "SPX", "ndx", "spx"], help="Index symbol to export")
    parser.add_argument("--per-side", type=int, default=8, help="Fallback snapshot levels per side when no signal payload exists")
    parser.add_argument("--max-levels", type=int, default=80, help="Maximum levels to export from signal payloads. Use 0 for all levels")
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT_DIR), help="Directory for the generated input text file")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_path = export_tradingview_data(
        db_path=Path(args.db),
        symbol=args.symbol,
        per_side=args.per_side,
        max_levels=args.max_levels,
        output_dir=Path(args.output_dir),
    )
    print(f"Wrote TradingView inputs to {output_path}")


if __name__ == "__main__":
    main()
