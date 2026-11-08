import asyncio
import base64
import json
from datetime import date, datetime, time, timedelta

import httpx
from fastapi.testclient import TestClient

from app.main import app
from app.paths import token_file
from app.services.costs import apply_slippage, charge_breakdown
from app.services.engine import entry_allowed, run_backtest
from app.services.instruments import (
    infer_strike_step,
    nearest_listed_strike,
    parse_contract,
    select_contract,
    select_expiry,
)
from app.services.quality import IST, validate_candles
from app.services.reports import build_report
from app.services.upstox import UpstoxClient, UpstoxError


def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=IST)


def _series(day: date, start: time, end: time, **fields) -> list[dict]:
    cursor = _at(day, start)
    stop = _at(day, end)
    rows = []
    while cursor <= stop:
        row = {"timestamp": cursor, "open": 100.0, "high": 101.0, "low": 99.0, "close": 100.0, "volume": 10}
        row.update(fields)
        rows.append(row)
        cursor += timedelta(minutes=1)
    return rows


def _costs(**overrides) -> dict:
    costs = {
        "brokerage_per_order": 0,
        "stt_sell_percent": 0,
        "exchange_txn_percent": 0,
        "sebi_per_crore": 0,
        "stamp_buy_percent": 0,
        "gst_percent": 0,
        "slippage_percent": 0,
    }
    costs.update(overrides)
    return costs


def _config(**overrides) -> dict:
    config = {
        "symbol": "NIFTY",
        "from_date": "2026-10-05",
        "to_date": "2026-10-05",
        "entry_time": "09:20:00",
        "exit_time": "09:22:00",
        "entry_signal": "scheduled_time",
        "option_type": "CE",
        "strike_offset_steps": 0,
        "expiry_rule": "weekly",
        "expiry_date": None,
        "entry_premium_min": None,
        "entry_premium_max": None,
        "target_percent": 10,
        "stop_loss_percent": 20,
        "max_trades_per_day": 1,
        "max_daily_loss": 10000,
        "max_open_positions": 1,
        "quantity_lots": 1,
        "starting_capital": 100000,
        "costs": _costs(),
    }
    config.update(overrides)
    return config


def _contract(strike: int, option_type: str, lot: int = 50, expiry: str = "2026-10-06") -> dict:
    return {
        "instrument_key": f"NSE_FO|NIFTY|{expiry}|{strike}|{option_type}",
        "trading_symbol": f"NIFTY{strike}{option_type}",
        "expiry": expiry,
        "strike": float(strike),
        "option_type": option_type,
        "lot_size": lot,
    }


class MemorySource:
    def __init__(self):
        self.expiry_list = [date(2026, 10, 6), date(2026, 10, 27)]
        self.contract_map = {
            "2026-10-06": [
                _contract(24000, "CE"), _contract(24100, "CE"), _contract(24200, "CE"),
                _contract(24000, "PE"), _contract(24100, "PE"), _contract(24200, "PE"),
            ],
            "2026-10-27": [
                _contract(24000, "CE", expiry="2026-10-27"),
                _contract(24100, "CE", expiry="2026-10-27"),
            ],
        }
        self.index = {
            "2026-10-05": [{
                "timestamp": _at(date(2026, 10, 5), time(9, 20)),
                "open": 24120.0, "high": 24200.0, "low": 24100.0, "close": 24180.0, "volume": 5,
            }]
        }
        self.options = {}
        self.issues = []

        class _Client:
            async def close(self):
                return None

        self.client = _Client()

    async def expiries(self, symbol):
        return list(self.expiry_list)

    async def contracts(self, symbol, expiry):
        return list(self.contract_map.get(expiry.isoformat(), []))

    async def index_candles(self, symbol, day):
        return list(self.index.get(day.isoformat(), []))

    async def option_candles(self, instrument_key, day):
        return list(self.options.get((instrument_key, day.isoformat()), []))


def _run(config, source):
    return asyncio.run(run_backtest(config, source))


def test_strike_step_comes_from_listed_strikes():
    assert infer_strike_step([24000, 24200, 24100]) == 100
    assert nearest_listed_strike([24100, 24200], 24150) == 24100


def test_expiry_and_contract_selection():
    expiries = [date(2026, 10, 6), date(2026, 10, 13), date(2026, 10, 27)]
    assert select_expiry(expiries, date(2026, 10, 5), "weekly", None) == date(2026, 10, 6)
    assert select_expiry(expiries, date(2026, 10, 5), "monthly", None) == date(2026, 10, 27)
    assert select_expiry(expiries, date(2026, 10, 5), "specific", date(2026, 10, 13)) == date(2026, 10, 13)
    try:
        select_expiry(expiries, date(2026, 10, 5), "specific", date(2026, 10, 8))
    except ValueError as exc:
        assert "not in the instrument expiries" in str(exc)
    else:
        raise AssertionError("missing expiry should fail")
    contracts = [
        _contract(24000, "CE"), _contract(24100, "CE"),
        _contract(24100, "PE", lot=15), _contract(24200, "PE", lot=15),
    ]
    selected = select_contract(contracts, 24120, "PE", 1)
    assert selected["option_type"] == "PE"
    assert selected["strike"] == 24200
    assert selected["lot_size"] == 15
    assert selected["strike_step"] == 100
    assert selected["spot"] == 24120
    bare = {"instrument_type": "CE", "strike_price": 100, "instrument_key": "K"}
    try:
        parse_contract(bare)
    except ValueError as exc:
        assert "lot_size" in str(exc)
    else:
        raise AssertionError("missing lot size should fail")


def test_candle_quality_missing_duplicate_and_zero_volume():
    day = date(2026, 10, 5)
    rows = _series(day, time(9, 20), time(9, 22))
    rows[1]["volume"] = 0
    quality = validate_candles(rows, window_start=rows[0]["timestamp"], window_end=rows[-1]["timestamp"])
    assert quality["valid"] is True
    assert quality["zero_volume_timestamps"]
    assert quality["missing_timestamps"] == []
    broken = list(rows)
    broken.append(dict(rows[0]))
    duplicate = validate_candles(broken, window_start=rows[0]["timestamp"], window_end=rows[-1]["timestamp"])
    assert duplicate["valid"] is False
    assert duplicate["duplicate_timestamps"]
    gap = [rows[0], rows[2]]
    missing = validate_candles(gap, window_start=rows[0]["timestamp"], window_end=rows[-1]["timestamp"])
    assert missing["missing_timestamps"]
    bad = [dict(rows[0], high=1)]
    invalid = validate_candles(bad)
    assert invalid["valid"] is False


def test_target_stop_eod_and_same_candle_conflict():
    day = date(2026, 10, 5)
    source = MemorySource()
    key = "NSE_FO|NIFTY|2026-10-06|24100|CE"
    bars = _series(day, time(9, 20), time(9, 22))
    bars[2]["high"] = 120
    source.options[(key, day.isoformat())] = bars
    result = _run(_config(), source)
    assert len(result["trades"]) == 1
    trade = result["trades"][0]
    assert trade["entry_price"] == 100
    assert trade["spot_at_entry"] == 24120
    assert trade["exit_reason"] == "target"
    assert trade["exit_price"] == 110
    assert trade["net_pnl"] == 500
    assert trade["quantity"] == 50

    stop_bars = _series(day, time(9, 20), time(9, 22))
    stop_bars[1]["low"] = 70
    source.options[(key, day.isoformat())] = stop_bars
    stopped = _run(_config(), source)["trades"][0]
    assert stopped["exit_reason"] == "stop_loss"
    assert stopped["exit_price"] == 80
    assert stopped["net_pnl"] == -1000

    flat = _series(day, time(9, 20), time(9, 22), open=100, high=105, low=95, close=100)
    flat[-1]["open"] = 97
    source.options[(key, day.isoformat())] = flat
    held = _run(_config(), source)["trades"][0]
    assert held["exit_reason"] == "exit_time"
    assert held["exit_price"] == 97

    conflict = _series(day, time(9, 20), time(9, 22))
    conflict[0]["high"] = 150
    conflict[0]["low"] = 50
    source.options[(key, day.isoformat())] = conflict
    both = _run(_config(), source)["trades"][0]
    assert both["exit_reason"] == "stop_loss"
    assert both["same_candle_conflict"] is True
    assert "intrabar_order_is_unknown" in both["execution_assumption"]


def test_missing_data_is_not_fabricated_and_costs_apply():
    day = date(2026, 10, 5)
    source = MemorySource()
    key = "NSE_FO|NIFTY|2026-10-06|24100|CE"
    gap = _series(day, time(9, 20), time(9, 22))
    del gap[1]
    source.options[(key, day.isoformat())] = gap
    skipped = _run(_config(), source)
    assert skipped["trades"] == []
    assert skipped["skipped"][0]["reason"] == "option_candles_incomplete"

    source.options[(key, day.isoformat())] = _series(day, time(9, 20), time(9, 22), high=120)
    # The series helper sets every bar, including the last high. Target can hit on entry.
    bars = _series(day, time(9, 20), time(9, 22))
    bars[-1]["high"] = 120
    source.options[(key, day.isoformat())] = bars
    charged = _run(_config(costs=_costs(brokerage_per_order=20)), source)["trades"][0]
    assert charged["charges"]["total_charges"] == 40
    assert charged["net_pnl"] == 460

    weekend = _run(_config(from_date="2026-10-03", to_date="2026-10-03"), source)
    assert weekend["trades"] == []
    assert weekend["skipped"][0]["reason"] == "weekend"
    no_index = _run(_config(from_date="2026-10-06", to_date="2026-10-06"), source)
    assert no_index["skipped"][0]["reason"] == "no_index_candle_at_entry"


def test_costs_slippage_drawdown_and_entry_limits():
    assert apply_slippage(100, 1, "buy") == 101
    assert apply_slippage(100, 1, "sell") == 99
    charges = charge_breakdown(100, 110, 50, _costs(brokerage_per_order=20, stt_sell_percent=0.1))
    assert charges["brokerage"] == 40
    assert charges["stt"] == 5.5
    report = build_report(
        [{"net_pnl": 100, "date": "2026-10-05", "symbol": "NIFTY", "option_type": "CE", "charges": {"total_charges": 1}},
         {"net_pnl": -300, "date": "2026-10-06", "symbol": "NIFTY", "option_type": "PE", "charges": {"total_charges": 2}},
         {"net_pnl": 50, "date": "2026-10-06", "symbol": "BANKNIFTY", "option_type": "CE", "charges": {"total_charges": 3}}],
        1000,
        {"symbol": "NIFTY"},
        [{"date": "2026-10-03", "reason": "weekend"}],
    )
    assert report["max_drawdown_amount"] == 300
    assert report["max_drawdown_percent"] == 27.27
    assert report["profit_factor"] == 0.5
    assert report["risk_reward_ratio"] == 0.25
    assert report["total_charges"] == 6
    assert report["by_option_type"]["PE"]["net_pnl"] == -300
    assert report["strategy"]["symbol"] == "NIFTY"
    assert entry_allowed(1, 0, 0, _config()) == (False, "maximum daily trades reached")
    assert entry_allowed(0, 1, 0, _config()) == (False, "maximum open positions reached")
    assert entry_allowed(0, 0, -10000, _config()) == (False, "maximum daily loss reached")


def test_upstox_retries_temporary_failures_and_does_not_retry_auth_failure():
    calls = {"n": 0}

    def recover(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(503, json={"status": "error", "errors": [{"message": "temporary"}]})
        return httpx.Response(200, json={"status": "success", "data": ["2026-10-06"]})

    async def go(handler):
        client = UpstoxClient("super-secret-token", transport=httpx.MockTransport(handler), retry_sleep=0)
        try:
            return await client.expired_expiries("NSE_INDEX|Nifty 50")
        finally:
            await client.close()

    payload = asyncio.run(go(recover))
    assert payload["data"] == ["2026-10-06"]
    assert calls["n"] == 2

    def denied(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(401, json={"status": "error", "errors": [{"errorCode": "UDAPI100016", "message": "Invalid token"}]})

    calls["n"] = 0
    try:
        asyncio.run(go(denied))
    except UpstoxError as exc:
        assert "UDAPI100016" in str(exc)
        assert "super-secret-token" not in str(exc)
    else:
        raise AssertionError("auth failure should raise")
    assert calls["n"] == 1


def test_api_validation_run_and_report(monkeypatch):
    client = TestClient(app)
    rejected = client.post("/api/backtests/strategy", json={"symbol": "NIFTY"})
    assert rejected.status_code == 422
    invented = _config(entry_signal="invented_signal")
    assert client.post("/api/backtests/strategy", json=invented).status_code == 422
    unauthenticated = client.post("/api/backtests/run", json={"strategy": _config()})
    assert unauthenticated.status_code == 401

    source = MemorySource()
    day = date(2026, 10, 5)
    key = "NSE_FO|NIFTY|2026-10-06|24100|CE"
    bars = _series(day, time(9, 20), time(9, 22))
    bars[-1]["high"] = 120
    source.options[(key, day.isoformat())] = bars
    monkeypatch.setattr("app.main.resolve_source", lambda: source)
    created = client.post("/api/backtests/strategy", json=_config())
    assert created.status_code == 200
    listed = client.get("/api/backtests/strategy")
    assert listed.json()["count"] == 1
    ran = client.post("/api/backtests/run", json={"strategy_id": created.json()["strategy_id"]})
    assert ran.status_code == 200, ran.text
    backtest_id = ran.json()["backtest_id"]
    assert ran.json()["net_profit"] == 500
    report = client.get(f"/api/backtests/{backtest_id}/report")
    assert report.json()["strategy"]["entry_signal"] == "scheduled_time"
    trades = client.get(f"/api/backtests/{backtest_id}/trades")
    assert trades.json()["count"] == 1
    csv_response = client.get(f"/api/backtests/{backtest_id}/report", params={"format": "csv"})
    assert "24100" in csv_response.text
    workbook = client.get(f"/api/backtests/{backtest_id}/report", params={"format": "xlsx"})
    assert workbook.status_code == 200
    stored = client.get(f"/api/backtests/{backtest_id}")
    assert stored.json()["status"] == "completed"


def test_auth_status_hides_token_and_csv_import_still_works():
    header = base64.urlsafe_b64encode(b'{"alg":"HS256"}').decode().rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps({"exp": 1, "sub": "T"}).encode()).decode().rstrip("=")
    token_file().write_text(json.dumps({"access_token": f"{header}.{payload}.sig", "user_id": "TEST"}), encoding="utf-8")
    client = TestClient(app)
    status = client.get("/auth/status").json()
    assert status["expired"] is True
    assert status["authenticated"] is False
    assert "access_token" not in status
    csv_body = "timestamp,open,high,low,close,volume\n2026-10-05T09:20:00+05:30,1,2,1,2,3\n"
    imported = client.post(
        "/api/candles/import-csv",
        files={"file": ("candles.csv", csv_body, "text/csv")},
    )
    assert imported.status_code == 200
    assert imported.json()["rows"] == 1
