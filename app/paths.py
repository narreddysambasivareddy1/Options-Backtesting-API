from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def data_dir() -> Path:
    """Local data directory. Tests can isolate it with APP_DATA_DIR."""
    path = Path(os.environ.get("APP_DATA_DIR", ROOT / "data"))
    path.mkdir(parents=True, exist_ok=True)
    return path


def token_file() -> Path:
    return data_dir() / "upstox_token.json"


def oauth_state_file() -> Path:
    return data_dir() / "oauth_state.txt"
