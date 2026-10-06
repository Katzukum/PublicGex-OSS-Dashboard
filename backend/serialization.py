"""Strict JSON for the TypeScript/Rust boundary (including missing Pandas values)."""
from __future__ import annotations

import dataclasses
import json
import math
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path


def finite_values(value):
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, (float, Decimal)):
        number = float(value)
        return number if math.isfinite(number) else None
    if isinstance(value, dict):
        return {str(key): finite_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [finite_values(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if dataclasses.is_dataclass(value):
        return finite_values(dataclasses.asdict(value))
    if hasattr(value, "item"):
        return finite_values(value.item())
    return None


def browser_json(value) -> str:
    return json.dumps(finite_values(value), allow_nan=False, separators=(",", ":"))
