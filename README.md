# Options Backtesting API

Paper-only historical options research for NIFTY and BANKNIFTY. The application downloads Upstox candles, selects the contract from the instrument data, and simulates a configured long-option trade. It does not place orders.

## What it does

- Upstox OAuth login, local token storage, and expiry detection.
- Expired-option expiry list and contract lookup for contracts that are no longer in the live instrument file.
- One-minute index candles and expired option candles, cached under `data/`.
- ATM selection from the listed strikes and the contract lot size. Strike intervals and lot sizes are not hardcoded.
- A configurable paper engine. The only entry signal included is `scheduled_time`. Any other signal is rejected.
- Target, stop-loss, exit time, daily trade limit, daily loss limit, open-position limit, slippage, and itemised charges.
- If target and stop are both touched in one candle, the stop is filled first and the trade records that assumption.
- Missing minutes stop that trade. They are not filled with invented premiums or treated as zero-volume candles.
- SQLite history, JSON, CSV, and Excel reports, and a local dashboard.

## Setup (Windows PowerShell)

```powershell
cd C:\API
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env`:

- `UPSTOX_CLIENT_ID`: API key from the Upstox developer app, not the UCC.
- `UPSTOX_CLIENT_SECRET`: API secret.
- `UPSTOX_REDIRECT_URI`: must exactly match the redirect URL saved in that app. Local value: `http://127.0.0.1:8000/auth/callback`.

Start the API:

```powershell
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/dashboard` or `http://127.0.0.1:8000/docs`.

Connect Upstox from `http://127.0.0.1:8000/auth/login`. Approve the login in the browser. The token is stored in `data/upstox_token.json` and expires at 3:30 AM the next day. Check `http://127.0.0.1:8000/auth/status` before a run.

Run the tests:

```powershell
pytest
```

Check whether Upstox currently returns October 2026 data:

```powershell
python -m app.probe_october
```

The probe prints counts and error text. It does not print the access token.

## Strategy rules

`POST /api/backtests/strategy` and `POST /api/backtests/run` reject the request when a required rule is missing. You must supply:

- index, date range, entry time, and exit time
- `entry_signal`: only `scheduled_time` is implemented
- `CE` or `PE`, strike offset in listed steps, and expiry rule (`weekly`, `monthly`, or `specific`)
- target percent, stop-loss percent, lots, max trades per day, max daily loss, max open positions, and starting capital
- brokerage per order, STT sell percent, exchange transaction percent, SEBI charge per crore, stamp buy percent, GST percent, and slippage percent

Enter those charge rates from your own cost sheet. The engine does not insert brokerage or tax defaults.

On a session, the engine reads the index open at the entry minute, chooses the expiry, infers the strike step from the listed strikes, and loads that option's one-minute candles through the exit minute. The entry fill is the option open. Later candles can hit the target or the stop. At the exit minute, an open trade leaves at that candle's open.

## Endpoints

- `GET /health`
- `GET /dashboard`
- `GET /auth/login`
- `GET /auth/callback`
- `GET /auth/status`
- `GET /api/instruments?symbol=NIFTY&expiry=2026-10-06`
- `GET /api/historical-candles`
- `POST /api/backtests/strategy`
- `GET /api/backtests/strategy`
- `POST /api/backtests/run`
- `GET /api/backtests/{backtest_id}`
- `GET /api/backtests/{backtest_id}/trades`
- `GET /api/backtests/{backtest_id}/report?format=json|csv|xlsx`
- `POST /api/backtests/atm-preview`
- `POST /api/candles/import-csv`
- `GET /api/data/status`

`GET /api/historical-candles` uses the V3 history API. Pass `expired=true` to use `/v2/expired-instruments/historical-candle` for an expired option key. One-minute expired history is documented by Upstox as the last month of candles up to the end date, so early October 2026 can be unavailable on a later request date. The response error is stored and shown. No candles are invented.

## Storage

- `data/upstox_token.json` — access token. Do not commit or share it.
- `data/instruments/` — expiry and contract caches
- `data/index/` — index candles
- `data/options/` — option candles
- `data/reports/` — exported reports
- `data/backtests.db` — saved strategies, runs, trades, and download status

`.env`, tokens, and `data/*` are gitignored.

## Limits

- This is research software. A completed run is not a forecast and not permission to trade.
- Only `scheduled_time` entries are implemented. There is no implied indicator, price pattern, or re-entry rule.
- Option premium is never approximated from the index move.
- A live check on 8 Nov 2026 downloaded NIFTY and BANKNIFTY one-minute index candles for October 2026. Weekday sessions returned 375 candles. 2 Oct 2026 and 20 Oct 2026 returned no candles. Expired option expiries, contracts, and option premiums returned `UDAPI1149` because that API requires an Upstox Plus subscription. No option backtest result was produced from those premiums.
- Same-candle target and stop collisions use the stop fill because the tick path is unknown.
- Restart uvicorn after pulling these changes so the process loads the new routes.
