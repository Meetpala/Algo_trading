"""Audit session for today (2026-11-04) to analyze why no trades were taken.
Replays actual 5m market data for NIFTY 50 and the 11 radar stocks through APEX-3 gates.
"""
import sys
import os
import pandas as pd
import numpy as np
import yfinance as yf
from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

SYMBOLS = {
    "NIFTY 50": "^NSEI",
    "HINDALCO": "HINDALCO.NS",
    "JSWSTEEL": "JSWSTEEL.NS",
    "RELIANCE": "RELIANCE.NS",
    "MARUTI": "MARUTI.NS",
    "TCS": "TCS.NS",
    "TRENT": "TRENT.NS",
    "BHARTIARTL": "BHARTIARTL.NS",
    "JIOFIN": "JIOFIN.NS",
    "TATASTEEL": "TATASTEEL.NS",
    "AXISBANK": "AXISBANK.NS",
    "TATAPOWER": "TATAPOWER.NS",
}

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    h, l, c = df['High'], df['Low'], df['Close']
    tr1 = h - l
    tr2 = (h - c.shift(1)).abs()
    tr3 = (l - c.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def compute_adx(df: pd.DataFrame, period: int = 14):
    h, l, c = df['High'], df['Low'], df['Close']
    tr1 = h - l
    tr2 = (h - c.shift(1)).abs()
    tr3 = (l - c.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    up = h - h.shift(1)
    down = l.shift(1) - l
    plus_dm = np.where((up > down) & (up > 0), up, 0.0)
    minus_dm = np.where((down > up) & (down > 0), down, 0.0)
    
    tr_s = tr.rolling(period).sum()
    p_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(period).sum() / tr_s)
    m_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(period).sum() / tr_s)
    
    dx = 100 * ((p_di - m_di).abs() / (p_di + m_di).replace(0, np.nan))
    return dx.rolling(period).mean()

def main():
    print("Fetching today's 5m candle data (2026-11-04)...")
    dfs = {}
    for name, sym in SYMBOLS.items():
        d = yf.download(sym, period="5d", interval="5m", progress=False)
        if not d.empty:
            if hasattr(d.columns, 'levels') and len(d.columns.levels) > 1:
                d.columns = d.columns.get_level_values(0)
            d.index = pd.to_datetime(d.index).tz_convert(IST)
            dfs[name] = d

    nifty_df = dfs.get("NIFTY 50")
    if nifty_df is None:
        print("Could not download NIFTY 50 data")
        return

    target_date = "2026-11-04"
    nifty_today = nifty_df[nifty_df.index.strftime('%Y-%m-%d') == target_date]
    print(f"\n--- NIFTY 50 Summary for {target_date} ---")
    print(f"Candles today: {len(nifty_today)}")
    if len(nifty_today) > 0:
        o = nifty_today.iloc[0]['Open']
        h = nifty_today['High'].max()
        l = nifty_today['Low'].min()
        c = nifty_today.iloc[-1]['Close']
        rng = h - l
        rng_pct = (rng / o) * 100
        change_pct = ((c - o) / o) * 100
        print(f"Open: {o:.2f} | High: {h:.2f} | Low: {l:.2f} | Close: {c:.2f}")
        print(f"Intraday Range: {rng:.2f} pts ({rng_pct:.2f}%) | Day Change: {change_pct:+.2f}%")

    nifty_df['ema20'] = nifty_df['Close'].ewm(span=20, adjust=False).mean()
    nifty_df['ema50'] = nifty_df['Close'].ewm(span=50, adjust=False).mean()
    nifty_df['adx14'] = compute_adx(nifty_df, 14)

    print("\n--- NIFTY 50 Regime During Entry Window (09:35 - 12:30 IST) ---")
    entry_bars = nifty_df[(nifty_df.index.strftime('%Y-%m-%d') == target_date) & 
                          (nifty_df.index.time >= time(9, 35)) & 
                          (nifty_df.index.time <= time(12, 30))]
    
    bullish_bars = 0
    bearish_bars = 0
    neutral_bars = 0
    regime_map = {}
    
    for dt, row in entry_bars.iterrows():
        t_str = dt.strftime('%H:%M')
        loc = nifty_df.index.get_loc(dt)
        prev_ema = nifty_df.iloc[loc - 3]['ema20']
        curr_ema = row['ema20']
        slope = (curr_ema - prev_ema) / prev_ema
        adx = row['adx14']
        close_px = row['Close']
        ema50 = row['ema50']
        
        is_bull = (slope > 0.0001) and (adx >= 22.0) and (close_px > curr_ema) and (curr_ema > ema50)
        is_bear = (slope < -0.0001) and (adx >= 22.0) and (close_px < curr_ema) and (curr_ema < ema50)
        
        state = "BULLISH" if is_bull else ("BEARISH" if is_bear else "NEUTRAL / CHOP")
        regime_map[t_str] = state
        if is_bull: bullish_bars += 1
        elif is_bear: bearish_bars += 1
        else: neutral_bars += 1
        
        if t_str in ['09:35', '10:00', '10:30', '11:00', '11:30', '12:00', '12:30']:
            print(f"[{t_str} IST] NIFTY: {close_px:.1f} | EMA20: {curr_ema:.1f} | Slope: {slope*100:+.4f}% | ADX: {adx:.1f} | State: {state}")

    print(f"\nRegime Breakdown across {len(entry_bars)} bars in Entry Window:")
    print(f"  Bullish Bars: {bullish_bars} ({bullish_bars/max(1, len(entry_bars))*100:.1f}%)")
    print(f"  Bearish Bars: {bearish_bars} ({bearish_bars/max(1, len(entry_bars))*100:.1f}%)")
    print(f"  Neutral / Chop Bars: {neutral_bars} ({neutral_bars/max(1, len(entry_bars))*100:.1f}%)")

    # Inspect all 11 radar stocks
    print("\n" + "="*70)
    print("--- 11 RADAR STOCKS BREAKOUT & GATE AUDIT (09:35 - 12:30 IST) ---")
    print("="*70)

    for name in [k for k in SYMBOLS.keys() if k != "NIFTY 50"]:
        df = dfs.get(name)
        if df is None or df.empty:
            continue
        
        # Compute indicators on full df before slicing
        df['vol_sma20'] = df['Volume'].rolling(20).mean()
        df['atr14'] = compute_atr(df, 14)

        day_df = df[df.index.strftime('%Y-%m-%d') == target_date]
        if day_df.empty:
            continue
            
        or_candles = day_df[(day_df.index.time >= time(9, 15)) & (day_df.index.time <= time(9, 30))]
        if len(or_candles) < 3:
            continue
            
        or_high = or_candles['High'].max()
        or_low = or_candles['Low'].min()
        or_range_pct = ((or_high - or_low) / or_low) * 100
        
        breakout_count = 0
        failed_gates_summary = []
        
        test_bars = day_df[(day_df.index.time >= time(9, 35)) & (day_df.index.time <= time(12, 30))]
        for dt, row in test_bars.iterrows():
            t_str = dt.strftime('%H:%M')
            c = row['Close']
            h = row['High']
            l = row['Low']
            v = row['Volume']
            atr = row['atr14']
            vol_avg = row['vol_sma20']
            nifty_state = regime_map.get(t_str, "NEUTRAL")
            
            # Check breakout
            is_breakout_up = c > (or_high + 0.15 * atr)
            is_breakout_down = c < (or_low - 0.15 * atr)
            
            if is_breakout_up or is_breakout_down:
                breakout_count += 1
                side = "LONG" if is_breakout_up else "SHORT"
                vol_surge = v / max(1.0, vol_avg)
                clv = (c - l) / max(0.01, h - l) if side == "LONG" else (h - c) / max(0.01, h - l)
                
                reasons = []
                # Gate 1: Regime alignment
                if side == "LONG" and nifty_state != "BULLISH":
                    reasons.append(f"Gate 1 Fail (NIFTY is {nifty_state}, not BULLISH)")
                elif side == "SHORT" and nifty_state != "BEARISH":
                    reasons.append(f"Gate 1 Fail (NIFTY is {nifty_state}, not BEARISH)")

                # Gate 5: Volume surge
                is_core = name in ["HINDALCO", "JSWSTEEL", "RELIANCE", "MARUTI", "TCS", "TRENT"]
                min_surge = 2.0 if is_core else 2.5
                if vol_surge < min_surge:
                    reasons.append(f"Gate 5 Fail (Vol Surge {vol_surge:.1f}x < {min_surge}x)")
                    
                # Gate 3: CLV
                min_clv = 0.70 if is_core else 0.75
                if clv < min_clv:
                    reasons.append(f"Gate 3 Fail (CLV {clv:.2f} < {min_clv})")
                    
                # Stop width Gate 9
                if side == "LONG":
                    stop_px = l - 1.5 * atr
                    r_pct = (c - stop_px) / c
                else:
                    stop_px = h + 1.5 * atr
                    r_pct = (stop_px - c) / c
                    
                if r_pct < 0.0055:
                    reasons.append(f"Gate 9B Fail (Stop too tight {r_pct*100:.2f}% < 0.55%)")
                elif r_pct > 0.0090:
                    reasons.append(f"Gate 9B Fail (Stop too wide {r_pct*100:.2f}% > 0.90%)")
                    
                failed_gates_summary.append((t_str, side, reasons if reasons else ["ALL GATES PASS -> VALID CANDIDATE!"]))
                
        print(f"\n{name} (OR High: {or_high:.2f}, Low: {or_low:.2f}, OR Range: {or_range_pct:.2f}%):")
        if breakout_count == 0:
            print("  -> Zero OR breakouts between 09:35 and 12:30 (Traded entirely inside Opening Range).")
        else:
            print(f"  -> {breakout_count} breakout candle(s) observed:")
            for t_str, side, r_list in failed_gates_summary[:4]:
                print(f"     [{t_str} IST] {side}: {', '.join(r_list)}")

if __name__ == "__main__":
    main()
