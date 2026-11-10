"""One configuration contract shared by the dashboard and live collector.

Defaults match the configured original application's instrument universe, not
its older SPY-only emergency fallback. No source-app settings are read at runtime.
"""
from __future__ import annotations

import copy
import json
import math
import os
from pathlib import Path
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from runtime_paths import data_path, require_data_path

DEFAULT_SYMBOLS = ["SPY", "QQQ", "IWM", "SPX", "NDX"]
DEFAULT_OPTION_RESEARCH = {
    "envelope_source": "volume_gamma",
    "pressure_ema_1": 9,
    "pressure_ema_2": 21,
    "exhaustion_sensitivity": "high",
    "exhaustion_price_filter_enabled": True,
    "exhaustion_period_bars": 5,
    "exhaustion_call_above": 0.70,
    "exhaustion_put_below": 0.30,
    "observation_timezone": "America/New_York",
}
DEFAULT_SETTINGS = {
    "refresh_interval": 10,
    "theme": "dark",
    "symbols": DEFAULT_SYMBOLS,
    "api_rate_limit_per_second": 10.0,
    "api_rate_limit_utilization": 0.6,
    "min_poll_interval_seconds": 15,
    "max_poll_interval_seconds": 120,
    "raw_retention_days": 30,
    "maximum_risk_dollars": 500,
    "fees_per_contract": 1.25,
    "feature_flags": {
        "decision_workspace": True, "execution_quotes": True, "edge_lab": True,
        "decision_alerts": True, "trace_replay": True, "trade_journal": True,
    },
    "weights": {"SPY": 0.5, "QQQ": 0.3, "IWM": 0.2},
    "weights_whale": {"SPX": 0.45, "NDX": 0.35, "IWM": 0.2},
    "weights_index_basket": {"SPX": 0.45, "NDX": 0.35, "IWM": 0.2},
    "auto_start_collector": True,
    "auto_start_ninjatrader": True,
    "ninjatrader_port": 5010,
    "option_research": DEFAULT_OPTION_RESEARCH,
}
LEGACY_STARTER_SETTINGS = copy.deepcopy(DEFAULT_SETTINGS)
LEGACY_STARTER_SETTINGS.update({"symbols": ["SPY"], "weights": {"SPY": 1.0}})
LEGACY_STARTER_SETTINGS.pop("weights_index_basket")
for _startup_setting in ("auto_start_collector", "auto_start_ninjatrader", "ninjatrader_port"):
    LEGACY_STARTER_SETTINGS.pop(_startup_setting)
PREFERENCES_NAME = ".publicgex-settings.json"


def _read_object(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return value


def merge_settings(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Settings must be an object")
    merged = copy.deepcopy(DEFAULT_SETTINGS)
    merged.update(copy.deepcopy(raw))
    # The canonical alias wins on legacy reads, as in the original dashboard.
    basket = raw.get("weights_index_basket", raw.get("weights_whale", DEFAULT_SETTINGS["weights_index_basket"]))
    merged["weights_index_basket"] = copy.deepcopy(basket)
    merged["weights_whale"] = copy.deepcopy(basket)
    if "feature_flags" in raw:
        if not isinstance(raw["feature_flags"], dict):
            raise ValueError("feature_flags must be an object")
        merged["feature_flags"] = DEFAULT_SETTINGS["feature_flags"] | raw["feature_flags"]
    if "option_research" in raw:
        if not isinstance(raw["option_research"], dict):
            raise ValueError("option_research must be an object")
        merged["option_research"] = DEFAULT_OPTION_RESEARCH | copy.deepcopy(raw["option_research"])
    return merged


def read_settings(path: str | Path = "settings.json") -> dict:
    return merge_settings(_read_object(require_data_path(path)))


def _number(value, name: str, *, minimum: float = 0, strict: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite number")
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a finite number") from None
    if not math.isfinite(number) or number < minimum or (strict and number == minimum):
        comparison = "greater than" if strict else "at least"
        raise ValueError(f"{name} must be finite and {comparison} {minimum:g}")
    return number


def _symbols(values) -> list[str]:
    if not isinstance(values, list) or not values:
        raise ValueError("At least one symbol is required")
    result = []
    for value in values:
        symbol = str(value).strip().upper()
        if not re.fullmatch(r"[A-Z0-9][A-Z0-9.-]{0,11}", symbol):
            raise ValueError("Symbols must contain only letters, digits, dots, or hyphens")
        if symbol not in result:
            result.append(symbol)
    return result


def _weights(values, name: str) -> dict[str, float]:
    if not isinstance(values, dict) or not values:
        raise ValueError(f"{name} must contain at least one instrument")
    result = {}
    for symbol, value in values.items():
        normalized = _symbols([symbol])[0]
        if normalized in result:
            raise ValueError(f"{name} contains duplicate instrument names")
        result[normalized] = _number(value, f"{name}.{normalized}")
    if sum(result.values()) <= 0:
        raise ValueError(f"{name} must have a positive total weight")
    return result


def validate_settings(settings: dict) -> dict:
    result = merge_settings(settings)
    result["symbols"] = _symbols(result["symbols"])
    for name, minimum in (("refresh_interval", 5), ("min_poll_interval_seconds", 1), ("max_poll_interval_seconds", 1), ("raw_retention_days", 1)):
        number = _number(result[name], name, minimum=minimum)
        if not number.is_integer():
            raise ValueError(f"{name} must be a whole number")
        result[name] = int(number)
    result["api_rate_limit_per_second"] = _number(result["api_rate_limit_per_second"], "api_rate_limit_per_second", strict=True)
    utilization = _number(result["api_rate_limit_utilization"], "api_rate_limit_utilization", minimum=0.1)
    if utilization > 1:
        raise ValueError("api_rate_limit_utilization must be between 0.1 and 1")
    result["api_rate_limit_utilization"] = utilization
    if result["max_poll_interval_seconds"] < result["min_poll_interval_seconds"]:
        raise ValueError("Maximum poll seconds must be at least minimum poll seconds")
    result["maximum_risk_dollars"] = _number(result["maximum_risk_dollars"], "maximum_risk_dollars", strict=True)
    result["fees_per_contract"] = _number(result["fees_per_contract"], "fees_per_contract")
    result["weights"] = _weights(result["weights"], "weights")
    basket = _weights(result["weights_index_basket"], "weights_index_basket")
    result["weights_index_basket"] = basket
    result["weights_whale"] = copy.deepcopy(basket)
    if result["theme"] not in {"dark", "light"}:
        raise ValueError("theme must be dark or light")
    for name in ("auto_start_collector", "auto_start_ninjatrader"):
        if not isinstance(result[name], bool):
            raise ValueError(f"{name} must be a boolean")
    port = _number(result["ninjatrader_port"], "ninjatrader_port", minimum=1024)
    if not port.is_integer() or port > 65535 or port == 5005:
        raise ValueError("NinjaTrader port must be an integer from 1024 to 65535, excluding collector event port 5005")
    result["ninjatrader_port"] = int(port)
    if any(not isinstance(value, bool) for value in result["feature_flags"].values()):
        raise ValueError("Feature flags must be boolean values")
    research = result["option_research"]
    if research["envelope_source"] not in {"volume_gamma", "oi_gamma"}:
        raise ValueError("option_research.envelope_source must be volume_gamma or oi_gamma")
    if research["exhaustion_sensitivity"] not in {"low", "medium", "high", "strong"}:
        raise ValueError("option_research.exhaustion_sensitivity must be low, medium, high, or strong")
    if not isinstance(research["exhaustion_price_filter_enabled"], bool):
        raise ValueError("option_research.exhaustion_price_filter_enabled must be a boolean")
    for name, lower, upper in (
        ("pressure_ema_1", 2, 120),
        ("pressure_ema_2", 2, 120),
        ("exhaustion_period_bars", 2, 30),
    ):
        number = _number(research[name], f"option_research.{name}", minimum=lower)
        if not number.is_integer() or number > upper:
            raise ValueError(f"option_research.{name} must be a whole number from {lower} to {upper}")
        research[name] = int(number)
    if research["pressure_ema_1"] >= research["pressure_ema_2"]:
        raise ValueError("option_research pressure_ema_1 must be less than pressure_ema_2")
    put_below = _number(research["exhaustion_put_below"], "option_research.exhaustion_put_below")
    call_above = _number(research["exhaustion_call_above"], "option_research.exhaustion_call_above")
    if not 0 <= put_below < call_above <= 1:
        raise ValueError("option_research thresholds require 0 <= put_below < call_above <= 1")
    research["exhaustion_put_below"] = put_below
    research["exhaustion_call_above"] = call_above
    timezone_name = str(research["observation_timezone"] or "").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("option_research.observation_timezone must be a valid IANA timezone") from None
    research["observation_timezone"] = timezone_name
    return result


def settings_upgrade(raw: dict, preferences: dict | None = None) -> dict | None:
    """Only the exact old generated preset is eligible; ambiguous edits stay intact.

    New explicit instrument saves are recorded separately so a deliberate SPY-only
    configuration is never mistaken for the old untouched starter preset.
    """
    if (preferences or {}).get("explicit_symbols"):
        return None
    if raw != LEGACY_STARTER_SETTINGS:
        return None
    return copy.deepcopy(DEFAULT_SETTINGS)


def ninjatrader_port_upgrade(raw: dict, preferences: dict | None = None) -> dict | None:
    """Restore the original indicator port once without replacing custom settings.

    15010 was this app's generated default. Explicit choices saved after this
    migration are tracked separately, including an intentional return to 15010.
    Other custom ports and all unrelated settings remain unchanged.
    """
    if (preferences or {}).get("explicit_ninjatrader_port") or raw.get("ninjatrader_port") != 15010:
        return None
    return copy.deepcopy(raw) | {"ninjatrader_port": DEFAULT_SETTINGS["ninjatrader_port"]}


def _write_object(path: Path, value: dict):
    path = require_data_path(path)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, indent=2, allow_nan=False), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def initialize_settings() -> dict:
    """Called by the owning service under its workspace lease, never by a collector."""
    path = data_path("settings.json")
    preferences_path = data_path(PREFERENCES_NAME)
    if not path.exists():
        _write_object(path, DEFAULT_SETTINGS)
        _write_object(preferences_path, {"version": 3, "explicit_symbols": False, "explicit_ninjatrader_port": False})
        return {"status": "initialized", "upgraded": False}
    raw = _read_object(path)
    preferences = _read_object(preferences_path)
    upgraded = settings_upgrade(raw, preferences)
    status = "upgraded_starter"
    backup_name = "settings.before-defaults-v2.json"
    if upgraded is None:
        upgraded = ninjatrader_port_upgrade(raw, preferences)
        if upgraded is None:
            return {"status": "preserved", "upgraded": False}
        status = "upgraded_ninjatrader_port"
        backup_name = "settings.before-ninjatrader-port-v3.json"
    backup = data_path(backup_name)
    if not backup.exists():
        with backup.open("x", encoding="utf-8") as target:
            target.write(path.read_text(encoding="utf-8"))
    _write_object(path, upgraded)
    preferences.update({"version": 3, status: True})
    if status == "upgraded_starter":
        preferences["explicit_symbols"] = False
    _write_object(preferences_path, preferences)
    return {"status": status, "upgraded": True, "backup_path": str(backup)}


def save_settings_patch(patch: dict) -> dict:
    if not isinstance(patch, dict):
        raise ValueError("Settings update must be an object")
    patch = copy.deepcopy(patch)
    canonical, legacy = "weights_index_basket", "weights_whale"
    if canonical in patch and legacy in patch:
        if _weights(patch[canonical], canonical) != _weights(patch[legacy], legacy):
            raise ValueError("Index-basket weight aliases must agree")
    if canonical in patch or legacy in patch:
        basket = patch[canonical] if canonical in patch else patch[legacy]
        patch[canonical] = copy.deepcopy(basket)
        patch[legacy] = copy.deepcopy(basket)
    current = read_settings()
    if "feature_flags" in patch:
        if not isinstance(patch["feature_flags"], dict):
            raise ValueError("feature_flags must be an object")
        patch["feature_flags"] = current["feature_flags"] | patch["feature_flags"]
    if "option_research" in patch:
        if not isinstance(patch["option_research"], dict):
            raise ValueError("option_research must be an object")
        patch["option_research"] = current["option_research"] | patch["option_research"]
    result = validate_settings(current | patch)
    _write_object(data_path("settings.json"), result)
    preferences_path = data_path(PREFERENCES_NAME)
    preferences = _read_object(preferences_path)
    preferences.update({"version": 3,
                        "explicit_symbols": bool(preferences.get("explicit_symbols") or "symbols" in patch),
                        "explicit_ninjatrader_port": bool(preferences.get("explicit_ninjatrader_port") or "ninjatrader_port" in patch)})
    _write_object(preferences_path, preferences)
    return result
