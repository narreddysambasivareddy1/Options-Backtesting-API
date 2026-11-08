from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

IST = timezone(timedelta(hours=5, minutes=30))
SESSION_OPEN = time(9, 15)
SESSION_LAST_MINUTE = time(15, 29)


def to_ist(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=IST)
    return value.astimezone(IST)


def parse_timestamp(value: str) -> datetime:
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    return to_ist(parsed)


def session_timestamps(day: date, start: time, end: time) -> list[datetime]:
    cursor = datetime.combine(day, start, tzinfo=IST)
    stop = datetime.combine(day, end, tzinfo=IST)
    stamps: list[datetime] = []
    while cursor <= stop:
        stamps.append(cursor)
        cursor += timedelta(minutes=1)
    return stamps


def validate_candles(rows: Iterable[dict], *, window_start: datetime | None = None,
                     window_end: datetime | None = None) -> dict:
    """Validate OHLCV rows. Missing timestamps stay missing; they are not zero-filled."""
    errors: list[str] = []
    duplicates: list[str] = []
    zero_volume: list[str] = []
    seen: dict[str, int] = {}
    parsed: list[dict] = []
    for index, row in enumerate(rows):
        try:
            stamp = row["timestamp"]
            if not isinstance(stamp, datetime):
                stamp = parse_timestamp(str(stamp))
            else:
                stamp = to_ist(stamp)
            open_ = float(row["open"])
            high = float(row["high"])
            low = float(row["low"])
            close = float(row["close"])
            volume = float(row["volume"])
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"row {index}: {exc}")
            continue
        if min(open_, high, low, close) <= 0:
            errors.append(f"row {index}: OHLC prices must be positive")
            continue
        if high < max(open_, close) or low > min(open_, close) or high < low:
            errors.append(f"row {index}: OHLC relationship is invalid")
            continue
        if volume < 0:
            errors.append(f"row {index}: volume cannot be negative")
            continue
        key = stamp.isoformat()
        seen[key] = seen.get(key, 0) + 1
        if volume == 0:
            zero_volume.append(key)
        parsed.append({
            "timestamp": stamp,
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "open_interest": row.get("open_interest"),
        })
    duplicates = sorted(key for key, count in seen.items() if count > 1)
    if duplicates:
        errors.append("duplicate timestamps: " + ", ".join(duplicates))
    parsed.sort(key=lambda item: item["timestamp"])
    missing: list[str] = []
    if window_start and window_end and not duplicates:
        have = {item["timestamp"] for item in parsed}
        cursor = window_start
        while cursor <= window_end:
            if cursor not in have:
                missing.append(cursor.isoformat())
            cursor += timedelta(minutes=1)
    return {
        "valid": not errors and not missing,
        "errors": errors,
        "duplicate_timestamps": duplicates,
        "missing_timestamps": missing,
        "zero_volume_timestamps": zero_volume,
        "rows": len(parsed),
        "candles": parsed,
    }
