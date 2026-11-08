from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from app.paths import data_dir
from app.services.instruments import parse_contracts, parse_expiries
from app.services.quality import parse_timestamp
from app.services.store import Store
from app.services.upstox import UpstoxError, underlying_key


def candles_from_upstox(payload: dict) -> list[dict]:
    data = payload.get("data") or {}
    rows = data.get("candles") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        raise ValueError("Upstox candle payload did not contain data.candles")
    candles = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            raise ValueError("Upstox candle row is incomplete")
        candles.append({
            "timestamp": row[0],
            "open": row[1],
            "high": row[2],
            "low": row[3],
            "close": row[4],
            "volume": row[5],
            "open_interest": row[6] if len(row) > 6 else None,
        })
    return candles


def _load_candles(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    for row in raw:
        row["timestamp"] = parse_timestamp(row["timestamp"])
    return raw


def _dump_candles(path: Path, candles: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serializable = []
    for row in candles:
        item = dict(row)
        stamp = item["timestamp"]
        item["timestamp"] = stamp.isoformat() if hasattr(stamp, "isoformat") else str(stamp)
        serializable.append(item)
    path.write_text(json.dumps(serializable), encoding="utf-8")


class UpstoxSource:
    """Disk-cached reads of index candles, expired contracts, and expired option candles."""

    def __init__(self, client, store: Store | None = None):
        self.client = client
        self.store = store or Store()
        self.issues: list[dict] = []

    def _safe(self, instrument_key: str) -> str:
        return instrument_key.replace("|", "_").replace("/", "_").replace("\\", "_")

    async def expiries(self, symbol: str) -> list[date]:
        path = data_dir() / "instruments" / f"{symbol}_expiries.json"
        key = underlying_key(symbol)
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
        else:
            try:
                payload = await self.client.expired_expiries(key)
            except UpstoxError as exc:
                self._issue("expiries", symbol, "", str(exc))
                return []
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.store.record_download("expiries", symbol, "all", "ok", f"{len(payload.get('data', []))} rows")
        try:
            return parse_expiries(payload)
        except ValueError as exc:
            self._issue("expiries", symbol, "", str(exc))
            return []

    async def contracts(self, symbol: str, expiry: date) -> list[dict]:
        path = data_dir() / "instruments" / symbol / f"{expiry.isoformat()}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        try:
            payload = await self.client.expired_option_contracts(underlying_key(symbol), expiry.isoformat())
            contracts = parse_contracts(payload)
        except (UpstoxError, ValueError) as exc:
            self._issue("contracts", symbol, expiry.isoformat(), str(exc))
            return []
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(contracts), encoding="utf-8")
        self.store.record_download("contracts", symbol, expiry.isoformat(), "ok", f"{len(contracts)} contracts")
        return contracts

    async def index_candles(self, symbol: str, day: date) -> list[dict]:
        path = data_dir() / "index" / symbol / f"{day.isoformat()}.json"
        if path.exists():
            return _load_candles(path)
        try:
            payload = await self.client.historical_candles(
                underlying_key(symbol), "minutes", 1, day.isoformat(), day.isoformat()
            )
            candles = candles_from_upstox(payload)
        except (UpstoxError, ValueError) as exc:
            self._issue("index", symbol, day.isoformat(), str(exc))
            return []
        _dump_candles(path, candles)
        self.store.record_download("index", symbol, day.isoformat(), "ok" if candles else "empty", str(len(candles)))
        return _load_candles(path)

    async def option_candles(self, instrument_key: str, day: date) -> list[dict]:
        path = data_dir() / "options" / self._safe(instrument_key) / f"{day.isoformat()}.json"
        if path.exists():
            return _load_candles(path)
        try:
            payload = await self.client.expired_historical_candles(
                instrument_key, "1minute", day.isoformat(), day.isoformat()
            )
            candles = candles_from_upstox(payload)
        except (UpstoxError, ValueError) as exc:
            self._issue("option", instrument_key, day.isoformat(), str(exc))
            return []
        _dump_candles(path, candles)
        self.store.record_download(
            "option", instrument_key, day.isoformat(), "ok" if candles else "empty", str(len(candles))
        )
        return _load_candles(path)

    def _issue(self, kind: str, key: str, day: str, detail: str) -> None:
        self.issues.append({"kind": kind, "key": key, "day": day, "detail": detail})
        self.store.record_download(kind, key, day or "na", "error", detail)


def data_status() -> dict:
    root = data_dir()

    def files(folder: str) -> list[str]:
        base = root / folder
        if not base.exists():
            return []
        return sorted(path.relative_to(root).as_posix() for path in base.rglob("*.json"))

    downloads = Store().download_rows()
    errors = [row for row in downloads if row["status"] == "error"]
    return {
        "index_files": files("index"),
        "option_files": files("options"),
        "instrument_files": files("instruments"),
        "download_count": len(downloads),
        "error_count": len(errors),
        "errors": errors[:20],
        "note": (
            "October 2026 option contracts are expired. Candles are requested from the Upstox "
            "expired-instruments API. On this account that API returned UDAPI1149: it requires an "
            "Upstox Plus subscription. One-minute expired history is also documented as only the "
            "latest month up to the requested end date. Missing files are missing data, not zero-volume candles."
        ),
    }
