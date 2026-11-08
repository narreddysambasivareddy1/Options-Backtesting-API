"""Check whether Upstox currently returns October 2026 index and expired-option data.

Prints counts and error text only. Does not print credentials or access tokens.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import date, timedelta

from app.paths import token_file
from app.services.instruments import infer_strike_step, parse_contracts, parse_expiries
from app.services.market import candles_from_upstox
from app.services.upstox import UpstoxClient, UpstoxError, underlying_key


async def _index_counts(client: UpstoxClient, key: str) -> dict:
    counts = {}
    for day in ("2026-10-01", "2026-10-15", "2026-10-30"):
        try:
            payload = await client.historical_candles(key, "minutes", 1, day, day)
            counts[day] = len(candles_from_upstox(payload))
        except (UpstoxError, ValueError) as exc:
            counts[day] = str(exc)
    return counts


async def _sample(client: UpstoxClient, symbol: str) -> dict:
    key = underlying_key(symbol)
    result: dict = {"symbol": symbol, "instrument_key": key}
    try:
        payload = await client.expired_expiries(key)
        expiries = [item.isoformat() for item in parse_expiries(payload) if item.year == 2026 and item.month == 10]
        result["october_2026_expiries"] = expiries
    except (UpstoxError, ValueError) as exc:
        result["expiries_error"] = str(exc)
        result["index_candle_counts"] = await _index_counts(client, key)
        return result
    if not expiries:
        result["contracts_error"] = "No October 2026 expiries were returned"
        return result
    expiry = expiries[0]
    try:
        raw = await client.expired_option_contracts(key, expiry)
        contracts = parse_contracts(raw)
        strikes = [row["strike"] for row in contracts if row["option_type"] == "CE"]
        result["sample_expiry"] = expiry
        result["contract_count"] = len(contracts)
        result["lot_sizes"] = sorted({row["lot_size"] for row in contracts})
        result["ce_strike_step"] = infer_strike_step(strikes) if len(set(strikes)) > 1 else None
        sample = next(row for row in contracts if row["option_type"] == "CE")
        result["sample_contract"] = sample["instrument_key"]
    except (UpstoxError, ValueError, StopIteration) as exc:
        result["contracts_error"] = str(exc)
        return result
    candle_checks = {}
    for day in ("2026-10-01", "2026-10-15", "2026-10-30"):
        try:
            index_payload = await client.historical_candles(key, "minutes", 1, day, day)
            candle_checks[f"index_{day}"] = len(candles_from_upstox(index_payload))
        except (UpstoxError, ValueError) as exc:
            candle_checks[f"index_{day}"] = str(exc)
        try:
            option_payload = await client.expired_historical_candles(
                result["sample_contract"], "1minute", day, day
            )
            candle_checks[f"option_{day}"] = len(candles_from_upstox(option_payload))
        except (UpstoxError, ValueError) as exc:
            candle_checks[f"option_{day}"] = str(exc)
    result["candle_counts"] = candle_checks
    return result


async def main() -> None:
    path = token_file()
    if not path.exists():
        print(json.dumps({"authenticated": False}))
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    client = UpstoxClient(str(payload["access_token"]))
    try:
        report = {
            "authenticated": True,
            "symbols": [await _sample(client, "NIFTY"), await _sample(client, "BANKNIFTY")],
        }
    finally:
        await client.close()
    print(json.dumps(report, indent=2))


async def cache_index() -> None:
    from app.services.market import UpstoxSource
    from app.services.tokens import read_token_payload

    payload = read_token_payload()
    if payload is None:
        print(json.dumps({"authenticated": False}))
        return
    client = UpstoxClient(str(payload["access_token"]))
    source = UpstoxSource(client)
    day = date(2026, 10, 1)
    end = date(2026, 10, 31)
    rows = []
    try:
        while day <= end:
            if day.weekday() < 5:
                for symbol in ("NIFTY", "BANKNIFTY"):
                    candles = await source.index_candles(symbol, day)
                    rows.append({"symbol": symbol, "day": day.isoformat(), "count": len(candles)})
            day += timedelta(days=1)
    finally:
        await client.close()
    print(json.dumps(rows))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "cache-index":
        asyncio.run(cache_index())
    else:
        asyncio.run(main())
