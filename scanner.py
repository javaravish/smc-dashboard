import pandas as pd
import math

def _resample_session(df, minutes):
    if df is None or df.empty:
        return pd.DataFrame(columns=["datetime","open","high","low","close","volume"])
    x = df.copy()
    x["datetime"] = pd.to_datetime(x["datetime"])
    x = x.sort_values("datetime").set_index("datetime")
    grouped = x.resample(f"{minutes}min", origin="start_day", offset="9h15min",
                         label="left", closed="left").agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"})
    counts = x["close"].resample(f"{minutes}min", origin="start_day",
                                 offset="9h15min", label="left", closed="left").count()
    return grouped[counts == minutes].dropna(subset=["open","high","low","close"]).reset_index()

def build_timeframes(one, five=None, fifteen=None):
    one = one.copy()
    if one.empty:
        empty = one.copy()
        return {"1m":empty, "2m":empty, "5m":empty, "15m":empty}
    one["datetime"] = pd.to_datetime(one["datetime"])
    return {"1m":one.sort_values("datetime").reset_index(drop=True),
            "2m":_resample_session(one,2), "5m":_resample_session(one,5),
            "15m":_resample_session(one,15)}

def _atr(d, period=14):
    prev = d.close.shift(1)
    tr = pd.concat([(d.high-d.low).abs(), (d.high-prev).abs(),
                    (d.low-prev).abs()], axis=1).max(axis=1)
    value = tr.tail(period).mean()
    return float(value) if pd.notna(value) else 0.0

def _confirmed_swings(d, left=2, right=2):
    highs, lows = [], []
    # Only pivots with right-hand confirmation candles are considered.
    for i in range(left, max(left, len(d)-right)):
        if i+right >= len(d):
            break
        h, l = float(d.high.iloc[i]), float(d.low.iloc[i])
        if h >= float(d.high.iloc[i-left:i+right+1].max()):
            highs.append((i,h))
        if l <= float(d.low.iloc[i-left:i+right+1].min()):
            lows.append((i,l))
    return highs, lows

def _clusters(levels, tolerance):
    vals = sorted(float(x) for x in levels)
    groups = []
    for v in vals:
        if not groups or abs(v - sum(groups[-1])/len(groups[-1])) > tolerance:
            groups.append([v])
        else:
            groups[-1].append(v)
    return [sum(g)/len(g) for g in groups if len(g) >= 2]

def _session_label(ts):
    try:
        t = pd.Timestamp(ts).time()
        m = t.hour*60+t.minute
        if 9*60+15 <= m < 10*60+30: return "London/India open overlap"
        if 10*60+30 <= m < 12*60: return "Morning session"
        if 12*60 <= m < 13*60+30: return "Lunch / low-liquidity window"
        if 13*60+30 <= m < 15*60+15: return "Afternoon session"
    except Exception:
        pass
    return "Outside regular session"

def analyze_smc(df, lookback=80):
    if df is None or len(df) < 15:
        return {"structure":"Insufficient data","signal":"WAIT",
                "findings":["Need at least 15 completed candles."],"score":0}
    d=df.tail(max(lookback,15)).copy().reset_index(drop=True)
    for c in ("open","high","low","close"):
        d[c]=pd.to_numeric(d[c],errors="coerce")
    d=d.dropna(subset=["open","high","low","close"]).reset_index(drop=True)
    if len(d)<15:
        return {"structure":"Insufficient data","signal":"WAIT","findings":["Not enough valid OHLC candles."],"score":0}
    last=d.iloc[-1]; prev=d.iloc[-2]
    px=float(last.close); atr=max(_atr(d),0.05); tol=max(atr*0.18,1.0)
    highs,lows=_confirmed_swings(d)
    pivot_highs=[v for _,v in highs]; pivot_lows=[v for _,v in lows]
    prior_high=pivot_highs[-1] if pivot_highs else float(d.high.iloc[:-2].tail(10).max())
    prior_low=pivot_lows[-1] if pivot_lows else float(d.low.iloc[:-2].tail(10).min())
    bullish_bos=px>prior_high and float(prev.close)<=prior_high
    bearish_bos=px<prior_low and float(prev.close)>=prior_low

    # Liquidity pools: repeated swing highs/lows, plus the nearest confirmed pivots.
    eqh=_clusters(pivot_highs[-12:],tol); eql=_clusters(pivot_lows[-12:],tol)
    recent_high=max(pivot_highs[-8:]) if pivot_highs else float(d.high.iloc[:-1].tail(10).max())
    recent_low=min(pivot_lows[-8:]) if pivot_lows else float(d.low.iloc[:-1].tail(10).min())
    sweep_high=float(last.high)>recent_high and px<recent_high
    sweep_low=float(last.low)<recent_low and px>recent_low
    internal_window=d.iloc[-6:-1]
    internal_high=float(internal_window.high.max()) if not internal_window.empty else recent_high
    internal_low=float(internal_window.low.min()) if not internal_window.empty else recent_low
    inducement_bear=float(last.high)>internal_high and px<internal_high and not sweep_high
    inducement_bull=float(last.low)<internal_low and px>internal_low and not sweep_low

    # Three-candle FVG / inverse FVG.
    a,c=d.iloc[-3],d.iloc[-1]
    bull_fvg=float(c.low)>float(a.high)
    bear_fvg=float(c.high)<float(a.low)
    bull_fvg_zone=(float(a.high),float(c.low)) if bull_fvg else None
    bear_fvg_zone=(float(c.high),float(a.low)) if bear_fvg else None
    bull_gaps=[]; bear_gaps=[]
    for j in range(max(2,len(d)-15),len(d)):
        if float(d.low.iloc[j])>float(d.high.iloc[j-2]):
            bull_gaps.append((float(d.high.iloc[j-2]),float(d.low.iloc[j])))
        if float(d.high.iloc[j])<float(d.low.iloc[j-2]):
            bear_gaps.append((float(d.high.iloc[j]),float(d.low.iloc[j-2])))
    prior_bull_gap=next((z for z in reversed(bull_gaps) if z!=bull_fvg_zone),None)
    prior_bear_gap=next((z for z in reversed(bear_gaps) if z!=bear_fvg_zone),None)
    # An inverse FVG is only flagged after price closes through the far edge
    # of a previously formed imbalance.
    ifvg_bear=bool(prior_bull_gap and px<prior_bull_gap[0])
    ifvg_bull=bool(prior_bear_gap and px>prior_bear_gap[1])

    body=abs(float(last.close)-float(last.open))
    avg_body=float((d.close-d.open).abs().tail(20).mean() or 0)
    displacement=avg_body>0 and body>=1.6*avg_body and (float(last.close)>float(last.open) if bullish_bos or sweep_low else float(last.close)<float(last.open) if bearish_bos or sweep_high else True)
    vol_mean=float(d.volume.tail(20).mean()) if "volume" in d else 0.0
    volume_expansion=bool(vol_mean>0 and float(last.get("volume",0) or 0)>=1.5*vol_mean)

    # Order block: last opposite candle in the 8 bars before the impulse/BOS candle.
    bull_ob=bear_ob=None
    prior=d.iloc[:-1].tail(8)
    if bullish_bos or (sweep_low and displacement):
        opp=prior[prior.close<prior.open]
        if not opp.empty:
            z=opp.iloc[-1]; bull_ob=(float(z.low),float(z.high))
    if bearish_bos or (sweep_high and displacement):
        opp=prior[prior.close>prior.open]
        if not opp.empty:
            z=opp.iloc[-1]; bear_ob=(float(z.low),float(z.high))
    bull_ob_mitigated=bool(bull_ob and float(last.low)<=bull_ob[1] and float(last.high)>=bull_ob[0])
    bear_ob_mitigated=bool(bear_ob and float(last.high)>=bear_ob[0] and float(last.low)<=bear_ob[1])
    breaker_bull=bool(bear_ob and px>bear_ob[1])
    breaker_bear=bool(bull_ob and px<bull_ob[0])

    # Trend change / market structure shift from recent confirmed pivots.
    prior_trend="Bullish" if len(pivot_highs)>=2 and len(pivot_lows)>=2 and pivot_highs[-1]>pivot_highs[-2] and pivot_lows[-1]>pivot_lows[-2] else (
        "Bearish" if len(pivot_highs)>=2 and len(pivot_lows)>=2 and pivot_highs[-1]<pivot_highs[-2] and pivot_lows[-1]<pivot_lows[-2] else "Neutral")
    choch_bull=prior_trend=="Bearish" and bullish_bos
    choch_bear=prior_trend=="Bullish" and bearish_bos
    mss_bull=choch_bull or (sweep_low and bullish_bos)
    mss_bear=choch_bear or (sweep_high and bearish_bos)

    swing_high=max(pivot_highs[-8:]) if pivot_highs else float(d.high.tail(20).max())
    swing_low=min(pivot_lows[-8:]) if pivot_lows else float(d.low.tail(20).min())
    dealing_range=max(swing_high-swing_low,0.01); eq=(swing_high+swing_low)/2
    premium=px>eq
    # OTE retracement band of the latest dealing range.
    bull_ote=(swing_high-0.79*dealing_range <= px <= swing_high-0.62*dealing_range)
    bear_ote=(swing_low+0.62*dealing_range <= px <= swing_low+0.79*dealing_range)

    # Previous-day high/low and current-session high/low (available history only).
    d["day"]=pd.to_datetime(d.datetime).dt.date
    days=sorted(d.day.unique())
    pdh=pdl=None
    if len(days)>=2:
        prior_day=d[d.day==days[-2]]
        if not prior_day.empty:
            pdh=float(prior_day.high.max()); pdl=float(prior_day.low.min())
    current=d[d.day==days[-1]]
    session_high=float(current.high.max()) if not current.empty else None
    session_low=float(current.low.min()) if not current.empty else None
    session=_session_label(last.datetime)

    bull_score=sum([bullish_bos,sweep_low,inducement_bull,bull_fvg,displacement,volume_expansion,bull_ob is not None,
                    choch_bull,mss_bull,bull_ote,not premium,ifvg_bull,breaker_bull])
    bear_score=sum([bearish_bos,sweep_high,inducement_bear,bear_fvg,displacement,volume_expansion,bear_ob is not None,
                    choch_bear,mss_bear,bear_ote,premium,ifvg_bear,breaker_bear])
    # A structure break is mandatory; score adds independent confluence.
    direction="CE_SETUP" if bullish_bos and bull_score>=3 else (
        "PE_SETUP" if bearish_bos and bear_score>=3 else "WAIT")
    findings=[
        f"Structure: {prior_trend}; BOS bullish={bool(bullish_bos)}, bearish={bool(bearish_bos)}.",
        f"Liquidity: EQH {', '.join(f'{x:.2f}' for x in eqh) if eqh else 'none'}; EQL {', '.join(f'{x:.2f}' for x in eql) if eql else 'none'}.",
        f"Liquidity sweep: BSL={bool(sweep_high)}, SSL={bool(sweep_low)}; inducement candidates bullish={bool(inducement_bull)}, bearish={bool(inducement_bear)}.",
        f"Imbalance: bullish FVG={bool(bull_fvg)}, bearish FVG={bool(bear_fvg)}, IFVG bullish={bool(ifvg_bull)}, bearish={bool(ifvg_bear)}.",
        f"Displacement={bool(displacement)}; volume expansion={bool(volume_expansion)}; bullish OB={bull_ob}; bearish OB={bear_ob}.",
        f"OB mitigation bullish={bull_ob_mitigated}, bearish={bear_ob_mitigated}; breaker bullish={breaker_bull}, bearish={breaker_bear}.",
        f"Dealing range {swing_low:.2f}-{swing_high:.2f}; equilibrium={eq:.2f}; location={'Premium' if premium else 'Discount'}.",
        f"OTE bullish={bool(bull_ote)}, bearish={bool(bear_ote)}; session={session}.",
        f"Previous-day high/low={pdh}/{pdl}; current-session high/low={session_high}/{session_low}."
    ]
    if bull_fvg_zone: findings.append(f"Bullish FVG zone {bull_fvg_zone[0]:.2f}-{bull_fvg_zone[1]:.2f}.")
    if bear_fvg_zone: findings.append(f"Bearish FVG zone {bear_fvg_zone[0]:.2f}-{bear_fvg_zone[1]:.2f}.")
    # Structural invalidation: nearest valid structure/OB/sweep anchor plus ATR buffer.
    stop=target=None; stop_reason=target_reason=None; risk=reward=None
    if direction=="CE_SETUP":
        candidates=[x for x in [recent_low, bull_ob[0] if bull_ob else None,
                                float(last.low) if sweep_low else None] if x is not None and x<px]
        if candidates:
            stop=max(candidates)-max(atr*0.08,0.5)
            risk=px-stop
        pools=[x for x in (eqh+[v for v in pivot_highs[-12:]]+[x for x in [pdh,session_high] if x is not None]) if x>px]
        if risk and risk>0:
            valid=sorted(set(round(x,2) for x in pools if x-px>risk))
            if valid: target=valid[0]; reward=target-px
        stop_reason="Below nearest valid bullish structure / OB / sweep low"
        target_reason="Nearest opposing buy-side liquidity beyond 1R"
    elif direction=="PE_SETUP":
        candidates=[x for x in [recent_high, bear_ob[1] if bear_ob else None,
                                float(last.high) if sweep_high else None] if x is not None and x>px]
        if candidates:
            stop=min(candidates)+max(atr*0.08,0.5)
            risk=stop-px
        pools=[x for x in (eql+[v for v in pivot_lows[-12:]]+[x for x in [pdl,session_low] if x is not None]) if x<px]
        if risk and risk>0:
            valid=sorted(set(round(x,2) for x in pools if px-x>risk))
            if valid: target=valid[-1]; reward=px-target
        stop_reason="Above nearest valid bearish structure / OB / sweep high"
        target_reason="Nearest opposing sell-side liquidity beyond 1R"
    if direction!="WAIT":
        if stop is None or target is None or risk is None or reward is None:
            findings.append("Setup rejected: no defensible structural stop and opposing-liquidity target were both found.")
            direction="WAIT"
        elif risk<=0 or reward<=risk:
            findings.append(f"Setup rejected: risk={risk:.2f} points; reward={reward:.2f} points. Require positive structural risk and reward greater than risk.")
            direction="WAIT"
    structure="Bullish" if bullish_bos else "Bearish" if bearish_bos else prior_trend
    return {"structure":structure,"signal":direction,"findings":findings,
            "swing_high":swing_high,"swing_low":swing_low,"equilibrium":eq,
            "premium_discount":"Premium" if premium else "Discount",
            "last_close":px,"last_time":str(last.datetime),
            "bull_score":int(bull_score),"bear_score":int(bear_score),
            "liquidity":{"equal_highs":eqh,"equal_lows":eql,"sweep_bsl":bool(sweep_high),"sweep_ssl":bool(sweep_low),
                         "inducement_bullish":bool(inducement_bull),"inducement_bearish":bool(inducement_bear),
                         "previous_day_high":pdh,"previous_day_low":pdl,
                         "session_high":session_high,"session_low":session_low},
            "imbalance":{"bullish_fvg":bull_fvg_zone,"bearish_fvg":bear_fvg_zone,
                         "bullish_ifvg":bool(ifvg_bull),"bearish_ifvg":bool(ifvg_bear)},
            "institutional_zones":{"bullish_order_block":bull_ob,"bearish_order_block":bear_ob,
                                   "bullish_ob_mitigated":bull_ob_mitigated,"bearish_ob_mitigated":bear_ob_mitigated,
                                   "bullish_breaker":breaker_bull,"bearish_breaker":breaker_bear},
            "ote":{"bullish":bool(bull_ote),"bearish":bool(bear_ote)},
            "session":session,"stop_loss":None if direction=="WAIT" else round(float(stop),2),
            "target":None if direction=="WAIT" else round(float(target),2),
            "risk_points":None if direction=="WAIT" else round(float(risk),2),
            "reward_points":None if direction=="WAIT" else round(float(reward),2),
            "stop_reason":stop_reason,"target_reason":target_reason}

def analyze_multi_timeframe(frames):
    results={tf:analyze_smc(frame) for tf,frame in frames.items()}
    directions=[results.get(tf,{}).get("signal") for tf in ("15m","5m","2m","1m")]
    if all(x=="CE_SETUP" for x in directions):
        signal="CE"
    elif all(x=="PE_SETUP" for x in directions):
        signal="PE"
    else:
        signal="WAIT"
    entry=results.get("1m",{}).get("last_close")
    execution=results.get("1m",{})
    stop=execution.get("stop_loss"); target=execution.get("target")
    risk=execution.get("risk_points"); reward=execution.get("reward_points")
    if signal in ("CE","PE") and (stop is None or target is None or risk is None or reward is None):
        signal="WAIT"
    return {"signal":signal,"timeframes":results,"entry":entry,
            "stop_loss":stop if signal!="WAIT" else None,
            "target":target if signal!="WAIT" else None,
            "risk_points":risk if signal!="WAIT" else None,
            "reward_points":reward if signal!="WAIT" else None,
            "stop_reason":execution.get("stop_reason"),
            "target_reason":execution.get("target_reason"),
            "message":"Strict 15m/5m/2m/1m SMC agreement; 1m structure defines execution levels."}

def historical_replay(frames,max_trades=500):
    one=frames["1m"].reset_index(drop=True)
    if len(one)<100:
        return {"signals":[],"summary":{"trades":0,"message":"Not enough 1-minute history for reliable replay."}}
    signals=[]; profitable_first_trade_days=set(); first_trade_seen_days=set()
    cursors={tf:0 for tf in ("15m","5m","2m","1m")}
    i=60
    while i<len(one)-1 and len(signals)<max_trades:
        stamp=pd.Timestamp(one.iloc[i].datetime)
        if stamp.time().hour<9 or (stamp.time().hour==9 and stamp.time().minute<15):
            i+=1; continue
        # Cheap 10-bar breakout prefilter avoids running the full multi-timeframe
        # SMC engine on every historical minute when no directional break exists.
        pre=one.iloc[max(0,i-10):i]
        if len(pre)<10:
            i+=1; continue
        curbar=one.iloc[i]
        quick_ce=float(curbar.close)>float(pre.high.max()) and float(curbar.close)>float(curbar.open)
        quick_pe=float(curbar.close)<float(pre.low.min()) and float(curbar.close)<float(curbar.open)
        if not (quick_ce or quick_pe):
            i+=1; continue
        snapshots={}
        for tf in ("15m","5m","2m","1m"):
            frame=frames[tf]
            while cursors[tf]+1<len(frame) and pd.Timestamp(frame.iloc[cursors[tf]+1].datetime)<=stamp:
                cursors[tf]+=1
            cur=cursors[tf]
            snapshots[tf]=frame.iloc[max(0,cur-59):cur+1]
        if any(len(snapshots[tf])<15 for tf in ("15m","5m","2m","1m")):
            i+=1; continue
        # Evaluate the entry timeframe first; higher-timeframe calculations are
        # only needed when the 1m chart itself has a qualified structural break.
        one_minute=analyze_smc(snapshots["1m"])
        if one_minute.get("signal") not in ("CE_SETUP","PE_SETUP"):
            i+=1; continue
        results={"1m":one_minute}
        for tf in ("15m","5m","2m"):
            results[tf]=analyze_smc(snapshots[tf])
        directions=[results[tf]["signal"] for tf in ("15m","5m","2m","1m")]
        direction="CE" if all(x=="CE_SETUP" for x in directions) else "PE" if all(x=="PE_SETUP" for x in directions) else "WAIT"
        analysis={"signal":direction,"stop_loss":one_minute.get("stop_loss"),
                  "target":one_minute.get("target")}
        if direction not in ("CE","PE"):
            i+=1; continue
        session_day=str(pd.Timestamp(one.iloc[i+1].datetime).date())
        if session_day in profitable_first_trade_days:
            i+=1; continue
        entry_bar=one.iloc[i+1]; entry=float(entry_bar.open)
        stop=analysis.get("stop_loss"); target=analysis.get("target")
        if stop is None or target is None:
            i+=1; continue
        stop=float(stop); target=float(target)
        risk=entry-stop if direction=="CE" else stop-entry
        reward=target-entry if direction=="CE" else entry-target
        if risk<=0 or reward<=risk:
            i+=1; continue
        outcome,exit_price,exit_time="OPEN / not resolved",None,None
        exit_index=None
        # Resolve within the same NSE session; never carry a replay trade overnight.
        for j in range(i+1,len(one)):
            bar=one.iloc[j]
            if str(pd.Timestamp(bar.datetime).date())!=session_day:
                break
            bt=pd.Timestamp(bar.datetime)
            lo,hi=float(bar.low),float(bar.high)
            hit_sl=(lo<=stop) if direction=="CE" else (hi>=stop)
            hit_tp=(hi>=target) if direction=="CE" else (lo<=target)
            if hit_sl and hit_tp:
                outcome,exit_price,exit_time="STOP (conservative same-bar rule)",stop,str(bar.datetime); exit_index=j; break
            if hit_sl:
                outcome,exit_price,exit_time="STOP",stop,str(bar.datetime); exit_index=j; break
            if hit_tp:
                outcome,exit_price,exit_time="TARGET",target,str(bar.datetime); exit_index=j; break
            if (bt.hour,bt.minute)>=(15,25):
                outcome="EOD PROFIT" if ((float(bar.close)-entry) if direction=="CE" else (entry-float(bar.close)))>0 else "EOD LOSS"
                exit_price=float(bar.close); exit_time=str(bar.datetime); exit_index=j; break
        if exit_index is None:
            # If data ends before 15:25, close at the last available bar of that session.
            same_day=[j for j in range(i+1,len(one)) if str(pd.Timestamp(one.iloc[j].datetime).date())==session_day]
            if same_day:
                exit_index=same_day[-1]; bar=one.iloc[exit_index]; exit_price=float(bar.close)
                pnl=(exit_price-entry) if direction=="CE" else (entry-exit_price)
                outcome="EOD PROFIT" if pnl>0 else "EOD LOSS"
                exit_time=str(bar.datetime)
        is_first=session_day not in first_trade_seen_days
        first_trade_seen_days.add(session_day)
        signals.append({"time":str(entry_bar.datetime),"signal":"BUY CE" if direction=="CE" else "BUY PE",
                        "entry":round(entry,2),"stop_loss":round(stop,2),"target":round(target,2),
                        "risk_points":round(risk,2),"reward_points":round(reward,2),
                        "outcome":outcome,"exit_price":None if exit_price is None else round(exit_price,2),
                        "exit_time":exit_time,"reason":"All four timeframes aligned; structural invalidation and opposing-liquidity target.",
                        "risk_reward":f"1:{reward/risk:.2f}","signal_bar_time":str(one.iloc[i].datetime)})
        pnl_points=None if exit_price is None else ((exit_price-entry) if direction=="CE" else (entry-exit_price))
        if is_first and pnl_points is not None and pnl_points>0:
            profitable_first_trade_days.add(session_day)
        if exit_index is None:
            break
        i=exit_index+1
    wins=sum(x["outcome"]=="TARGET" for x in signals)
    losses=sum(str(x["outcome"]).startswith("STOP") for x in signals)
    eod=sum(str(x["outcome"]).startswith("EOD") for x in signals)
    profitable=wins+sum(x["outcome"]=="EOD PROFIT" for x in signals)
    resolved=wins+losses+eod
    return {"signals":signals,"summary":{"trades":len(signals),"targets":wins,"stops":losses,
            "eod_exits":eod,"open":sum(x["exit_price"] is None for x in signals),
            "win_rate":round(100*profitable/resolved,2) if resolved else None,
            "message":"Replay uses SMC structural stops and liquidity targets; first profitable trade blocks further entries that session. Historical results do not guarantee future performance."}}
