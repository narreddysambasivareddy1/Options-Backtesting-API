from __future__ import annotations

from datetime import date, datetime, time, timedelta

from app.services.costs import apply_slippage, charge_breakdown
from app.services.instruments import select_contract, select_expiry
from app.services.quality import IST, validate_candles

CONFLICT_NOTE = (
    "stop_loss_filled_before_target_because_intrabar_order_is_unknown"
)


def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=IST)


def _find(candles: list[dict], stamp: datetime) -> dict | None:
    for candle in candles:
        if candle["timestamp"] == stamp:
            return candle
    return None


def entry_allowed(entries_today: int, open_positions: int, realized_today: float, config: dict) -> tuple[bool, str]:
    if entries_today >= int(config["max_trades_per_day"]):
        return False, "maximum daily trades reached"
    if open_positions >= int(config["max_open_positions"]):
        return False, "maximum open positions reached"
    if realized_today <= -float(config["max_daily_loss"]):
        return False, "maximum daily loss reached"
    return True, ""


def _walk_exit(candles: list[dict], entry_fill: float, config: dict) -> dict:
    target = entry_fill * (1 + float(config["target_percent"]) / 100)
    stop = entry_fill * (1 - float(config["stop_loss_percent"]) / 100)
    exit_at = _at(candles[0]["timestamp"].date(), time.fromisoformat(config["exit_time"]))
    for candle in candles:
        hit_stop = candle["low"] <= stop
        hit_target = candle["high"] >= target
        if hit_stop and hit_target:
            raw = stop
            reason = "stop_loss"
            return {
                "exit_timestamp": candle["timestamp"],
                "exit_raw": raw,
                "exit_reason": reason,
                "same_candle_conflict": True,
                "execution_assumption": CONFLICT_NOTE,
            }
        if hit_stop:
            return {
                "exit_timestamp": candle["timestamp"],
                "exit_raw": stop,
                "exit_reason": "stop_loss",
                "same_candle_conflict": False,
                "execution_assumption": None,
            }
        if hit_target:
            return {
                "exit_timestamp": candle["timestamp"],
                "exit_raw": target,
                "exit_reason": "target",
                "same_candle_conflict": False,
                "execution_assumption": None,
            }
        if candle["timestamp"] == exit_at:
            return {
                "exit_timestamp": candle["timestamp"],
                "exit_raw": candle["open"],
                "exit_reason": "exit_time",
                "same_candle_conflict": False,
                "execution_assumption": None,
            }
    return {
        "exit_timestamp": None,
        "exit_raw": None,
        "exit_reason": "exit_candle_missing",
        "same_candle_conflict": False,
        "execution_assumption": None,
    }


async def run_backtest(config: dict, source) -> dict:
    """Paper-only long-option simulation. The only entry signal is scheduled_time."""
    if config.get("entry_signal") != "scheduled_time":
        raise ValueError("Only the scheduled_time entry signal is implemented. No other signal was assumed.")
    symbol = config["symbol"]
    start = date.fromisoformat(config["from_date"])
    end = date.fromisoformat(config["to_date"])
    entry_clock = time.fromisoformat(config["entry_time"])
    exit_clock = time.fromisoformat(config["exit_time"])
    expiries = await source.expiries(symbol)
    trades: list[dict] = []
    skipped: list[dict] = []
    day = start
    while day <= end:
        if day.weekday() >= 5:
            skipped.append({"date": day.isoformat(), "reason": "weekend"})
            day += timedelta(days=1)
            continue
        index_rows = await source.index_candles(symbol, day)
        entry_stamp = _at(day, entry_clock)
        exit_stamp = _at(day, exit_clock)
        index_quality = validate_candles(index_rows, window_start=entry_stamp, window_end=entry_stamp)
        spot_candle = _find(index_quality["candles"], entry_stamp)
        if spot_candle is None:
            skipped.append({
                "date": day.isoformat(),
                "reason": "no_index_candle_at_entry",
                "detail": "No index session candle was available. Holiday and missing data are not filled in.",
            })
            day += timedelta(days=1)
            continue
        allowed, why = entry_allowed(0, 0, 0.0, config)
        if not allowed:
            skipped.append({"date": day.isoformat(), "reason": why})
            day += timedelta(days=1)
            continue
        try:
            expiry = select_expiry(expiries, day, config["expiry_rule"], _optional_date(config.get("expiry_date")))
            contracts = await source.contracts(symbol, expiry)
            selected = select_contract(
                contracts, spot_candle["open"], config["option_type"], int(config["strike_offset_steps"])
            )
        except ValueError as exc:
            skipped.append({"date": day.isoformat(), "reason": "contract_selection_failed", "detail": str(exc)})
            day += timedelta(days=1)
            continue
        option_rows = await source.option_candles(selected["instrument_key"], day)
        quality = validate_candles(option_rows, window_start=entry_stamp, window_end=exit_stamp)
        if quality["duplicate_timestamps"] or quality["errors"] or quality["missing_timestamps"]:
            skipped.append({
                "date": day.isoformat(),
                "reason": "option_candles_incomplete",
                "instrument_key": selected["instrument_key"],
                "errors": quality["errors"],
                "missing_timestamps": quality["missing_timestamps"],
                "duplicate_timestamps": quality["duplicate_timestamps"],
                "zero_volume_timestamps": quality["zero_volume_timestamps"],
            })
            day += timedelta(days=1)
            continue
        entry_candle = _find(quality["candles"], entry_stamp)
        if entry_candle is None:
            skipped.append({"date": day.isoformat(), "reason": "option_entry_candle_missing"})
            day += timedelta(days=1)
            continue
        reference = entry_candle["open"]
        premium_min = config.get("entry_premium_min")
        premium_max = config.get("entry_premium_max")
        if premium_min is not None and reference < float(premium_min):
            skipped.append({"date": day.isoformat(), "reason": "premium_below_configured_minimum"})
            day += timedelta(days=1)
            continue
        if premium_max is not None and reference > float(premium_max):
            skipped.append({"date": day.isoformat(), "reason": "premium_above_configured_maximum"})
            day += timedelta(days=1)
            continue
        entry_fill = apply_slippage(reference, float(config["costs"]["slippage_percent"]), "buy")
        path = [item for item in quality["candles"] if entry_stamp <= item["timestamp"] <= exit_stamp]
        outcome = _walk_exit(path, entry_fill, config)
        if outcome["exit_raw"] is None:
            skipped.append({
                "date": day.isoformat(),
                "reason": outcome["exit_reason"],
                "instrument_key": selected["instrument_key"],
            })
            day += timedelta(days=1)
            continue
        exit_fill = apply_slippage(outcome["exit_raw"], float(config["costs"]["slippage_percent"]), "sell")
        quantity = int(selected["lot_size"]) * int(config["quantity_lots"])
        charges = charge_breakdown(entry_fill, exit_fill, quantity, config["costs"])
        gross = (exit_fill - entry_fill) * quantity
        net = gross - charges["total_charges"]
        realized_today = net
        entries_today = 1
        allowed_more, _ = entry_allowed(entries_today, 0, realized_today, config)
        trades.append({
            "date": day.isoformat(),
            "symbol": symbol,
            "option_type": selected["option_type"],
            "expiry": selected["expiry"],
            "trading_symbol": selected["trading_symbol"],
            "instrument_key": selected["instrument_key"],
            "spot_at_entry": round(spot_candle["open"], 4),
            "strike_step": selected["strike_step"],
            "theoretical_atm": selected["theoretical_atm"],
            "strike": selected["strike"],
            "lot_size": selected["lot_size"],
            "quantity_lots": int(config["quantity_lots"]),
            "quantity": quantity,
            "entry_time": entry_stamp.isoformat(),
            "entry_reference": round(reference, 4),
            "entry_price": round(entry_fill, 4),
            "exit_time": outcome["exit_timestamp"].isoformat(),
            "exit_price": round(exit_fill, 4),
            "exit_reason": outcome["exit_reason"],
            "same_candle_conflict": outcome["same_candle_conflict"],
            "execution_assumption": outcome["execution_assumption"],
            "gross_pnl": round(gross, 4),
            "charges": charges,
            "net_pnl": round(net, 4),
            "zero_volume_timestamps": quality["zero_volume_timestamps"],
            "further_entries_blocked": not allowed_more,
        })
        day += timedelta(days=1)
    return {"trades": trades, "skipped": skipped, "expiries": [item.isoformat() for item in expiries]}


def _optional_date(value: str | None) -> date | None:
    if not value:
        return None
    return date.fromisoformat(value)
