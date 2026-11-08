from __future__ import annotations

from urllib.parse import urlencode

import httpx

from app.settings import settings


def build_authorize_url(config, state: str) -> str:
    params = {
        "response_type": "code",
        "client_id": config.upstox_client_id,
        "redirect_uri": config.upstox_redirect_uri,
        "state": state,
    }
    return f"{config.upstox_api_base}/v2/login/authorization/dialog?{urlencode(params)}"


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
            headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        payload = response.json()
    if "access_token" not in payload:
        raise RuntimeError("Upstox response did not contain an access_token")
    return payload


class UpstoxClient:
    def __init__(self, access_token: str):
        self.client = httpx.AsyncClient(
            base_url=settings.upstox_api_base,
            timeout=60,
            headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
        )

    async def historical_candles(self, instrument_key: str, unit: str, interval: int,
                                 to_date: str, from_date: str) -> dict:
        # Upstox V3 history endpoint. Use exact option instrument_key for option-premium candles.
        path = (
            f"/v3/historical-candle/{instrument_key}/{unit}/{interval}/"
            f"{to_date}/{from_date}"
        )
        response = await self.client.get(path)
        response.raise_for_status()
        return response.json()

    async def close(self):
        await self.client.aclose()
