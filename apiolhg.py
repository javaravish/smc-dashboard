from datetime import datetime, timedelta
from pathlib import Path
from collections import deque
import logging
import threading
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from angel_client import AngelClient, Credentials
from scanner import build_timeframes, analyze_multi_timeframe, historical_replay

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nifty-smc-api")
BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
app = FastAPI(title="NIFTY SMC Local API", version="0.6.0")
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
    required = ["ANGEL_CLIENT_CODE", "ANGEL_API_KEY", "ANGEL_PIN", "ANGEL_TOTP_SECRET", "ANGEL_NIFTY_TOKEN"]
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
    frame = c.historical("NSE", v["ANGEL_NIFTY_TOKEN"], "ONE_MINUTE", start, end)
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
    return {"ok": True, "service": "nifty-smc-local-api", "mode": "local-smartapi-scanner",
            "angel_one_connected": bool(_angel_client and _angel_client.jwt),
            "websocket_connected": bool(_angel_client and _angel_client.ws_connected),
            "websocket_error": _angel_client.ws_error if _angel_client else None,
            "historical_candles_loaded": bool(_live.get("history_loaded")),
            "historical_candle_count": len(_live_candles),
            "historical_load_error": _live.get("history_error"),
            "timestamp": datetime.now().astimezone().isoformat()}

@app.post("/api/connect")
def connect():
    global _angel_client
    try:
        creds, v = credentials()
        c = AngelClient(creds)
        c.login()
        _angel_client = c

        # Seed the live analysis buffer with recent completed 1-minute candles.
        # If preload fails, keep the login/WebSocket usable and report the issue.
        preload_count = 0
        preload_error = None
        try:
            end = datetime.now()
            start = end - timedelta(days=5)
            historical = c.historical(
                "NSE", v["ANGEL_NIFTY_TOKEN"], "ONE_MINUTE", start, end
            )
            preload_count = _seed_live_candles(historical)
        except Exception as preload_exc:
            preload_error = str(preload_exc)[:250]
            with _live_lock:
                _live["history_loaded"] = False
                _live["history_error"] = preload_error
            logger.exception("Historical live-buffer preload failed")

        ws_result = c.start_market_websocket(
            v["ANGEL_NIFTY_TOKEN"], _on_market_tick, exchange_type=1
        )
        return {
            "ok": True, "angel_one_connected": True,
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

@app.get("/api/scan")
def scan(days: int = Query(5, ge=1, le=30)):
    try:
        one, _ = fetch_one_minute(days)
        frames = build_timeframes(one)
        a = analyze_multi_timeframe(frames)
        return {"ok": True, "market": "NIFTY 50", "candle_counts": {k: len(x) for k, x in frames.items()},
                "analysis": a, "note": "Historical candle analysis only. No orders placed."}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Scan failed")
        raise HTTPException(status_code=400, detail=str(e)[:300])

@app.get("/api/history")
def history(days: int = Query(30, ge=15, le=30), strict: bool = True):
    try:
        one, _ = fetch_one_minute(max(days, 30))
        frames = build_timeframes(one)
        result = historical_replay(frames)
        dates = sorted({str(pd.to_datetime(x).date()) for x in one.datetime}) if len(one) else []
        selected = set(dates[-15:])
        result["signals"] = [s for s in result["signals"] if str(pd.to_datetime(s["time"]).date()) in selected]
        result["session_dates"] = sorted(selected)
        result["candle_counts"] = {k: len(x) for k, x in frames.items()}
        result["summary"]["sessions_included"] = len(selected)
        result["summary"]["strict_four_timeframe"] = True
        return {"ok": True, **result,
                "message": "Replay uses completed candles only; signals are NIFTY spot setups, not option fills."}
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Historical replay failed")
        raise HTTPException(status_code=400, detail=str(e)[:300])

@app.get("/api/live")
def live():
    try:
        c = client()
        with _live_lock:
            snapshot = dict(_live)
            candles = list(_live_candles)
            if snapshot.get("forming"):
                candles.append(snapshot["forming"].copy())
        if snapshot["price"] is None:
            return {
                "ok": True, "connected": bool(c.ws_connected), "price": None,
                "signal": "WAIT", "timeframes": {},
                "historical_candles_loaded": bool(snapshot.get("history_loaded")),
                "candle_count_1m": len(candles),
                "message": "Historical candles are preloaded. Waiting for the first WebSocket tick.",
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
        tf = (analysis or {}).get("timeframes", {})
        concepts = [f"{k}: {x.get('structure')} / {x.get('signal')}" for k, x in tf.items()]
        fvg = [f for x in tf.values() for f in x.get("findings", []) if "FVG" in f]
        obs = [f for x in tf.values() for f in x.get("findings", []) if "OB" in f]
        age = max(0, (datetime.now() - datetime.fromisoformat(snapshot["timestamp"])).total_seconds())
        return {"ok": True, "connected": c.ws_connected, "price": snapshot["price"],
                "quote_time": snapshot["timestamp"], "tick_age_seconds": round(age, 2),
                "stale": age > 15, "signal": (analysis or {}).get("signal", "WAIT"),
                "fvg": fvg, "order_block": obs, "concepts": concepts, "timeframes": tf,
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
