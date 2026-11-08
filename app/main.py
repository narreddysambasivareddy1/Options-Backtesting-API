from __future__ import annotations

import csv
import io
import json
import secrets
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from app.services.upstox import UpstoxClient, build_authorize_url, exchange_code
from app.services.backtest import atm_strike, calculate_report

app = FastAPI(
    title="Options Backtesting API",
    version="0.1.0",
    description="Paper-only historical options research API. No order placement.",
)

DATA_DIR = Path("data")
TOKEN_FILE = DATA_DIR / "upstox_token.json"
DATA_DIR.mkdir(exist_ok=True)
_oauth_state: str | None = None
_candles_cache: dict[str, list[dict[str, Any]]] = {}


def read_token() -> str | None:
    if not TOKEN_FILE.exists():
        return None
    try:
        return json.loads(TOKEN_FILE.read_text(encoding="utf-8")).get("access_token")
    except (OSError, json.JSONDecodeError):
        return None


def save_token(payload: dict[str, Any]) -> None:
    TOKEN_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")


@app.get("/health")
def health():
    return {"status": "ok", "mode": "paper_backtesting_only"}


@app.get("/auth/login")
def auth_login():
    global _oauth_state
    from app.settings import settings
    if not settings.upstox_client_id or not settings.upstox_redirect_uri:
        raise HTTPException(500, "Set UPSTOX_CLIENT_ID and UPSTOX_REDIRECT_URI in .env")
    _oauth_state = secrets.token_urlsafe(24)
    return RedirectResponse(build_authorize_url(settings, _oauth_state))


@app.get("/auth/callback")
async def auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    global _oauth_state
    if error:
        raise HTTPException(400, f"Upstox authorization failed: {error}")
    if not code:
        raise HTTPException(400, "Missing authorization code from Upstox")
    if not _oauth_state or not state or not secrets.compare_digest(state, _oauth_state):
        raise HTTPException(400, "OAuth state mismatch. Start again at /auth/login")
    _oauth_state = None
    try:
        payload = await exchange_code(code)
        save_token(payload)
    except Exception as exc:
        raise HTTPException(502, f"Token exchange failed: {exc}") from exc
    return {"message": "Upstox authorization completed. Token stored locally; do not share it."}


@app.get("/auth/status")
def auth_status():
    return {"authenticated": bool(read_token()), "token_file_exists": TOKEN_FILE.exists()}


@app.get("/api/historical-candles")
async def historical_candles(
    instrument_key: str = Query(..., description="Exact Upstox instrument_key, e.g. from contract master"),
    from_date: date = Query(...),
    to_date: date = Query(...),
    unit: str = Query("minutes", pattern="^(minutes|hours|days|weeks|months)$"),
    interval: int = Query(1, ge=1, le=300),
):
    if from_date > to_date:
        raise HTTPException(400, "from_date must be on or before to_date")
    token = read_token()
    if not token:
        raise HTTPException(401, "Not authenticated. Open /auth/login first.")
    client = UpstoxClient(token)
    try:
        data = await client.historical_candles(
            instrument_key, unit, interval, to_date.isoformat(), from_date.isoformat()
        )
        candles = data.get("data", {}).get("candles", [])
        return {"instrument_key": instrument_key, "count": len(candles), "candles": candles}
    except Exception as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        await client.close()


class AtmPreviewRequest(BaseModel):
    spot_price: float = Field(gt=0)
    strike_step: int = Field(gt=0, description="Use verified exchange strike interval for the selected index/expiry")


@app.post("/api/backtests/atm-preview")
def atm_preview(body: AtmPreviewRequest):
    return {"spot_price": body.spot_price, "atm_strike": atm_strike(body.spot_price, body.strike_step)}


class BacktestRequest(BaseModel):
    symbol: str = Field(pattern="^(NIFTY|BANKNIFTY)$")
    from_date: date = date(2026, 10, 1)
    to_date: date = date(2026, 10, 31)
    strategy_name: str | None = None
    starting_capital: float = Field(default=100000, gt=0)
    quantity_lots: int = Field(default=1, ge=1)


@app.post("/api/backtests/run")
def run_backtest(body: BacktestRequest):
    if body.from_date > body.to_date:
        raise HTTPException(400, "from_date must be on or before to_date")
    if not body.strategy_name:
        raise HTTPException(
            400,
            "Strategy not configured. Define entry, option type, exit, stop-loss/target, "
            "trade frequency, lot sizing and costs before running a meaningful backtest."
        )
    raise HTTPException(501, "Strategy plug-in is not implemented yet; no trading rules have been assumed.")


@app.post("/api/candles/import-csv")
async def import_candles(file: UploadFile = File(...)):
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(400, "Upload a .csv file")
    raw = await file.read()
    try:
        frame = pd.read_csv(io.BytesIO(raw))
        required = {"timestamp", "open", "high", "low", "close", "volume"}
        missing = required - set(frame.columns)
        if missing:
            raise ValueError(f"Missing columns: {', '.join(sorted(missing))}")
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="raise")
        for col in ("open", "high", "low", "close", "volume"):
            frame[col] = pd.to_numeric(frame[col], errors="raise")
        if frame.empty:
            raise ValueError("CSV has no rows")
        if (frame[["open", "high", "low", "close"]] <= 0).any().any():
            raise ValueError("OHLC prices must be positive")
        key = secrets.token_hex(8)
        records = frame.sort_values("timestamp").to_dict(orient="records")
        _candles_cache[key] = [
            {**r, "timestamp": r["timestamp"].isoformat()} for r in records
        ]
        return {"dataset_id": key, "rows": len(records), "columns": list(frame.columns)}
    except Exception as exc:
        raise HTTPException(400, f"Invalid candle CSV: {exc}") from exc
