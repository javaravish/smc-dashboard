NIFTY / BANKNIFTY / SENSEX multi-index dashboard update

Files to replace in your existing project folder:
  1. api.py
  2. angel_client.py
  3. scanner.py
  4. Replace your dashboard index.html with NIFTY_SMC_dashboard_multi_index.html

Keep your existing .env credentials. Optional explicit index tokens can be added:
  ANGEL_NIFTY_TOKEN=99926000
  ANGEL_BANKNIFTY_TOKEN=99926009
  ANGEL_SENSEX_TOKEN=99919000

The defaults are common Angel One index tokens, but verify each token against the current Angel One instrument master/account before use. Existing NIFTY token in your .env takes precedence.

Restart Uvicorn after replacing the Python files:
  py -3.12 -m uvicorn api:app --host 127.0.0.1 --port 8000

Then refresh the dashboard (Ctrl+F5), log in using Connect Angel One, and choose NIFTY 50, BANKNIFTY, or SENSEX from the dropdown. Switching market resets the selected index's live candle buffer, fetches its recent history, and restarts the WebSocket subscription.

Safety / scope:
- Live quote, historical candles, chart, SMC multi-timeframe analysis, scan, and historical replay are market-selectable.
- Automatic real-money option trading remains NIFTY-only. BANKNIFTY and SENSEX live analysis is available, but the backend blocks arming live orders for those indices until their option contract selection, exchange routing, lot sizing, and risk rules are separately implemented and validated.
- You cannot switch indices while auto trading is armed or an option position/order is open or pending.
- Scanner absolute 60-point risk cap was removed so the same structural SMC logic can analyze the different index price scales. This does not mean the signals are validated or profitable; paper-test each index.

AUTO OPTION TRADING - THREE INDICES
- Auto option trading supports the currently selected NIFTY, BANKNIFTY, or SENSEX market.
- Only one selected index can be armed at a time. Switching is blocked while trading is armed or an order/position is open.
- NIFTY and BANKNIFTY option orders route through NFO; SENSEX option orders route through BFO.
- The backend selects the nearest unexpired ATM option contract from Angel One's current instrument master. BANKNIFTY uses the nearest expiry actually listed; do not assume a weekly contract.
- Structural spot stop ceilings default to 60 points for NIFTY and 150 points for BANKNIFTY/SENSEX. Review these risk limits before live use.
- Live orders are real-money market orders. A network/API outage can delay a spot-triggered exit.
