"""Audit session for Thursday 2026-11-05 to analyze why no trades were taken.
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
    print("Fetching today's 5m candle data (2026-11-05)...")
    dfs = {}
    for name, sym in SYMBOLS.items():
        d = yf.download(sym, period="5d", interval="5m", progress=False)
        if not d.empty:
            if hasattr(d.columns, 'levels') and len(d.columns.levels) > 1:
                d.columns = d.columns.get_level_values(0)
            d.index = pd.to_datetime(d.index).tz_convert(IST)
            dfs[name] = d

    nifty_df = dfs.get("NIFTY 50")
    if nifty_df is None or nifty_df.empty:
        print("Could not download NIFTY 50 data")
        return

    dates = sorted(list(set(nifty_df.index.strftime('%Y-%m-%d'))))
    print(f"Available session dates: {dates}")
    target_date = dates[-1]
    print(f"Auditing target session: {target_date}")

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
    for idx, row in entry_bars.iterrows():
        t_str = idx.strftime('%H:%M')
        c = row['Close']
        ema20 = row['ema20']
        ema50 = row['ema50']
        adx = row['adx14']
        
        # APEX-3 regime logic:
        # Bullish: Close > EMA20 > EMA50, ADX >= 20, slope > 0
        # Bearish: Close < EMA20 < EMA50, ADX >= 20, slope < 0
        # Otherwise: Neutral
        if c > ema20 and ema20 > ema50 and adx >= 20:
            regime = "BULLISH"
            bullish_bars += 1
        elif c < ema20 and ema20 < ema50 and adx >= 20:
            regime = "BEARISH"
            bearish_bars += 1
        else:
            regime = "NEUTRAL"
            neutral_bars += 1
        regime_map[idx] = regime

    print(f"Total 5m bars in entry window: {len(entry_bars)}")
    print(f"Bullish bars: {bullish_bars} ({bullish_bars/len(entry_bars)*100:.1f}%)" if len(entry_bars) else "0")
    print(f"Bearish bars: {bearish_bars} ({bearish_bars/len(entry_bars)*100:.1f}%)" if len(entry_bars) else "0")
    print(f"Neutral bars: {neutral_bars} ({neutral_bars/len(entry_bars)*100:.1f}%)" if len(entry_bars) else "0")

    print("\n" + "=" * 90)
    print(f"{'SYMBOL':<12} | {'OR WIDTH':<10} | {'OR GATE 1':<12} | {'BREAKOUT CANDIDATES':<25} | {'PRIMARY FILTER REASON'}")
    print("=" * 90)

    for name in sorted(SYMBOLS.keys()):
        if name == "NIFTY 50":
            continue
        df = dfs.get(name)
        if df is None or df.empty:
            print(f"{name:<12} | NO DATA")
            continue

        today_df = df[df.index.strftime('%Y-%m-%d') == target_date]
        if len(today_df) < 3:
            print(f"{name:<12} | INSUFFICIENT BARS (< 3)")
            continue

        # Opening Range: 09:15 to 09:30 (first 3 bars)
        or_df = today_df[(today_df.index.time >= time(9, 15)) & (today_df.index.time <= time(9, 25))]
        if len(or_df) < 3:
            print(f"{name:<12} | INCOMPLETE OR ({len(or_df)} bars)")
            continue

        or_high = or_df['High'].max()
        or_low = or_df['Low'].min()
        or_open = or_df.iloc[0]['Open']
        or_width_pct = ((or_high - or_low) / or_open) * 100

        # Gate 1: OR width <= 1.05%
        gate1_pass = or_width_pct <= 1.05
        gate1_status = "PASS" if gate1_pass else f"FAIL ({or_width_pct:.2f}%)"

        # Check candidate breakouts in entry window (09:35 - 12:30)
        eval_df = today_df[(today_df.index.time >= time(9, 35)) & (today_df.index.time <= time(12, 30))].copy()
        
        # Compute indicators
        eval_df['atr14'] = compute_atr(df.loc[:eval_df.index[-1]], 14).loc[eval_df.index]
        eval_df['vol_ma20'] = df['Volume'].rolling(20).mean().loc[eval_df.index]
        
        long_breakouts = eval_df[eval_df['Close'] > or_high]
        short_breakouts = eval_df[eval_df['Close'] < or_low]
        total_breakouts = len(long_breakouts) + len(short_breakouts)

        reasons = []
        if not gate1_pass:
            reasons.append(f"Gate 1: OR Width {or_width_pct:.2f}% > 1.05% limit (High volatility/exhaustion)")
        elif total_breakouts == 0:
            reasons.append("Remained coiled inside Opening Range (No breakout)")
        else:
            # Check reasons for breakout rejection
            sample_reasons = []
            for b_idx, b_row in pd.concat([long_breakouts, short_breakouts]).iterrows():
                b_side = "BUY" if b_row['Close'] > or_high else "SELL"
                regime = regime_map.get(b_idx, "NEUTRAL")
                
                # Gate 3: Alignment
                if b_side == "BUY" and regime != "BULLISH":
                    sample_reasons.append(f"Gate 3: Long breakout in {regime} market at {b_idx.strftime('%H:%M')}")
                elif b_side == "SELL" and regime != "BEARISH":
                    sample_reasons.append(f"Gate 3: Short breakout in {regime} market at {b_idx.strftime('%H:%M')}")

                # Gate 5: Volume multiple >= 2.0x
                vol_mult = b_row['Volume'] / b_row['vol_ma20'] if b_row['vol_ma20'] > 0 else 0
                if vol_mult < 2.0:
                    sample_reasons.append(f"Gate 5: Weak volume {vol_mult:.2f}x < 2.0x threshold at {b_idx.strftime('%H:%M')}")

                # Gate 7: CLV >= 0.75
                rng = b_row['High'] - b_row['Low']
                clv = (b_row['Close'] - b_row['Low']) / rng if rng > 0 else 0
                if b_side == "BUY" and clv < 0.75:
                    sample_reasons.append(f"Gate 7: Weak Close Location Value {clv:.2f} < 0.75 (wick rejection)")
                elif b_side == "SELL" and (1 - clv) < 0.75:
                    sample_reasons.append(f"Gate 7: Weak CLV {1-clv:.2f} < 0.75 for Short")

                # Gate 9A: ATR / Price >= 0.22%
                atr_ratio = (b_row['atr14'] / b_row['Close']) * 100 if b_row['Close'] > 0 else 0
                if atr_ratio < 0.22:
                    sample_reasons.append(f"Gate 9A: Sluggish volatility ATR/Price {atr_ratio:.2f}% < 0.22%")

            if sample_reasons:
                reasons.append(sample_reasons[0])
            else:
                reasons.append("Filtered by Sizing / Risk Bounds")

        primary_reason = reasons[0] if reasons else "No triggers"
        print(f"{name:<12} | {or_width_pct:>7.2f}% | {gate1_status:<12} | {total_breakouts:>2} bars (L:{len(long_breakouts)}, S:{len(short_breakouts)}) | {primary_reason}")

if __name__ == "__main__":
    main()

