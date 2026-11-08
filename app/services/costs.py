from __future__ import annotations


def apply_slippage(price: float, slippage_percent: float, side: str) -> float:
    """Apply adverse slippage. Buys fill higher and sells fill lower."""
    if price <= 0:
        raise ValueError("price must be positive")
    if slippage_percent < 0:
        raise ValueError("slippage_percent cannot be negative")
    factor = slippage_percent / 100
    if side == "buy":
        return price * (1 + factor)
    if side == "sell":
        return price * (1 - factor)
    raise ValueError("side must be buy or sell")


def charge_breakdown(entry_price: float, exit_price: float, quantity: int, costs: dict) -> dict:
    buy_turnover = entry_price * quantity
    sell_turnover = exit_price * quantity
    brokerage = float(costs["brokerage_per_order"]) * 2
    stt = sell_turnover * float(costs["stt_sell_percent"]) / 100
    exchange = (buy_turnover + sell_turnover) * float(costs["exchange_txn_percent"]) / 100
    sebi = (buy_turnover + sell_turnover) / 10_000_000 * float(costs["sebi_per_crore"])
    stamp = buy_turnover * float(costs["stamp_buy_percent"]) / 100
    gst = (brokerage + exchange + sebi) * float(costs["gst_percent"]) / 100
    total = brokerage + stt + exchange + sebi + stamp + gst
    return {
        "brokerage": round(brokerage, 4),
        "stt": round(stt, 4),
        "exchange_txn": round(exchange, 4),
        "sebi": round(sebi, 4),
        "stamp": round(stamp, 4),
        "gst": round(gst, 4),
        "total_charges": round(total, 4),
    }
