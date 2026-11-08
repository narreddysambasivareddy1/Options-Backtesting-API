from __future__ import annotations

import io
import json
import logging
import secrets
import uuid
from datetime import date
from typing import Any

import pandas as pd
from fastapi import Depends, FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response

from app.paths import ROOT
from app.schemas import AtmPreviewRequest, RunRequest, StrategyConfig
from app.services.backtest import atm_strike
from app.services.engine import run_backtest
from app.services.market import UpstoxSource, data_status
from app.services.reports import build_report, trades_csv, trades_workbook
from app.services.store import Store
from app.services.tokens import (
    auth_status,
    clear_oauth_state,
    read_oauth_state,
    read_token_payload,
    save_oauth_state,
    save_token,
)
from app.services.upstox import UpstoxClient, UpstoxError, build_authorize_url, exchange_code, underlying_key
from app.settings import settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("app.main")

app = FastAPI(
    title="Options Backtesting API",
    version="0.2.0",
    description="Paper-only historical options research API. No order placement.",
)

_candles_cache: dict[str, list[dict[str, Any]]] = {}


def get_store() -> Store:
    return Store()


def require_token() -> str:
    status = auth_status()
    if not status["authenticated"]:
        if status["expired"]:
            raise HTTPException(401, "Upstox access token has expired. Open /auth/login again.")
        raise HTTPException(401, "Not authenticated. Open /auth/login first.")
    payload = read_token_payload()
    assert payload is not None
    return str(payload["access_token"])


async def get_source(token: str = Depends(require_token)) -> UpstoxSource:
    return UpstoxSource(UpstoxClient(token), Store())


def resolve_source() -> UpstoxSource:
    return UpstoxSource(UpstoxClient(require_token()), Store())


@app.get("/")
def root():
    return {
        "service": "Options Backtesting API",
        "mode": "paper_backtesting_only",
        "dashboard": "/dashboard",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/dashboard")
def dashboard():
    page = ROOT / "app" / "static" / "dashboard.html"
    return FileResponse(page)


@app.get("/health")
def health():
    return {"status": "ok", "mode": "paper_backtesting_only"}


@app.get("/auth/login")
def auth_login():
    if not settings.upstox_client_id or not settings.upstox_redirect_uri:
        raise HTTPException(500, "Set UPSTOX_CLIENT_ID and UPSTOX_REDIRECT_URI in .env")
    state = secrets.token_urlsafe(24)
    save_oauth_state(state)
    return RedirectResponse(build_authorize_url(settings, state))


@app.get("/auth/callback")
async def auth_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error:
        raise HTTPException(400, f"Upstox authorization failed: {error}")
    if not code:
        raise HTTPException(400, "Missing authorization code from Upstox")
    expected = read_oauth_state()
    if not expected or not state or not secrets.compare_digest(state, expected):
        raise HTTPException(400, "OAuth state mismatch. Start again at /auth/login")
    clear_oauth_state()
    try:
        payload = await exchange_code(code)
        save_token(payload)
    except Exception as exc:
        logger.info("token exchange failed status=%s", getattr(exc, "status_code", "n/a"))
        raise HTTPException(502, f"Token exchange failed: {exc}") from exc
    return {"message": "Upstox authorization completed. Token stored locally; do not share it."}


@app.get("/auth/status")
def auth_status_route():
    return auth_status()


@app.get("/api/instruments")
async def instruments(
    symbol: str = Query("NIFTY", pattern="^(NIFTY|BANKNIFTY)$"),
    expiry: date | None = None,
    source: UpstoxSource = Depends(get_source),
):
    try:
        expiries = await source.expiries(symbol)
        contracts = await source.contracts(symbol, expiry) if expiry else []
        await source.client.close()
    except Exception as exc:
        await source.client.close()
        raise HTTPException(502, str(exc)) from exc
    october = [item.isoformat() for item in expiries if item.year == 2026 and item.month == 10]
    return {
        "symbol": symbol,
        "instrument_key": underlying_key(symbol),
        "source": "upstox_expired_instruments_v2",
        "expiry_count": len(expiries),
        "october_2026_expiries": october,
        "expiries": [item.isoformat() for item in expiries],
        "contracts": contracts,
        "issues": source.issues,
        "note": "Expired October 2026 contracts come from the expired-instruments API, not the live instrument file.",
    }


@app.get("/api/historical-candles")
async def historical_candles(
    instrument_key: str = Query(..., description="Upstox instrument_key"),
    from_date: date = Query(...),
    to_date: date = Query(...),
    unit: str = Query("minutes", pattern="^(minutes|hours|days|weeks|months)$"),
    interval: int = Query(1, ge=1, le=300),
    expired: bool = Query(False, description="Use the expired-instrument candle API"),
    expired_interval: str = Query("1minute", pattern="^(1minute|30minute|day|week|month)$"),
    token: str = Depends(require_token),
):
    if from_date > to_date:
        raise HTTPException(400, "from_date must be on or before to_date")
    client = UpstoxClient(token)
    try:
        if expired:
            data = await client.expired_historical_candles(
                instrument_key, expired_interval, to_date.isoformat(), from_date.isoformat()
            )
        else:
            data = await client.historical_candles(
                instrument_key, unit, interval, to_date.isoformat(), from_date.isoformat()
            )
        candles = data.get("data", {}).get("candles", [])
        return {"instrument_key": instrument_key, "count": len(candles), "candles": candles, "expired": expired}
    except UpstoxError as exc:
        raise HTTPException(502, str(exc)) from exc
    finally:
        await client.close()


@app.post("/api/backtests/strategy")
def save_strategy(body: StrategyConfig, store: Store = Depends(get_store)):
    strategy_id = uuid.uuid4().hex
    return store.save_strategy(strategy_id, body.as_engine())


@app.get("/api/backtests/strategy")
def list_strategies(store: Store = Depends(get_store)):
    rows = store.list_strategies()
    return {"count": len(rows), "strategies": rows}


@app.post("/api/backtests/run")
async def run(body: RunRequest, store: Store = Depends(get_store)):
    if body.strategy is not None:
        config = body.strategy.as_engine()
        strategy_id = body.strategy_id
    else:
        saved = store.get_strategy(body.strategy_id or "")
        if saved is None:
            raise HTTPException(404, "Saved strategy was not found")
        config = saved["config"]
        strategy_id = saved["strategy_id"]
    backtest_id = uuid.uuid4().hex
    source = None
    try:
        source = resolve_source()
        result = await run_backtest(config, source)
        report = build_report(result["trades"], float(config["starting_capital"]), config, result["skipped"])
        report["backtest_id"] = backtest_id
        report["data_quality"]["provider_issues"] = source.issues
        report["data_quality"]["available_expiries"] = result["expiries"]
        store.save_backtest(backtest_id, strategy_id, "completed", config, report, None, result["trades"])
        _write_report_files(backtest_id, report)
        return {
            "backtest_id": backtest_id,
            "status": "completed",
            "trade_count": report["trade_count"],
            "net_profit": report["net_profit"],
            "data_quality": report["data_quality"],
            "message": "Paper backtest completed from available candles. Skipped sessions are listed in data_quality.",
        }
    except ValueError as exc:
        store.save_backtest(backtest_id, strategy_id, "failed", config, None, str(exc), [])
        raise HTTPException(400, str(exc)) from exc
    finally:
        if source is not None:
            await source.client.close()


@app.get("/api/backtests/{backtest_id}")
def get_backtest(backtest_id: str, store: Store = Depends(get_store)):
    row = store.get_backtest(backtest_id)
    if row is None:
        raise HTTPException(404, "Backtest was not found")
    return row


@app.get("/api/backtests/{backtest_id}/trades")
def get_trades(backtest_id: str, store: Store = Depends(get_store)):
    trades = store.get_trades(backtest_id)
    if trades is None:
        raise HTTPException(404, "Backtest was not found")
    return {"backtest_id": backtest_id, "count": len(trades), "trades": trades}


@app.get("/api/backtests/{backtest_id}/report")
def get_report(
    backtest_id: str,
    report_format: str = Query("json", alias="format", pattern="^(json|csv|xlsx)$"),
    store: Store = Depends(get_store),
):
    row = store.get_backtest(backtest_id)
    if row is None or row["report"] is None:
        raise HTTPException(404, "Backtest report was not found")
    report = row["report"]
    if report_format == "json":
        return report
    if report_format == "csv":
        return Response(
            content=trades_csv(report),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{backtest_id}.csv"'},
        )
    return Response(
        content=trades_workbook(report),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{backtest_id}.xlsx"'},
    )


@app.post("/api/backtests/atm-preview")
def atm_preview(body: AtmPreviewRequest):
    return {"spot_price": body.spot_price, "atm_strike": atm_strike(body.spot_price, body.strike_step)}


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
        _candles_cache[key] = [{**row, "timestamp": row["timestamp"].isoformat()} for row in records]
        return {"dataset_id": key, "rows": len(records), "columns": list(frame.columns)}
    except Exception as exc:
        raise HTTPException(400, f"Invalid candle CSV: {exc}") from exc


@app.get("/api/data/status")
def data_status_route():
    return data_status()


def _write_report_files(backtest_id: str, report: dict) -> None:
    try:
        from app.paths import data_dir
        folder = data_dir() / "reports"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{backtest_id}.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        (folder / f"{backtest_id}.csv").write_text(trades_csv(report), encoding="utf-8")
        (folder / f"{backtest_id}.xlsx").write_bytes(trades_workbook(report))
    except OSError:
        logger.info("report file export failed for backtest %s", backtest_id)
