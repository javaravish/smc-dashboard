# Angel One NIFTY 50 Multi-Timeframe SMC Scanner — Prototype v2

## Configuration
- Multi-timeframe: 15m bias → 5m structure → 2m setup → 1m entry
- SMC candidates: structure breaks, FVG, equal-high/low liquidity pools, displacement, candidate order blocks
- Weekly options context; moderate-risk design; manual execution only

## Local website connection (first test)
This ZIP now includes a small FastAPI service so the website can test connectivity to Python.
It is **connection-test-only**: it does not log in to Angel One or serve real scanner signals yet.

1. Install dependencies: `python -m pip install -r requirements.txt`
2. Start the API in one Command Prompt window: `python -m uvicorn api:app --host 127.0.0.1 --port 8000`
3. Keep that window open while testing.
4. In the dashboard's local Python connection box, use `http://127.0.0.1:8000` and click **Test Python connection**.
5. You can also open `http://127.0.0.1:8000/api/status` in your browser; it should return JSON with `ok: true`.

For now this is deliberately bound to localhost. It is not exposed to your Wi-Fi or the public internet. Do not change the host to `0.0.0.0` or publish this API without adding authentication and proper origin restrictions.

## Setup
1. Install Python 3.10+.
2. Extract this ZIP and open a terminal in this folder.
3. Run `pip install -r requirements.txt`.
4. Copy `.env.example` to `.env`.
5. Enter Angel One client code, API key, PIN and TOTP secret in `.env`, or enter them in the app sidebar.
6. Verify the NIFTY 50 index token using Angel One's current instrument master. The sample token is only a starting point and must be verified.
7. Run `streamlit run app.py`.

## Important implementation notes
- Historical requests use Angel SmartAPI `ONE_MINUTE`, `FIVE_MINUTE`, and `FIFTEEN_MINUTE`.
- Two-minute candles are aggregated from one-minute candles.
- The app requests a configurable calendar window (default 20 days) to capture approximately 15 trading sessions.
- This is a research prototype, not a validated strategy or financial advice.
- SMC rules are deliberately labelled candidates/proxies. BOS/MSS, order blocks, inducement, breaker/mitigation blocks, OTE, premium/discount, session liquidity and kill zones need further explicit rule definitions and validation before being treated as detected concepts.
- Weekly expiry is a configuration context only in this version. It does not yet select a weekly option contract, retrieve option-chain Greeks/IV, calculate option-premium stops, or handle lot sizing.
- This version fetches historical candles. Persistent live WebSocket streaming, reconnect logic, live candle aggregation, alert delivery and automatic refresh are not yet wired into the Streamlit app.
- No order placement is implemented. Keep manual approval and paper trading.
- Never commit `.env`, expose credentials, or paste access tokens/TOTP secrets into chat.
