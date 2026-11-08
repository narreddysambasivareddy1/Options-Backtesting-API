from __future__ import annotations

import csv
import io
from collections import defaultdict

from openpyxl import Workbook

from app.services.backtest import calculate_report


def _group(trades: list[dict], key: str) -> dict:
    grouped: dict[str, list[dict]] = defaultdict(list)
    for trade in trades:
        grouped[str(trade.get(key))].append(trade)
    return {
        name: {
            "trade_count": len(rows),
            "net_pnl": round(sum(float(row["net_pnl"]) for row in rows), 4),
        }
        for name, rows in sorted(grouped.items())
    }


def build_report(trades: list[dict], starting_capital: float, config: dict, skipped: list[dict]) -> dict:
    base = calculate_report(trades, starting_capital)
    wins = [float(row["net_pnl"]) for row in trades if float(row["net_pnl"]) > 0]
    losses = [float(row["net_pnl"]) for row in trades if float(row["net_pnl"]) < 0]
    charges = round(sum(float(row.get("charges", {}).get("total_charges", 0)) for row in trades), 4)
    gross_profit = round(sum(wins), 4)
    gross_loss = round(sum(losses), 4)
    avg_profit = round(gross_profit / len(wins), 4) if wins else 0.0
    avg_loss = round(gross_loss / len(losses), 4) if losses else 0.0
    profit_factor = round(gross_profit / abs(gross_loss), 4) if gross_loss else None
    risk_reward = round(avg_profit / abs(avg_loss), 4) if avg_loss else None
    daily: dict[str, float] = defaultdict(float)
    for trade in trades:
        daily[trade["date"]] += float(trade["net_pnl"])
    equity = starting_capital
    peak = equity
    max_drawdown_amount = 0.0
    curve = [{"trade_index": 0, "equity": round(equity, 4), "drawdown": 0.0}]
    for index, trade in enumerate(trades, start=1):
        equity += float(trade["net_pnl"])
        peak = max(peak, equity)
        drawdown = peak - equity
        max_drawdown_amount = max(max_drawdown_amount, drawdown)
        curve.append({
            "trade_index": index,
            "equity": round(equity, 4),
            "drawdown": round(drawdown, 4),
        })
    data_problems = [row for row in skipped if row.get("reason") not in {"weekend"}]
    assumptions = sorted({
        row["execution_assumption"]
        for row in trades
        if row.get("execution_assumption")
    })
    base.update({
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "net_profit": base["gross_net_pnl"],
        "total_charges": charges,
        "average_profit_per_winning_trade": avg_profit,
        "average_loss_per_losing_trade": avg_loss,
        "profit_factor": profit_factor,
        "risk_reward_ratio": risk_reward,
        "max_drawdown_amount": round(max_drawdown_amount, 4),
        "daily_pnl": {day: round(value, 4) for day, value in sorted(daily.items())},
        "by_index": _group(trades, "symbol"),
        "by_option_type": _group(trades, "option_type"),
        "equity_curve": curve,
        "strategy": config,
        "data_quality": {
            "skipped": skipped,
            "problem_count": len(data_problems),
            "trades_with_zero_volume_candles": sum(1 for row in trades if row.get("zero_volume_timestamps")),
            "execution_assumptions": assumptions,
        },
    })
    return base


def trades_csv(report: dict) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["# strategy", json_compact(report.get("strategy"))])
    writer.writerow(["# data_quality_problems", report.get("data_quality", {}).get("problem_count")])
    trades = report.get("trades", [])
    fields = [
        "date", "symbol", "option_type", "expiry", "trading_symbol", "instrument_key",
        "spot_at_entry", "strike", "quantity", "entry_time", "entry_price", "exit_time",
        "exit_price", "exit_reason", "execution_assumption", "gross_pnl", "net_pnl",
    ]
    writer.writerow(fields)
    for trade in trades:
        writer.writerow([trade.get(field) for field in fields])
    return buffer.getvalue()


def json_compact(value) -> str:
    import json
    return json.dumps(value, separators=(",", ":"), default=str)


def trades_workbook(report: dict) -> bytes:
    book = Workbook()
    summary = book.active
    summary.title = "Summary"
    summary.append(["field", "value"])
    for key in (
        "trade_count", "winning_trades", "losing_trades", "win_rate_percent",
        "gross_profit", "gross_loss", "net_profit", "total_charges",
        "average_profit_per_winning_trade", "average_loss_per_losing_trade",
        "max_drawdown_percent", "max_drawdown_amount", "profit_factor", "risk_reward_ratio",
    ):
        summary.append([key, report.get(key)])
    summary.append(["strategy", json_compact(report.get("strategy"))])
    summary.append(["data_quality_problems", report.get("data_quality", {}).get("problem_count")])
    sheet = book.create_sheet("Trades")
    trades = report.get("trades", [])
    if trades:
        headers = list(trades[0].keys())
        sheet.append(headers)
        for trade in trades:
            sheet.append([json_compact(trade[header]) if isinstance(trade[header], (dict, list)) else trade[header]
                          for header in headers])
    stream = io.BytesIO()
    book.save(stream)
    return stream.getvalue()
