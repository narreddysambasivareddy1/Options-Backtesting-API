from __future__ import annotations

import base64
import json
from datetime import datetime, timezone

from app.paths import oauth_state_file, token_file


def read_token_payload() -> dict | None:
    path = token_file()
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("access_token"):
        return None
    return payload


def save_token(payload: dict) -> None:
    token_file().write_text(json.dumps(payload, indent=2), encoding="utf-8")


def token_expiry(access_token: str) -> datetime | None:
    try:
        segment = access_token.split(".")[1]
        padding = "=" * (-len(segment) % 4)
        data = json.loads(base64.urlsafe_b64decode(segment + padding))
        exp = data.get("exp")
        if exp is None:
            return None
        return datetime.fromtimestamp(int(exp), tz=timezone.utc)
    except (IndexError, ValueError, json.JSONDecodeError):
        return None


def auth_status() -> dict:
    payload = read_token_payload()
    exists = token_file().exists()
    if payload is None:
        return {
            "authenticated": False,
            "token_file_exists": exists,
            "expired": False,
            "expires_at": None,
            "user_id": None,
        }
    expires = token_expiry(str(payload["access_token"]))
    expired = expires is not None and expires <= datetime.now(timezone.utc)
    return {
        "authenticated": not expired,
        "token_file_exists": True,
        "expired": expired,
        "expires_at": expires.isoformat() if expires else None,
        "user_id": payload.get("user_id"),
    }


def save_oauth_state(state: str) -> None:
    oauth_state_file().write_text(state, encoding="utf-8")


def read_oauth_state() -> str | None:
    path = oauth_state_file()
    if not path.exists():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


def clear_oauth_state() -> None:
    path = oauth_state_file()
    if path.exists():
        path.unlink()
