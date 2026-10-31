from dataclasses import dataclass
import logging
import threading
import pyotp
from SmartApi import SmartConnect
from SmartApi.smartWebSocketV2 import SmartWebSocketV2
import pandas as pd

logger = logging.getLogger("nifty-smc-angel")

@dataclass
class Credentials:
    client_code: str
    api_key: str
    pin: str
    totp_secret: str

class AngelClient:
    def __init__(self, credentials):
        self.credentials = credentials
        self.api = SmartConnect(api_key=credentials.api_key)
        self.jwt = None
        self.feed_token = None
        self.ws = None
        self.ws_thread = None
        self.ws_connected = False
        self.ws_error = None
        self._ws_lock = threading.Lock()

    def login(self):
        otp = pyotp.TOTP(self.credentials.totp_secret).now()
        result = self.api.generateSession(self.credentials.client_code, self.credentials.pin, otp)
        if not result or not result.get("status"):
            raise RuntimeError((result or {}).get("message", "Angel One login failed"))
        data = result.get("data") or {}
        self.jwt = data.get("jwtToken")
        self.feed_token = self.api.getfeedToken()
        if not self.jwt or not self.feed_token:
            raise RuntimeError("Login response did not contain JWT/feed token.")
        return True

    def historical(self, exchange, symbol_token, interval, start, end):
        if not self.jwt:
            raise RuntimeError("Login required.")
        params = {
            "exchange": exchange, "symboltoken": str(symbol_token), "interval": interval,
            "fromdate": start.strftime("%Y-%m-%d %H:%M"),
            "todate": end.strftime("%Y-%m-%d %H:%M"),
        }
        response = self.api.getCandleData(params)
        if not response or not response.get("status"):
            raise RuntimeError((response or {}).get("message", "Historical data request failed"))
        rows = response.get("data") or []
        frame = pd.DataFrame(rows, columns=["datetime", "open", "high", "low", "close", "volume"])
        if frame.empty:
            return frame
        frame["datetime"] = pd.to_datetime(frame["datetime"])
        for col in ["open", "high", "low", "close", "volume"]:
            frame[col] = pd.to_numeric(frame[col], errors="coerce")
        return frame.dropna().sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)

    def ltp(self, exchange, tradingsymbol, symbol_token):
        if not self.jwt:
            raise RuntimeError("Login required.")
        response = self.api.ltpData(exchange, tradingsymbol, str(symbol_token))
        if not response or not response.get("status"):
            raise RuntimeError((response or {}).get("message", "LTP request failed"))
        data = response.get("data") or {}
        price = data.get("ltp")
        if price is None:
            raise RuntimeError("Angel One LTP response did not contain a price.")
        return {"price": float(price), "exchange": exchange, "tradingsymbol": tradingsymbol,
                "symbol_token": str(symbol_token),
                "raw_time": data.get("exchangeTimestamp") or data.get("exchange_timestamp")}

    def start_market_websocket(self, symbol_token, on_tick, exchange_type=1):
        """Start SmartAPI WebSocket 2.0 in a daemon thread; no order APIs are used."""
        if not self.jwt or not self.feed_token:
            raise RuntimeError("Login required before starting the market WebSocket.")
        with self._ws_lock:
            if self.ws_thread and self.ws_thread.is_alive():
                return {"started": True, "already_running": True}
            self.ws_error = None
            self.ws_connected = False
            self.ws = SmartWebSocketV2(
                self.jwt, self.credentials.api_key, self.credentials.client_code, self.feed_token
            )

            def on_open(wsapp):
                self.ws_connected = True
                logger.info("Angel One WebSocket connected; subscribing to NIFTY token %s", symbol_token)
                self.ws.subscribe(
                    correlation_id="nifty-smc-live",
                    mode=1,  # LTP mode
                    token_list=[{"exchangeType": int(exchange_type), "tokens": [str(symbol_token)]}],
                )

            def on_data(wsapp, message):
                try:
                    if isinstance(message, dict):
                        on_tick(message)
                except Exception:
                    logger.exception("Could not process WebSocket tick")

            def on_error(wsapp, error):
                self.ws_error = str(error)[:300]
                logger.error("Angel One WebSocket error: %s", self.ws_error)

            def on_close(wsapp):
                self.ws_connected = False
                logger.warning("Angel One WebSocket closed")

            self.ws.on_open = on_open
            self.ws.on_data = on_data
            self.ws.on_error = on_error
            self.ws.on_close = on_close

            def run_socket():
                try:
                    self.ws.connect()
                except Exception as exc:
                    self.ws_error = str(exc)[:300]
                    self.ws_connected = False
                    logger.exception("WebSocket connection failed")

            self.ws_thread = threading.Thread(target=run_socket, name="angel-smartapi-ws", daemon=True)
            self.ws_thread.start()
            return {"started": True, "already_running": False}

    def close_market_websocket(self):
        with self._ws_lock:
            ws = self.ws
            thread = self.ws_thread
            if ws:
                try:
                    ws.close_connection()
                except Exception:
                    logger.exception("Error closing SmartAPI WebSocket")
            self.ws_connected = False
        # Let the socket worker finish before a replacement subscription starts.
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        with self._ws_lock:
            if self.ws_thread is thread and (not thread or not thread.is_alive()):
                self.ws_thread = None
                self.ws = None
