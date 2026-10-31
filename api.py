from datetime import datetime, timedelta
import json
import math
import requests
from pathlib import Path
from collections import deque
import logging
import threading
import time
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from angel_client import AngelClient, Credentials
from scanner import build_timeframes, analyze_multi_timeframe, historical_replay

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nifty-smc-api")
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
app = FastAPI(title="NIFTY SMC Local API", version="0.7.0")
app.add_middleware(CORSMiddleware, allow_origins=["null", "http://127.0.0.1", "http://localhost",
    "http://127.0.0.1:5500", "http://localhost:5500", "https://javaravish.github.io"], allow_credentials=False,
    allow_methods=["GET", "POST"], allow_headers=["*"])
_angel_client = None
_live_lock = threading.RLock()
_live = {
    "price": None, "timestamp": None, "sequence": None, "forming": None,
    "history_loaded": False, "history_error": None
}
_live_candles = deque(maxlen=1500)
_market_lock = threading.RLock()
_active_market = "NIFTY"
MARKETS = {
    "NIFTY": {"label": "NIFTY 50", "exchange": "NSE", "token_env": "ANGEL_NIFTY_TOKEN", "default_token": "99926000", "exchange_type": 1, "option_exchange": "NFO", "option_name": "NIFTY", "max_stop_points": 60},
    "BANKNIFTY": {"label": "BANKNIFTY", "exchange": "NSE", "token_env": "ANGEL_BANKNIFTY_TOKEN", "default_token": "99926009", "exchange_type": 1, "option_exchange": "NFO", "option_name": "BANKNIFTY", "max_stop_points": 150},
    "SENSEX": {"label": "SENSEX", "exchange": "BSE", "token_env": "ANGEL_SENSEX_TOKEN", "default_token": "99919000", "exchange_type": 3, "option_exchange": "BFO", "option_name": "SENSEX", "max_stop_points": 150},
}

def market_config(symbol=None):
    key = str(symbol or _active_market).upper()
    if key not in MARKETS:
        raise HTTPException(status_code=400, detail="Unsupported market. Choose NIFTY, BANKNIFTY or SENSEX.")
    cfg = dict(MARKETS[key])
    try:
        env = read_env()
    except Exception:
        env = {}
    cfg.update({"symbol": key, "token": env.get(cfg["token_env"]) or cfg["default_token"]})
    return cfg


# Auto trading is deliberately disarmed by default. Enable it explicitly from the
# dashboard only after verifying account, instrument master, quantity and risk.
TRADING_STATE_FILE = BASE_DIR / "trading_state.json"
_INSTRUMENT_MASTER_URL = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
_trade_lock = threading.RLock()
_trade = {
    "enabled": False, "mode": "DISABLED", "lots": 1, "max_daily_trades": 5,
    "position": None, "pending": None, "first_trade_result": None,
    "session_date": None, "last_signal_key": None, "last_action": "Not armed",
    "last_order_id": None, "last_error": None, "last_order_check": 0.0,
    "instrument_master": None, "instrument_master_loaded_at": None
}

def _load_trade_day_state():
    try:
        data = json.loads(TRADING_STATE_FILE.read_text(encoding="utf-8"))
        if data.get("session_date") == datetime.now().date().isoformat():
            _trade["session_date"] = data.get("session_date")
        _trade.update({k: data.get(k) for k in ("first_trade_result", "position", "pending",
                "last_signal_key", "last_order_id")})
    except Exception:
        pass

def _save_trade_day_state():
    safe = {k: _trade.get(k) for k in ("session_date", "first_trade_result", "position",
        "pending", "last_signal_key", "last_order_id")}
    try:
        TRADING_STATE_FILE.write_text(json.dumps(safe, default=str, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("Could not persist daily trading state")

def _roll_trade_day():
    today = datetime.now().date().isoformat()
    if _trade.get("session_date") != today:
        # Reset the daily lock, but never discard an open/pending broker order
        # merely because the local calendar date changed.
        _trade.update({"session_date": today, "first_trade_result": None,
                       "last_signal_key": None})
        _save_trade_day_state()

def _instrument_master():
    cached = _trade.get("instrument_master")
    loaded = _trade.get("instrument_master_loaded_at")
    if cached is not None and loaded and (datetime.now() - loaded).total_seconds() < 6 * 3600:
        return cached
    response = requests.get(_INSTRUMENT_MASTER_URL, timeout=20)
    response.raise_for_status()
    rows = response.json()
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("Angel One instrument master response was empty or invalid.")
    _trade["instrument_master"] = rows
    _trade["instrument_master_loaded_at"] = datetime.now()
    return rows

def _select_weekly_atm_option(spot_price, direction, market_symbol=None):
    """Select nearest listed ATM option from Angel One's current instrument master."""
    cfg = market_config(market_symbol)
    candidates = []
    today = datetime.now().date()
    for row in _instrument_master():
        if row.get("exch_seg") != cfg["option_exchange"] or row.get("instrumenttype") != "OPTIDX":
            continue
        if str(row.get("name", "")).upper() != cfg["option_name"]:
            continue
        symbol = str(row.get("symbol", "")).upper()
        if not symbol.endswith("CE" if direction == "CE" else "PE"):
            continue
        expiry_text = str(row.get("expiry", ""))
        expiry = None
        for fmt in ("%d%b%Y", "%d-%b-%Y", "%d%b%y", "%Y-%m-%d"):
            try:
                expiry = datetime.strptime(expiry_text, fmt).date()
                break
            except ValueError:
                pass
        if expiry is None or expiry < today:
            continue
        try:
            strike, lot_size, token = float(row.get("strike")) / 100, int(row.get("lotsize")), str(row.get("token"))
        except (TypeError, ValueError):
            continue
        if token and lot_size > 0:
            candidates.append((expiry, abs(strike - float(spot_price)), strike, lot_size, token, symbol))
    if not candidates:
        raise RuntimeError(f"No current/future {cfg['option_name']} {cfg['option_exchange']} {direction} option found in Angel One's instrument master.")
    expiry = min(x[0] for x in candidates)
    chosen = min((x for x in candidates if x[0] == expiry), key=lambda x: (x[1], x[2]))
    return {"expiry": expiry.isoformat(), "strike": chosen[2], "lot_size": chosen[3],
            "symbol_token": chosen[4], "tradingsymbol": chosen[5], "option_type": direction,
            "exchange": cfg["option_exchange"], "underlying": cfg["symbol"]}

def _submit_market_order(contract, transaction_type, quantity):
    c = client()
    params = {
        "variety": "NORMAL", "tradingsymbol": contract["tradingsymbol"],
        "symboltoken": str(contract["symbol_token"]), "transactiontype": transaction_type,
        "exchange": contract.get("exchange", "NFO"), "ordertype": "MARKET", "producttype": "INTRADAY",
        "duration": "DAY", "price": "0", "squareoff": "0", "stoploss": "0",
        "quantity": str(int(quantity))
    }
    response = c.api.placeOrder(params)
    if isinstance(response, str):
        order_id = response
    else:
        order_id = (response or {}).get("data", {}).get("orderid") or (response or {}).get("data", {}).get("orderId")
    if not order_id:
        raise RuntimeError("Angel One did not return an order ID: " + str(response)[:250])
    return str(order_id)

def _order_status(order_id):
    c = client()
    response = c.api.orderBook()
    rows = (response or {}).get("data") or []
    for row in rows:
        if str(row.get("orderid") or row.get("orderId")) == str(order_id):
            return str(row.get("status", row.get("orderstatus", "UNKNOWN"))).lower(), row
    return "unknown", None

def _finish_trade(result, spot_exit):
    position = _trade.get("position") or {}
    entry_time = position.get("entry_time")
    entry_day = str(entry_time)[:10] if entry_time else None
    if entry_day == datetime.now().date().isoformat() and _trade.get("first_trade_result") is None:
        if result == "TARGET":
            first_result = "PROFIT"
        elif result == "STOP":
            first_result = "LOSS"
        else:
            direction = position.get("direction")
            entry_spot = float(position.get("entry_spot", spot_exit))
            spot_points = (spot_exit - entry_spot) if direction == "CE" else (entry_spot - spot_exit)
            first_result = "PROFIT" if spot_points > 0 else "LOSS"
        _trade["first_trade_result"] = first_result
    _trade["last_action"] = f"Position closed: {result} at {position.get('market', 'selected index')} {spot_exit:.2f}"
    _trade["position"] = None
    _trade["pending"] = None
    _save_trade_day_state()

def _trading_on_tick(spot_price, stamp, analysis, frame):
    """Manage spot-defined exits first; only then consider a new ATM weekly option entry."""
    with _trade_lock:
        _roll_trade_day()
        position = _trade.get("position")
        pending = _trade.get("pending")

        # Reconcile any submitted order before taking another action. This also
        # handles manually requested exits and avoids duplicate SELL submissions.
        if pending:
            if time.time() - float(_trade.get("last_order_check") or 0) < 5:
                return
            _trade["last_order_check"] = time.time()
            try:
                status, row = _order_status(pending["order_id"])
                if status in ("complete", "completed"):
                    if pending["kind"] == "ENTRY":
                        position = pending["position"]
                        fill_price = None
                        if row:
                            for price_key in ("averageprice", "avgprice", "average_price", "avg_price"):
                                try:
                                    candidate = float(row.get(price_key) or 0)
                                    if candidate > 0:
                                        fill_price = candidate
                                        break
                                except (TypeError, ValueError):
                                    pass
                        position["entry_option_price"] = fill_price
                        position["entry_order_id"] = pending.get("order_id")
                        _trade["position"] = position
                        _trade["last_action"] = "Entry order filled; position is active"
                    else:
                        _finish_trade(pending["reason"], pending["spot_exit"])
                    _trade["pending"] = None
                    _save_trade_day_state()
                elif status in ("rejected", "cancelled", "canceled"):
                    _trade["last_error"] = f"Order {pending['order_id']} {status}: {row}"
                    _trade["pending"] = None
                    _trade["last_action"] = f"Order {status}"
                    _save_trade_day_state()
                return
            except Exception as exc:
                _trade["last_error"] = f"Order status check failed: {str(exc)[:220]}"
                return

        # Protect an open position even when the user has disarmed new entries
        # or the current SMC analysis has changed to WAIT.
        position = _trade.get("position")
        if position:
            direction = position["direction"]
            hit_sl = spot_price <= position["stop_loss"] if direction == "CE" else spot_price >= position["stop_loss"]
            hit_tp = spot_price >= position["target"] if direction == "CE" else spot_price <= position["target"]
            end_of_day = (stamp.hour, stamp.minute) >= (15, 25)
            if hit_sl or hit_tp or end_of_day:
                exit_reason = "EOD" if end_of_day else ("STOP" if hit_sl else "TARGET")  # conservative if both levels crossed
                try:
                    order_id = _submit_market_order(position["contract"], "SELL", position["quantity"])
                    _trade["pending"] = {"kind": "EXIT", "order_id": order_id,
                        "reason": exit_reason, "spot_exit": float(spot_price)}
                    _trade["last_order_id"] = order_id
                    _trade["last_action"] = f"{exit_reason} exit order submitted for {position.get('market', _active_market)}"
                    _save_trade_day_state()
                except Exception as exc:
                    _trade["last_error"] = str(exc)[:300]
                    logger.exception("Automatic option exit failed")
            return

        # No new entries unless explicitly armed for live order placement.
        if not _trade.get("enabled") or _trade.get("mode") != "LIVE":
            return
        # Do not enter outside the regular NSE cash session.
        if not (stamp.hour > 9 or (stamp.hour == 9 and stamp.minute >= 15)) or (stamp.hour > 15 or (stamp.hour == 15 and stamp.minute > 25)):
            return
        if analysis and analysis.get("signal") == "WAIT":
            _trade["last_signal_key"] = None
            _save_trade_day_state()
            return
        if not analysis or analysis.get("signal") not in ("CE", "PE") or frame is None or len(frame) < 12:
            return
        if _trade.get("first_trade_result") == "PROFIT":
            _trade["last_action"] = "Daily entry blocked: first trade was profitable"
            return

        direction = analysis["signal"]
        entry_spot = float(spot_price)
        # The SMC scanner owns these levels. Never replace them with a fixed R multiple.
        stop = analysis.get("stop_loss")
        target = analysis.get("target")
        if stop is None or target is None:
            _trade["last_action"] = "Signal skipped: SMC did not produce both a structural stop and liquidity target"
            return
        stop, target = float(stop), float(target)
        risk = entry_spot - stop if direction == "CE" else stop - entry_spot
        reward = target - entry_spot if direction == "CE" else entry_spot - target
        cfg = market_config()
        max_stop = float(cfg["max_stop_points"])
        if risk <= 0 or risk > max_stop or reward <= risk:
            _trade["last_action"] = f"Signal skipped: {cfg['symbol']} SMC levels invalid (risk {risk:.2f}, reward {reward:.2f}); require 0<risk<={max_stop:.0f} and reward>risk"
            return
        # One entry per continuous signal; require a WAIT/neutral reset before
        # the same direction can trigger again after a losing trade.
        signal_key = direction
        if _trade.get("last_signal_key") == signal_key:
            return
        try:
            contract = _select_weekly_atm_option(entry_spot, direction, _active_market)
            quantity = int(contract["lot_size"]) * int(_trade.get("lots", 1))
            order_id = _submit_market_order(contract, "BUY", quantity)
            position = {"market": _active_market, "direction": direction, "contract": contract, "quantity": quantity,
                "entry_spot": entry_spot, "stop_loss": stop, "target": target,
                "risk_points": risk, "reward_points": reward,
                "stop_reason": analysis.get("stop_reason"), "target_reason": analysis.get("target_reason"),
                "entry_time": stamp.isoformat()}
            _trade.update({"pending": {"kind": "ENTRY", "order_id": order_id, "position": position},
                "last_order_id": order_id, "last_signal_key": signal_key,
                "last_action": f"BUY {contract['tradingsymbol']} submitted on {contract['exchange']}"})
            _save_trade_day_state()
        except Exception as exc:
            _trade["last_error"] = str(exc)[:300]
            logger.exception("Automatic option entry failed")

_load_trade_day_state()

def read_env():
    if not ENV_FILE.exists():
        raise RuntimeError(".env file missing from project folder.")
    vals = {}
    for raw in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals

def credentials():
    v = read_env()
    required = ["ANGEL_CLIENT_CODE", "ANGEL_API_KEY", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
    missing = [k for k in required if not v.get(k)]
    if missing:
        raise RuntimeError("Missing .env setting(s): " + ", ".join(missing))
    return Credentials(v["ANGEL_CLIENT_CODE"], v["ANGEL_API_KEY"], v["ANGEL_PIN"], v["ANGEL_TOTP_SECRET"]), v

def client():
    if not _angel_client or not _angel_client.jwt:
        raise HTTPException(status_code=401, detail="Connect Angel One first using POST /api/connect.")
    return _angel_client

def fetch_one_minute(days):
    c = client()
    _, v = credentials()
    end = datetime.now()
    start = end - timedelta(days=days)
    cfg = market_config()
    frame = c.historical(cfg["exchange"], cfg["token"], "ONE_MINUTE", start, end)
    if frame.empty:
        raise RuntimeError("Angel One returned no one-minute candles for the requested period.")
    return frame, v

def _tick_time(message):
    raw = message.get("exchange_timestamp") or message.get("exchangeTimestamp") or message.get("last_traded_timestamp")
    if raw is None:
        return datetime.now()
    try:
        n = int(raw)
        return datetime.fromtimestamp(n / 1000) if n > 10_000_000_000 else datetime.fromtimestamp(n)
    except Exception:
        return pd.to_datetime(raw).to_pydatetime()

def _seed_live_candles(frame):
    """Preload completed historical 1-minute candles before live ticks arrive."""
    if frame is None or frame.empty:
        raise RuntimeError("Historical preload returned no candles.")
    now_minute = datetime.now().replace(second=0, microsecond=0)
    prepared = frame.copy()
    # SmartAPI may return timezone-aware IST timestamps, while local tick times
    # are naive local datetimes. Normalize historical timestamps to naive IST
    # so comparisons and candle aggregation use one consistent timezone.
    parsed = pd.to_datetime(prepared["datetime"])
    if getattr(parsed.dt, "tz", None) is not None:
        parsed = parsed.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
    prepared["datetime"] = parsed
    # Do not preload the currently forming minute as a completed candle.
    prepared = prepared[prepared["datetime"] < now_minute]
    prepared = prepared.sort_values("datetime").drop_duplicates("datetime")
    rows = prepared.tail(1400).to_dict("records")
    with _live_lock:
        _live_candles.clear()
        _live_candles.extend(rows)
        _live["forming"] = None
        _live["history_loaded"] = bool(rows)
        _live["history_error"] = None if rows else "No completed historical candles were available."
    return len(rows)


def _on_market_tick(message):
    price_raw = message.get("last_traded_price", message.get("ltp"))
    if price_raw is None:
        return
    # SmartWebSocketV2's parsed LTP packet is in rupees in current SDK releases.
    price = float(price_raw)
    if price > 100000:  # defensive for SDK versions returning paise
        price /= 100
    stamp = _tick_time(message)
    minute = stamp.replace(second=0, microsecond=0)
    with _live_lock:
        forming = _live.get("forming")
        if forming is None or forming["datetime"] != minute:
            if forming is not None and forming["datetime"] < minute:
                _live_candles.append(forming.copy())
            # Ignore out-of-order ticks from an older minute for candle construction,
            # while still retaining the latest received quote timestamp separately.
            if forming is not None and minute < forming["datetime"]:
                _live.update({"price": price, "timestamp": stamp.isoformat(),
                              "sequence": message.get("sequence_number")})
                return
            forming = {"datetime": minute, "open": price, "high": price, "low": price, "close": price, "volume": 0}
            _live["forming"] = forming
        else:
            forming["high"] = max(forming["high"], price)
            forming["low"] = min(forming["low"], price)
            forming["close"] = price
        _live.update({"price": price, "timestamp": stamp.isoformat(), "sequence": message.get("sequence_number")})

@app.get("/api/status")
def status():
    cfg = market_config()
    return {"ok": True, "service": "nifty-smc-local-api", "mode": "local-smartapi-scanner",
            "market": cfg["symbol"], "market_label": cfg["label"],
            "supported_markets": [{"symbol": k, "label": v["label"]} for k, v in MARKETS.items()],
            "angel_one_connected": bool(_angel_client and _angel_client.jwt),
            "websocket_connected": bool(_angel_client and _angel_client.ws_connected),
            "websocket_error": _angel_client.ws_error if _angel_client else None,
            "historical_candles_loaded": bool(_live.get("history_loaded")),
            "historical_candle_count": len(_live_candles),
            "historical_load_error": _live.get("history_error"),
            "trading": _trading_public_state(), "timestamp": datetime.now().astimezone().isoformat()}


def _trading_public_state():
    with _trade_lock:
        _roll_trade_day()
        return {k: _trade.get(k) for k in ("enabled", "mode", "lots", "max_daily_trades",
            "position", "pending", "first_trade_result", "last_action", "last_order_id", "last_error")}

@app.get("/api/trading/status")
def trading_status():
    state = _trading_public_state()
    position = state.get("position")
    quote = None
    pnl = None
    pnl_error = None
    if position:
        contract = position.get("contract") or {}
        try:
            quote = client().ltp(contract.get("exchange", "NFO"), contract["tradingsymbol"], contract["symbol_token"])
            entry_premium = position.get("entry_option_price")
            if entry_premium is not None:
                pnl = round((float(quote["price"]) - float(entry_premium)) * int(position["quantity"]), 2)
        except Exception as exc:
            pnl_error = str(exc)[:220]
    return {"ok": True, "market": _active_market, "market_label": market_config()["label"],
            **state, "position_ltp": quote["price"] if quote else None,
            "position_ltp_time": quote.get("raw_time") if quote else None,
            "position_pnl": pnl, "position_pnl_basis": "option premium x filled quantity, before brokerage/taxes",
            "position_pnl_error": pnl_error,
            "warning": "Live mode sends index-option market orders: NFO for NIFTY/BANKNIFTY and BFO for SENSEX. Stop-loss and target are monitored on the selected index spot, not option premium."}

@app.post("/api/trading/arm")
def trading_arm(enabled: bool = Query(...), lots: int = Query(1, ge=1, le=20),
                confirm_live: bool = Query(False)):
    with _trade_lock:
        _roll_trade_day()
        if enabled and not confirm_live:
            raise HTTPException(status_code=400, detail="Set confirm_live=true to explicitly arm real-money order placement.")
        if enabled:
            c = client()
            if not c.ws_connected or _live.get("price") is None:
                raise HTTPException(status_code=400, detail=f"Connect Angel One and confirm the live {market_config()['label']} feed before arming.")
            if _trade.get("position") or _trade.get("pending"):
                raise HTTPException(status_code=409, detail="An option position or order is already open/pending. Reconcile or close it before arming another trade.")
            _trade.update({"enabled": True, "mode": "LIVE", "lots": lots, "last_error": None,
                           "last_action": f"LIVE AUTO TRADING ARMED FOR {_active_market}"})
        else:
            _trade.update({"enabled": False, "mode": "DISABLED", "last_action": "Auto trading disarmed"})
        return {"ok": True, **_trading_public_state()}

@app.post("/api/trading/exit")
def trading_exit():
    with _trade_lock:
        position = _trade.get("position")
        if not position:
            raise HTTPException(status_code=400, detail="No confirmed open option position to exit.")
        try:
            order_id = _submit_market_order(position["contract"], "SELL", position["quantity"])
            _trade["pending"] = {"kind": "EXIT", "order_id": order_id,
                "reason": "MANUAL", "spot_exit": float(_live.get("price") or position["entry_spot"])}
            _trade["last_order_id"] = order_id
            _trade["last_action"] = "Manual exit order submitted"
            _save_trade_day_state()
            return {"ok": True, **_trading_public_state()}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)[:300])

@app.post("/api/market")
def select_market(symbol: str = Query(..., pattern="^(NIFTY|BANKNIFTY|SENSEX)$")):
    """Switch the active index feed. Trading must be disarmed and flat first."""
    global _active_market
    symbol = symbol.upper()
    with _market_lock, _trade_lock:
        if symbol == _active_market:
            return {"ok": True, "market": symbol, "market_label": market_config(symbol)["label"], "changed": False}
        if _trade.get("enabled") or _trade.get("position") or _trade.get("pending"):
            raise HTTPException(status_code=409, detail="Disarm automatic trading and close/reconcile any open or pending position before switching indices.")
        cfg = market_config(symbol)
        if _angel_client and _angel_client.jwt:
            _angel_client.close_market_websocket()
            if _angel_client.ws_thread and _angel_client.ws_thread.is_alive():
                raise HTTPException(status_code=503, detail="The previous market WebSocket is still shutting down. Wait a moment and select the index again.")
        _active_market = symbol
        with _live_lock:
            _live.update({"price": None, "timestamp": None, "sequence": None, "forming": None,
                          "history_loaded": False, "history_error": None})
            _live_candles.clear()
        if _angel_client and _angel_client.jwt:
            preload_count = 0
            preload_error = None
            try:
                end = datetime.now()
                historical = _angel_client.historical(cfg["exchange"], cfg["token"], "ONE_MINUTE", end - timedelta(days=5), end)
                preload_count = _seed_live_candles(historical)
            except Exception as exc:
                preload_error = str(exc)[:250]
                with _live_lock:
                    _live["history_error"] = preload_error
                logger.exception("Market history preload failed for %s", symbol)
            ws_result = _angel_client.start_market_websocket(cfg["token"], _on_market_tick, exchange_type=cfg["exchange_type"])
        else:
            preload_count, preload_error, ws_result = 0, "Login to Angel One to start the selected market feed.", {"started": False}
        return {"ok": True, "market": symbol, "market_label": cfg["label"], "changed": True,
                "historical_candles_loaded": preload_count, "historical_load_error": preload_error,
                "websocket_started": ws_result["started"],
                "message": f"Selected {cfg['label']}. Feed is being refreshed for this index."}


@app.post("/api/connect")
def connect():
    global _angel_client
    try:
        creds, v = credentials()
        c = AngelClient(creds)
        c.login()
        _angel_client = c

        cfg = market_config()
        # Seed the live analysis buffer with recent completed 1-minute candles.
        # If preload fails, keep the login/WebSocket usable and report the issue.
        preload_count = 0
        preload_error = None
        try:
            end = datetime.now()
            start = end - timedelta(days=5)
            historical = c.historical(
                cfg["exchange"], cfg["token"], "ONE_MINUTE", start, end
            )
            preload_count = _seed_live_candles(historical)
        except Exception as preload_exc:
            preload_error = str(preload_exc)[:250]
            with _live_lock:
                _live["history_loaded"] = False
                _live["history_error"] = preload_error
            logger.exception("Historical live-buffer preload failed")

        ws_result = c.start_market_websocket(
            cfg["token"], _on_market_tick, exchange_type=cfg["exchange_type"]
        )
        return {
            "ok": True, "angel_one_connected": True, "market": cfg["symbol"], "market_label": cfg["label"],
            "websocket_started": ws_result["started"],
            "historical_candles_loaded": preload_count,
            "historical_load_error": preload_error,
            "message": (
                "Angel One login successful. Historical candles were loaded into the live-analysis buffer; "
                "WebSocket updates will continue from incoming ticks. No order was placed."
                if preload_count else
                "Angel One login and WebSocket startup initiated, but historical preload did not complete. "
                "Check historical_load_error. No order was placed."
            )
        }
    except Exception as e:
        logger.exception("Angel One login/WebSocket start failed")
        if _angel_client:
            _angel_client.close_market_websocket()
        _angel_client = None
        raise HTTPException(status_code=400, detail=str(e)[:300])


@app.get("/api/candles")
def candles(
    interval: str = Query("1m", pattern="^(1m|2m|3m|5m|15m)$"),
    days: int = Query(1, ge=1, le=30),
):
    """Return real Angel One NIFTY OHLCV candles for the dashboard chart."""
    try:
        c = client()
        cfg = market_config()
        end = datetime.now()
        start = end - timedelta(days=days)
        # SmartAPI native intervals; 2-minute bars are resampled from 1-minute data.
        native = {"1m": "ONE_MINUTE", "3m": "THREE_MINUTE",
                  "5m": "FIVE_MINUTE", "15m": "FIFTEEN_MINUTE"}
        source_interval = "1m" if interval == "2m" else interval
        frame = c.historical(
            cfg["exchange"], cfg["token"], native[source_interval], start, end
        )
        if frame is None or frame.empty:
            return {"ok": True, "interval": interval, "candles": [], "count": 0,
                    "message": "Angel One returned no candles for this period."}
        data = frame.copy()
        parsed = pd.to_datetime(data["datetime"])
        if getattr(parsed.dt, "tz", None) is not None:
            parsed = parsed.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        data["datetime"] = parsed
        data = data.sort_values("datetime").drop_duplicates("datetime")
        if interval == "2m":
            data = data.set_index("datetime").resample(
                "2min", origin="start_day", label="left", closed="left"
            ).agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum"
            }).dropna(subset=["open", "high", "low", "close"]).reset_index()
        # Dashboard receives a stable, simple list of timestamped OHLCV objects.
        output = []
        for row in data.itertuples(index=False):
            output.append({
                "datetime": pd.Timestamp(row.datetime).isoformat(),
                "open": float(row.open), "high": float(row.high),
                "low": float(row.low), "close": float(row.close),
                "volume": float(getattr(row, "volume", 0) or 0),
            })
        return {"ok": True, "market": cfg["label"], "market_symbol": cfg["symbol"], "interval": interval,
                "count": len(output), "candles": output,
                "source": "Angel One SmartAPI historical OHLCV",
                "note": "These are exchange historical candles, not synthetic demo bars."}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Candle fetch failed")
        raise HTTPException(status_code=400, detail=str(e)[:300])

@app.get("/api/scan")
def scan(days: int = Query(5, ge=1, le=30)):
    try:
        one, _ = fetch_one_minute(days)
        frames = build_timeframes(one)
        a = analyze_multi_timeframe(frames)
        cfg = market_config()
        return {"ok": True, "market": cfg["label"], "market_symbol": cfg["symbol"], "candle_counts": {k: len(x) for k, x in frames.items()},
                "analysis": a, "note": "Historical candle analysis only. No orders placed."}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Scan failed")
        raise HTTPException(status_code=400, detail=str(e)[:300])

@app.get("/api/history")
def history(days: int = Query(15, ge=1, le=250), strict: bool = True, symbol: str | None = Query(None, pattern="^(NIFTY|BANKNIFTY|SENSEX)$")):
    try:
        # `days` means actual trading sessions. Request a wider calendar window
        # to cover weekends/holidays, then keep exactly the newest available sessions.
        calendar_days = min(550, int(math.ceil(days * 7 / 5)) + 14)
        c = client()
        cfg = market_config(symbol)
        end = datetime.now()
        start = end - timedelta(days=calendar_days)
        chunks = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(days=25), end)
            part = c.historical(cfg["exchange"], cfg["token"], "ONE_MINUTE", cursor, chunk_end)
            if part is not None and not part.empty:
                chunks.append(part)
            cursor = chunk_end
        if not chunks:
            raise RuntimeError("Angel One returned no one-minute candles for the requested trading-session history.")
        one = pd.concat(chunks, ignore_index=True).drop_duplicates("datetime").sort_values("datetime").reset_index(drop=True)
        frames = build_timeframes(one)
        result = historical_replay(frames)
        dates = sorted({str(pd.to_datetime(x).date()) for x in one.datetime})
        selected = set(dates[-days:])
        result["signals"] = [s for s in result["signals"] if str(pd.to_datetime(s["time"]).date()) in selected]
        result["session_dates"] = sorted(selected)
        result["candle_counts"] = {k: len(x) for k, x in frames.items()}
        result["summary"]["sessions_included"] = len(selected)
        result["summary"]["requested_sessions"] = days
        result["summary"]["strict_four_timeframe"] = bool(strict)
        return {"ok": True, **result,
                "market": cfg["label"], "market_symbol": cfg["symbol"],
                "message": f"Replay uses the latest {len(selected)} available trading sessions for {cfg['label']}. Signals are index spot setups, not option fills."}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Historical replay failed")
        raise HTTPException(status_code=400, detail=str(e)[:300])

@app.get("/api/live")
def live():
    try:
        c = client()
        cfg = market_config()
        with _live_lock:
            snapshot = dict(_live)
            # Use completed candles only for signal generation and order levels.
            candles = list(_live_candles)
        if snapshot["price"] is None:
            return {
                "ok": True, "market": cfg["label"], "market_symbol": cfg["symbol"], "connected": bool(c.ws_connected), "price": None,
                "signal": "WAIT", "timeframes": {}, "analysis_ready": False,
                "entry": None, "stop_loss": None, "target": None,
                "risk_points": None, "reward_points": None,
                "stop_reason": None, "target_reason": None,
                "historical_candles_loaded": bool(snapshot.get("history_loaded")),
                "candle_count_1m": len(candles),
                "message": "Waiting for the first WebSocket tick before live SMC analysis.",
                "websocket_error": c.ws_error
            }
        frame = pd.DataFrame(candles)
        analysis = None
        frames = {}
        if len(frame) >= 15:
            frame["datetime"] = pd.to_datetime(frame["datetime"])
            frame = frame.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
            frames = build_timeframes(frame)
            analysis = analyze_multi_timeframe(frames)
        if snapshot.get("timestamp"):
            try:
                quote_stamp = datetime.fromisoformat(snapshot["timestamp"])
                quote_age = max(0, (datetime.now() - quote_stamp).total_seconds())
                if quote_age <= 15:
                    _trading_on_tick(float(snapshot["price"]), quote_stamp, analysis, frame)
            except Exception:
                logger.exception("Trading engine tick evaluation failed")
        tf = (analysis or {}).get("timeframes", {})
        concepts = [f"{k}: {x.get('structure')} / {x.get('signal')}" for k, x in tf.items()]
        fvg = [f for x in tf.values() for f in x.get("findings", []) if "FVG" in f]
        obs = [f for x in tf.values() for f in x.get("findings", []) if "OB" in f]
        age = max(0, (datetime.now() - datetime.fromisoformat(snapshot["timestamp"])).total_seconds())
        return {"ok": True, "market": cfg["label"], "market_symbol": cfg["symbol"], "connected": c.ws_connected, "price": snapshot["price"],
                "quote_time": snapshot["timestamp"], "tick_age_seconds": round(age, 2),
                "stale": age > 15, "signal": (analysis or {}).get("signal", "WAIT"),
                "fvg": fvg, "order_block": obs, "concepts": concepts, "timeframes": tf,
                "entry": (analysis or {}).get("entry"),
                "stop_loss": (analysis or {}).get("stop_loss"),
                "target": (analysis or {}).get("target"),
                "risk_points": (analysis or {}).get("risk_points"),
                "reward_points": (analysis or {}).get("reward_points"),
                "stop_reason": (analysis or {}).get("stop_reason"),
                "target_reason": (analysis or {}).get("target_reason"),
                "historical_candles_loaded": bool(snapshot.get("history_loaded")),
                "historical_load_error": snapshot.get("history_error"),
                "candle_count_1m": len(frame),
                "candle_counts": {k: len(v) for k, v in frames.items()},
                "analysis_ready": bool(tf),
                "note": (
                    "Live analysis combines preloaded completed historical 1-minute candles with newly completed "
                    "tick-built candles. The current forming minute is included for context. Signals remain heuristic; "
                    "no orders are placed."
                ),
                "websocket_error": c.ws_error}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Live feed read failed")
        raise HTTPException(status_code=400, detail=str(e)[:300])
