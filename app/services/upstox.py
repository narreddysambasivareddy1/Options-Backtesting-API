"""Upstox HTTP client.

Official references used by this module:
- OAuth dialog and token: /v2/login/authorization/dialog and /v2/login/authorization/token
- Live historical candles: /v3/historical-candle/{key}/{unit}/{interval}/{to}/{from}
- Expired expiries: /v2/expired-instruments/expiries
- Expired option contracts: /v2/expired-instruments/option/contract
- Expired historical candles: /v2/expired-instruments/historical-candle/{key}/{interval}/{to}/{from}

Expired 1minute history is documented as the last one month of candles up to the end date.
That limit is reported from the API response. This module does not fill gaps.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from app.settings import settings

logger = logging.getLogger("app.upstox")

RETRY_STATUSES = {429, 500, 502, 503, 504}
UNDERLYINGS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
}


class UpstoxError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


def build_authorize_url(config, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": config.upstox_client_id,
        "redirect_uri": config.upstox_redirect_uri,
        "state": state,
    }
    return f"{config.upstox_api_base}/v2/login/authorization/dialog?{urlencode(params)}"


def _error_text(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        text = response.text[:300]
        return f"HTTP {response.status_code}: {text}"
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors:
        parts = []
        for item in errors:
            if isinstance(item, dict):
                code = item.get("errorCode") or item.get("error_code") or ""
                message = item.get("message") or ""
                parts.append(f"{code} {message}".strip())
        if parts:
            return f"HTTP {response.status_code}: {'; '.join(parts)}"
    return f"HTTP {response.status_code}: request failed"


class UpstoxClient:
    def __init__(self, access_token: str, transport: httpx.AsyncBaseTransport | None = None,
                 retry_sleep: float | None = None):
        self.retry_sleep = retry_sleep
        self.client = httpx.AsyncClient(
            base_url=settings.upstox_api_base,
            timeout=60,
            transport=transport,
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, params: dict | None = None) -> dict:
        delays = [0.0, 0.5, 1.0, 2.0, 4.0]
        last_error: Exception | None = None
        for attempt, delay in enumerate(delays):
            if attempt and delay:
                await asyncio.sleep(self.retry_sleep if self.retry_sleep is not None else delay)
            try:
                response = await self.client.request(method, path, params=params)
            except httpx.TransportError as exc:
                last_error = exc
                logger.info("upstox transport failure method=%s path=%s attempt=%s", method, path, attempt + 1)
                continue
            logger.info(
                "upstox response method=%s path=%s status=%s attempt=%s",
                method, path, response.status_code, attempt + 1,
            )
            if response.status_code in RETRY_STATUSES and attempt < len(delays) - 1:
                retry_after = response.headers.get("Retry-After")
                if retry_after and retry_after.isdigit() and self.retry_sleep is None:
                    await asyncio.sleep(min(int(retry_after), 10))
                continue
            if response.status_code >= 400:
                raise UpstoxError(_error_text(response), response.status_code)
            try:
                payload = response.json()
            except Exception as exc:
                raise UpstoxError("Upstox returned a non-JSON response", response.status_code) from exc
            if isinstance(payload, dict) and payload.get("status") == "error":
                raise UpstoxError(_error_text(response), response.status_code)
            return payload
        raise UpstoxError(f"Upstox request failed after retries: {last_error}")

    async def historical_candles(self, instrument_key: str, unit: str, interval: int,
                                 to_date: str, from_date: str) -> dict:
        """V3 history for instruments that are still listed, including spot indices."""
        path = (
            f"/v3/historical-candle/{quote(instrument_key, safe='')}/{unit}/{interval}/"
            f"{to_date}/{from_date}"
        )
        return await self._request("GET", path)

    async def expired_expiries(self, instrument_key: str) -> dict:
        return await self._request(
            "GET",
            "/v2/expired-instruments/expiries",
            params={"instrument_key": instrument_key},
        )

    async def expired_option_contracts(self, instrument_key: str, expiry_date: str) -> dict:
        return await self._request(
            "GET",
            "/v2/expired-instruments/option/contract",
            params={"instrument_key": instrument_key, "expiry_date": expiry_date},
        )

    async def expired_historical_candles(self, instrument_key: str, interval: str,
                                         to_date: str, from_date: str) -> dict:
        path = (
            f"/v2/expired-instruments/historical-candle/{quote(instrument_key, safe='')}/"
            f"{interval}/{to_date}/{from_date}"
        )
        return await self._request("GET", path)


async def exchange_code(code: str) -> dict:
    if not settings.upstox_client_id or not settings.upstox_client_secret:
        raise RuntimeError("Missing Upstox client ID or client secret in .env")
    data = {
        "code": code,
        "client_id": settings.upstox_client_id,
        "client_secret": settings.upstox_client_secret,
        "redirect_uri": settings.upstox_redirect_uri,
        "grant_type": "authorization_code",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            f"{settings.upstox_api_base}/v2/login/authorization/token",
            data=data,
            headers={"Accept": "application/json"},
        )
    logger.info("upstox token exchange status=%s", response.status_code)
    if response.status_code >= 400:
        raise UpstoxError(_error_text(response), response.status_code)
    payload = response.json()
    if "access_token" not in payload:
        raise RuntimeError("Upstox response did not contain an access token")
    return payload


def underlying_key(symbol: str) -> str:
    try:
        return UNDERLYINGS[symbol]
    except KeyError as exc:
        raise ValueError("symbol must be NIFTY or BANKNIFTY") from exc
