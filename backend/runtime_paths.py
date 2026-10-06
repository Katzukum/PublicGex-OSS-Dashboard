"""Private application data paths, configured before any domain module imports."""
from __future__ import annotations

import os
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PUBLICGEX_DATA_DIR", SOURCE_ROOT.parent / ".data")).expanduser().resolve()


def data_path(name: str) -> Path:
    candidate = (DATA_DIR / name).resolve()
    if not candidate.is_relative_to(DATA_DIR):
        raise ValueError("Application data must stay inside the configured data directory")
    return candidate


def require_data_path(path: str | Path) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = DATA_DIR / candidate
    candidate = candidate.resolve()
    if not candidate.is_relative_to(DATA_DIR):
        raise ValueError("Application data must stay inside the configured data directory")
    return candidate
