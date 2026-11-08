from __future__ import annotations

import math
from typing import Iterable


def atm_strike(spot_price: float, strike_step: int) -> int:
    """Round spot to nearest listed strike increment."""
    if spot_price <= 0 or strike_step <= 0:
        raise ValueError("spot_price and strike_step must be positive")
    return int(math.floor((spot_price / strike_step) + 0.5) * strike_step)


def calculate_report(trades: Iterable[dict], starting_capital: float) -> dict:
    rows = list(trades)
    pnls = [float(t["net_pnl"]) for t in rows]
    wins = sum(1 for p in pnls if p > 0)
    equity = starting_capital
    peak = equity
    max_drawdown = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak:
            max_drawdown = max(max_drawdown, (peak - equity) / peak)
    total = sum(pnls)
    return {
        "trade_count": len(rows),
        "winning_trades": wins,
        "losing_trades": sum(1 for p in pnls if p < 0),
        "win_rate_percent": round((wins / len(rows) * 100), 2) if rows else 0.0,
        "gross_net_pnl": round(total, 2),
        "ending_capital": round(starting_capital + total, 2),
        "max_drawdown_percent": round(max_drawdown * 100, 2),
        "trades": rows,
    }
