from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from app.paths import data_dir


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, path=None):
        self.path = path or (data_dir() / "backtests.db")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS strategies (
                    id TEXT PRIMARY KEY,
                    created_at TEXT NOT NULL,
                    config_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS backtests (
                    id TEXT PRIMARY KEY,
                    strategy_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    report_json TEXT,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    backtest_id TEXT NOT NULL,
                    trade_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS downloads (
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL,
                    day TEXT NOT NULL,
                    status TEXT NOT NULL,
                    detail TEXT,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (kind, key, day)
                );
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def save_strategy(self, strategy_id: str, config: dict) -> dict:
        with self._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO strategies (id, created_at, config_json) VALUES (?, ?, ?)",
                (strategy_id, _now(), json.dumps(config)),
            )
        return {"strategy_id": strategy_id, "config": config}

    def list_strategies(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT id, created_at, config_json FROM strategies ORDER BY created_at DESC").fetchall()
        return [
            {"strategy_id": row["id"], "created_at": row["created_at"], "config": json.loads(row["config_json"])}
            for row in rows
        ]

    def get_strategy(self, strategy_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id, created_at, config_json FROM strategies WHERE id = ?", (strategy_id,)).fetchone()
        if row is None:
            return None
        return {"strategy_id": row["id"], "created_at": row["created_at"], "config": json.loads(row["config_json"])}

    def save_backtest(self, backtest_id: str, strategy_id: str | None, status: str,
                      config: dict, report: dict | None, error: str | None, trades: list[dict]) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO backtests
                (id, strategy_id, status, created_at, config_json, report_json, error)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (backtest_id, strategy_id, status, _now(), json.dumps(config),
                 json.dumps(report) if report is not None else None, error),
            )
            conn.execute("DELETE FROM trades WHERE backtest_id = ?", (backtest_id,))
            conn.executemany(
                "INSERT INTO trades (backtest_id, trade_json) VALUES (?, ?)",
                [(backtest_id, json.dumps(trade)) for trade in trades],
            )

    def get_backtest(self, backtest_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM backtests WHERE id = ?", (backtest_id,)).fetchone()
        if row is None:
            return None
        return {
            "backtest_id": row["id"],
            "strategy_id": row["strategy_id"],
            "status": row["status"],
            "created_at": row["created_at"],
            "config": json.loads(row["config_json"]),
            "report": json.loads(row["report_json"]) if row["report_json"] else None,
            "error": row["error"],
        }

    def get_trades(self, backtest_id: str) -> list[dict] | None:
        found = self.get_backtest(backtest_id)
        if found is None:
            return None
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT trade_json FROM trades WHERE backtest_id = ? ORDER BY id", (backtest_id,)
            ).fetchall()
        return [json.loads(row["trade_json"]) for row in rows]

    def record_download(self, kind: str, key: str, day: str, status: str, detail: str = "") -> None:
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO downloads (kind, key, day, status, detail, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, key, day) DO UPDATE SET
                    status = excluded.status,
                    detail = excluded.detail,
                    updated_at = excluded.updated_at
                """,
                (kind, key, day, status, detail[:500], _now()),
            )

    def download_rows(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT kind, key, day, status, detail, updated_at FROM downloads ORDER BY updated_at DESC"
            ).fetchall()
        return [dict(row) for row in rows]
