from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.services.backtest import atm_strike


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value)
    return date.fromisoformat(text[:10])


def parse_expiries(payload: dict) -> list[date]:
    data = payload.get("data", payload)
    if isinstance(data, dict):
        data = (
            data.get("expiry_dates")
            or data.get("expiries")
            or data.get("expiryDates")
            or data.get("data")
            or []
        )
    if not isinstance(data, list):
        raise ValueError("Upstox expiry payload did not contain a list")
    parsed: list[date] = []
    for item in data:
        if isinstance(item, dict):
            item = item.get("expiry") or item.get("expiry_date") or item.get("date")
            if item is None:
                continue
        parsed.append(_as_date(item))
    if not parsed:
        raise ValueError("Upstox expiry payload did not contain any dates")
    return sorted(set(parsed))


def parse_contract(row: dict) -> dict:
    option_type = str(row.get("instrument_type") or "").upper()
    if option_type not in {"CE", "PE"}:
        raise ValueError("instrument_type must be CE or PE")
    lot_size = row.get("lot_size") or row.get("minimum_lot")
    if lot_size is None:
        raise ValueError(f"Contract {row.get('instrument_key')} is missing lot_size")
    lot = int(lot_size)
    if lot <= 0:
        raise ValueError("lot_size must be positive")
    strike = row.get("strike_price")
    if strike is None:
        raise ValueError(f"Contract {row.get('instrument_key')} is missing strike_price")
    key = row.get("instrument_key")
    if not key:
        raise ValueError("Contract is missing instrument_key")
    return {
        "instrument_key": str(key),
        "trading_symbol": str(row.get("trading_symbol") or ""),
        "expiry": _as_date(row.get("expiry")).isoformat(),
        "strike": float(strike),
        "option_type": option_type,
        "lot_size": lot,
        "weekly": row.get("weekly"),
    }


def parse_contracts(payload: dict) -> list[dict]:
    data = payload.get("data", [])
    if isinstance(data, dict):
        data = data.get("contracts") or data.get("data") or []
    contracts = []
    for row in data:
        if str(row.get("instrument_type") or "").upper() not in {"CE", "PE"}:
            continue
        contracts.append(parse_contract(row))
    return contracts


def infer_strike_step(strikes: list[float]) -> int:
    unique = sorted({int(round(strike)) for strike in strikes})
    diffs = [right - left for left, right in zip(unique, unique[1:]) if right > left]
    if not diffs:
        raise ValueError("At least two listed strikes are required to infer the strike interval")
    return min(diffs)


def nearest_listed_strike(strikes: list[float], target: float) -> int:
    listed = sorted({int(round(strike)) for strike in strikes})
    if not listed:
        raise ValueError("No listed strikes")
    return min(listed, key=lambda strike: (abs(strike - target), strike))


def select_expiry(expiries: list[date], trade_day: date, rule: str, specific: date | None) -> date:
    eligible = [item for item in expiries if item >= trade_day]
    if rule == "specific":
        if specific is None:
            raise ValueError("expiry_date is required when expiry_rule is specific")
        if specific not in expiries:
            raise ValueError(f"Expiry {specific.isoformat()} is not in the instrument expiries")
        if specific < trade_day:
            raise ValueError("Selected expiry is already past on the trade date")
        return specific
    if not eligible:
        raise ValueError(f"No expiry on or after {trade_day.isoformat()}")
    if rule == "weekly":
        return min(eligible)
    if rule == "monthly":
        month = [item for item in eligible if item.month == trade_day.month and item.year == trade_day.year]
        if not month:
            raise ValueError(f"No monthly expiry left in {trade_day.strftime('%Y-%m')}")
        return max(month)
    raise ValueError("expiry_rule must be weekly, monthly, or specific")


def select_contract(contracts: list[dict], spot: float, option_type: str, offset_steps: int) -> dict:
    side = [item for item in contracts if item["option_type"] == option_type]
    if not side:
        raise ValueError(f"No {option_type} contracts for the selected expiry")
    step = infer_strike_step([item["strike"] for item in side])
    theoretical = atm_strike(spot, step)
    target = theoretical + (offset_steps * step)
    chosen = nearest_listed_strike([item["strike"] for item in side], target)
    matches = [item for item in side if int(round(item["strike"])) == chosen]
    selected = dict(matches[0])
    selected["strike_step"] = step
    selected["theoretical_atm"] = theoretical
    selected["spot"] = spot
    selected["strike_offset_steps"] = offset_steps
    return selected
