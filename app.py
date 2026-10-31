import os
from datetime import datetime, timedelta
import streamlit as st
import pandas as pd
from dotenv import load_dotenv
from angel_client import AngelClient, load_credentials
from scanner import build_timeframes, analyze_smc

load_dotenv()
st.set_page_config(page_title="NIFTY SMC Scanner", layout="wide")
st.title("NIFTY 50 · Multi-Timeframe SMC Scanner")
st.caption("Angel One SmartAPI · Weekly options context · Moderate risk · Manual execution only")

with st.sidebar:
    st.header("Connection")
    client_code = st.text_input("Angel One client code", value=os.getenv("ANGEL_CLIENT_CODE", ""))
    api_key = st.text_input("SmartAPI key", value=os.getenv("ANGEL_API_KEY", ""), type="password")
    pin = st.text_input("PIN / password", value=os.getenv("ANGEL_PIN", ""), type="password")
    totp_secret = st.text_input("TOTP secret", value=os.getenv("ANGEL_TOTP_SECRET", ""), type="password")
    st.caption("Credentials stay on this computer. Never share them in chat or commit .env.")
    st.divider()
    token = st.text_input("NIFTY 50 symbol token", value=os.getenv("ANGEL_NIFTY_TOKEN", "99926000"))
    exchange = st.selectbox("Exchange", ["NSE"], index=0)
    st.caption("Verify the token against Angel One's current instrument master before use.")
    days = st.number_input("Historical trading days", min_value=1, max_value=250, value=15, step=1)

if "angel_client" not in st.session_state:
    st.session_state.angel_client = None
if "login_message" not in st.session_state:
    st.session_state.login_message = ""

c1, c2 = st.columns(2)
with c1:
    if st.button("Login to Angel One", type="primary"):
        try:
            creds = load_credentials(client_code, api_key, pin, totp_secret)
            client = AngelClient(creds)
            client.login()
            st.session_state.angel_client = client
            st.session_state.login_message = "Login successful. Credentials were not displayed."
        except Exception as e:
            st.session_state.angel_client = None
            st.session_state.login_message = f"Login failed: {e}"
with c2:
    fetch = st.button("Fetch historical candles")

if st.session_state.login_message:
    st.info(st.session_state.login_message)

if fetch:
    if not st.session_state.angel_client:
        st.error("Login first.")
    else:
        try:
            end = datetime.now()
            start = end - timedelta(days=int(days * 7 / 5) + 14)
            with st.spinner("Fetching NIFTY historical candles..."):
                one = st.session_state.angel_client.historical(
                    exchange, token, "ONE_MINUTE", start, end
                )
                five = st.session_state.angel_client.historical(
                    exchange, token, "FIVE_MINUTE", start, end
                )
                fifteen = st.session_state.angel_client.historical(
                    exchange, token, "FIFTEEN_MINUTE", start, end
                )
            frames = build_timeframes(one, five, fifteen)
            st.session_state.frames = frames
            st.success(f"Loaded {len(one)} one-minute candles.")
        except Exception as e:
            st.error(f"Data fetch failed: {e}")

if "frames" in st.session_state:
    frames = st.session_state.frames
    tabs = st.tabs(["15m Bias", "5m Structure", "2m Setup", "1m Entry"])
    for tab, tf in zip(tabs, ["15m", "5m", "2m", "1m"]):
        with tab:
            df = frames.get(tf, pd.DataFrame())
            if df.empty:
                st.warning(f"No {tf} candles available.")
                continue
            result = analyze_smc(df)
            a, b, c = st.columns(3)
            a.metric("Candles", len(df))
            b.metric("Structure", result["structure"])
            c.metric("Latest signal", result["signal"])
            st.write("**SMC findings**")
            for item in result["findings"]:
                st.write(f"- {item}")
            st.dataframe(df.tail(30), use_container_width=True)
    st.subheader("Combined multi-timeframe decision")
    bias = analyze_smc(frames["15m"])["structure"]
    structure = analyze_smc(frames["5m"])["structure"]
    setup = analyze_smc(frames["2m"])["signal"]
    entry = analyze_smc(frames["1m"])["signal"]
    if bias == "Bullish" and structure == "Bullish" and setup == "CE_SETUP" and entry == "CE_SETUP":
        combined = "CE WATCH — wait for your entry confirmation"
    elif bias == "Bearish" and structure == "Bearish" and setup == "PE_SETUP" and entry == "PE_SETUP":
        combined = "PE WATCH — wait for your entry confirmation"
    else:
        combined = "WAIT — timeframes are not aligned"
    st.info(combined)
    st.warning("Research prototype only. Signals are heuristic, not validated recommendations. No option selection, premium/Greeks, order placement, or guaranteed live alerts.")
else:
    st.markdown("""
    ### Selected configuration
    - Multi-timeframe: **15m bias → 5m structure → 2m setup → 1m entry**
    - SMC: market structure, BOS/MSS proxy, FVG, liquidity levels/sweeps, equal highs/lows, candidate order blocks, premium/discount, displacement proxy
    - Options context: **weekly expiry**, moderate risk; manual execution only
    - Historical analysis: last 15 trading days (requests a wider calendar window to cover weekends/holidays)

    Start by logging in, then fetch historical candles. This version does not yet run a persistent live WebSocket alert service.
    """)
