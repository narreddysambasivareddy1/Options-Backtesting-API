# Options Backtesting API (starter)

A paper-only FastAPI foundation for the NIFTY and BANK NIFTY historical options backtest discussed in ChatGPT.

## Current scope
- Upstox OAuth 2.0 login and callback.
- Local access-token storage (never commit it).
- Upstox V3 historical candle download helper.
- CSV candle import endpoint for testing without Upstox.
- ATM strike helper and a generic backtest engine.
- Backtest report with trades, net P&L, win rate and drawdown.

## Important limitations
1. The actual entry/exit strategy has **not** been specified in the conversation. The API deliberately refuses to run a strategy until one is supplied; it does not invent trading rules.
2. October 2026 expired-option contracts and candles must be available from Upstox for the exact instruments and interval. Index candles alone are not option-premium candles.
3. Validate contract expiry, strike interval, lot size, trading holidays, timestamps, charges, slippage and missing candles before trusting any result.
4. This project does not place orders. It is for historical research/paper backtesting only.

## Setup (Windows PowerShell)
```powershell
cd options-backtesting-api
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`:
- `UPSTOX_CLIENT_ID`: API Key from Upstox (not UCC).
- `UPSTOX_CLIENT_SECRET`: API Secret.
- `UPSTOX_REDIRECT_URI`: must exactly match the redirect URI registered in the Upstox app. For local development, register `http://127.0.0.1:8000/auth/callback` in the app first, if Upstox allows it.

Run:
```powershell
uvicorn app.main:app --reload
```
Open `http://127.0.0.1:8000/docs`, then call `GET /auth/login`.

After successful Upstox login, Upstox redirects to `/auth/callback`; the backend exchanges the one-time code and stores the token in `data/upstox_token.json`. Keep that file private. Upstox access tokens expire at 3:30 AM the following day.

## Endpoints
- `GET /health`
- `GET /auth/login`
- `GET /auth/callback`
- `GET /auth/status`
- `GET /api/historical-candles?instrument_key=...&from_date=2026-10-01&to_date=2026-10-31&unit=minutes&interval=1`
- `POST /api/backtests/atm-preview`
- `POST /api/backtests/run` (intentionally returns 400 until a strategy is configured)
- `POST /api/candles/import-csv` (upload a CSV with timestamp, open, high, low, close, volume)

## Run tests
```powershell
pytest
```

## Next required decisions
Before a real options backtest, define:
- Entry signal and time.
- Call/put selection rule.
- Exit signal, stop loss, target, and end-of-day exit.
- Whether one trade or multiple trades per day.
- Quantity/lot sizing.
- Brokerage, taxes and slippage assumptions.
