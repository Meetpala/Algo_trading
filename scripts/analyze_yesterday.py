"""Script to simulate APEX-3 trading for yesterday (2026-10-21).

Fetches the actual 5-minute market candles for yesterday across the universe and NIFTY,
runs the deterministic 12-step APEX-3 pipeline, and produces an itemized trade log
with exact Zerodha charges and net P&L.
"""
import os
import sys
import math

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, time, date
from zoneinfo import ZoneInfo

from apex3.config import CostsConfig
from backtest_engine import calculate_charges
from apex3.strategy.regime import BenchmarkSnapshot, MarketRegime, evaluate_market_regime
from apex3.strategy.trail import (
    compute_initial_stop_and_target,
    evaluate_fee_protected_stop,
    floor_to_tick,
    ceil_to_tick,
)

IST = ZoneInfo("Asia/Kolkata")

# Universe of candidates (Exact Elite 20 Universe + Benchmark)
SYMBOLS = [
    "^NSEI",
    "RELIANCE.NS", "HDFCBANK.NS", "ICICIBANK.NS", "SBIN.NS", "AXISBANK.NS",
    "KOTAKBANK.NS", "BAJFINANCE.NS", "INFY.NS", "TCS.NS", "MARUTI.NS",
    "M&M.NS", "TATAMOTORS.NS", "TATASTEEL.NS", "HINDALCO.NS", "JSWSTEEL.NS",
    "LT.NS", "BHARTIARTL.NS", "BEL.NS", "HAL.NS", "SUNPHARMA.NS", "ITC.NS", "TRENT.NS"
]

def fetch_data():
    raw_data = {}
    print("Downloading 5-minute candles for yesterday (2026-10-21)...")
    for s in SYMBOLS:
        clean = s.replace("^", "").replace(".NS", "")
        df = yf.download(s, period="5d", interval="5m", progress=False)
        if not df.empty:
            if hasattr(df.columns, 'levels') and len(df.columns.levels) > 1:
                df.columns = df.columns.get_level_values(0)
            df.index = pd.to_datetime(df.index)
            raw_data[clean] = df
    return raw_data

def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high = df['High']
    low = df['Low']
    close = df['Close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    return tr.rolling(period).mean()

def compute_adx(df: pd.DataFrame, period: int = 14):
    high = df['High']
    low = df['Low']
    close = df['Close']
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    tr_smooth = tr.rolling(period).sum()
    plus_di = 100 * (pd.Series(plus_dm, index=df.index).rolling(period).sum() / tr_smooth)
    minus_di = 100 * (pd.Series(minus_dm, index=df.index).rolling(period).sum() / tr_smooth)
    
    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    adx = dx.rolling(period).mean()
    return adx

def simulate_day(target_date_str="2026-10-21"):
    data = fetch_data()
    nifty_df = data.get("NSEI")
    if nifty_df is None or nifty_df.empty:
        print("Error: Could not download NIFTY index data.")
        return

    # Filter for target date
    nifty_day = nifty_df[nifty_df.index.strftime('%Y-%m-%d') == target_date_str]
    if nifty_day.empty:
        # Check available dates
        avail_dates = nifty_df.index.strftime('%Y-%m-%d').unique().tolist()
        print(f"Target date {target_date_str} not in data. Available dates: {avail_dates}")
        if avail_dates:
            target_date_str = avail_dates[-1]
            print(f"Using latest available trading session: {target_date_str}")
            nifty_day = nifty_df[nifty_df.index.strftime('%Y-%m-%d') == target_date_str]

    print(f"\n=======================================================")
    print(f"  SIMULATING APEX-3 SYSTEM FOR SESSION: {target_date_str}")
    print(f"=======================================================")

    starting_capital = 15000.0
    slot_budget = 28500.0  # 4x MIS leverage on Rs. 15,000 (Rs. 57,000 max exposure / 2 slots)
    max_risk_per_trade = 120.0  # 0.80% risk cap on Rs. 15,000
    
    # 1. Opening Range (09:15 - 09:30)
    or_start = time(9, 15)
    or_end = time(9, 30)
    entry_cutoff = time(13, 0)
    square_off = time(14, 55)

    # Pre-calculate indicators
    prep_data = {}
    for sym, df in data.items():
        if sym == "NSEI":
            continue
        df_copy = df.copy()
        df_copy['ATR'] = compute_atr(df_copy, 14)
        df_copy['ADX'] = compute_adx(df_copy, 14)
        df_copy['Vol_Avg'] = df_copy['Volume'].rolling(20).mean()
        
        # Intra-day cumulative VWAP
        df_copy['Date'] = df_copy.index.date
        df_copy['Typical'] = (df_copy['High'] + df_copy['Low'] + df_copy['Close']) / 3.0
        df_copy['PV'] = df_copy['Typical'] * df_copy['Volume']
        df_copy['Cum_PV'] = df_copy.groupby('Date')['PV'].cumsum()
        df_copy['Cum_Vol'] = df_copy.groupby('Date')['Volume'].cumsum()
        df_copy['VWAP'] = df_copy['Cum_PV'] / df_copy['Cum_Vol'].replace(0, np.nan)
        
        day_bars = df_copy[df_copy.index.strftime('%Y-%m-%d') == target_date_str]
        if not day_bars.empty:
            prep_data[sym] = (df_copy, day_bars)

    # Extract NIFTY opening range & regime
    nifty_or_bars = nifty_day[(nifty_day.index.time >= or_start) & (nifty_day.index.time < or_end)]
    if nifty_or_bars.empty:
        print("No Opening Range bars found for NIFTY.")
        return

    nifty_or_high = float(nifty_or_bars['High'].max())
    nifty_or_low = float(nifty_or_bars['Low'].min())
    print(f"NIFTY Opening Range: High = {nifty_or_high:,.2f} | Low = {nifty_or_low:,.2f}")

    # Track open positions & trades
    open_positions = {}
    completed_trades = []
    
    # Iterate through each 5m bar of the session
    bar_times = [t for t in nifty_day.index if t.time() >= or_end]

    for current_dt in bar_times:
        current_time = current_dt.time()

        # Check existing positions for Target, Stop, Fee-Lock, or Square-Off
        closed_syms = []
        for sym, pos in open_positions.items():
            sym_df, day_bars = prep_data[sym]
            if current_dt not in day_bars.index:
                continue
            bar = day_bars.loc[current_dt]
            high_p = float(bar['High'])
            low_p = float(bar['Low'])
            close_p = float(bar['Close'])

            # Update best favorable excursion
            if pos['side'] == "BUY":
                if high_p > pos['best_favorable']:
                    pos['best_favorable'] = high_p
                current_r = (pos['best_favorable'] - pos['entry_price']) / pos['r_rupees'] if pos['r_rupees'] > 0 else 0
            else:
                if low_p < pos['best_favorable']:
                    pos['best_favorable'] = low_p
                current_r = (pos['entry_price'] - pos['best_favorable']) / pos['r_rupees'] if pos['r_rupees'] > 0 else 0

            # Evaluate Fee-Protected Breakeven Step
            # Triggers at +1.65R OR at 13:45 IST if >= +0.75R
            if not pos['fee_locked']:
                if current_r >= 1.65 or (current_time >= time(13, 45) and current_r >= 0.75):
                    pos['fee_locked'] = True
                    if pos['side'] == "BUY":
                        pos['current_stop'] = ceil_to_tick(pos['entry_price'] * 1.0015)
                    else:
                        pos['current_stop'] = floor_to_tick(pos['entry_price'] * 0.9985)
                    print(f"  [{current_time.strftime('%H:%M')}] {sym} FEE-LOCK ACTIVATED at +{current_r:.2f}R -> SL moved to Rs. {pos['current_stop']:.2f}")

            # Check Target Exit (Fixed 2.0R)
            is_closed = False
            exit_price = 0.0
            exit_reason = ""

            if pos['side'] == "BUY":
                if high_p >= pos['target_price']:
                    exit_price = pos['target_price']
                    exit_reason = "2.0R TARGET HIT"
                    is_closed = True
                elif low_p <= pos['current_stop']:
                    exit_price = pos['current_stop']
                    exit_reason = "STOP LOSS HIT" if not pos['fee_locked'] else "FEE-LOCK EXIT"
                    is_closed = True
            else:
                if low_p <= pos['target_price']:
                    exit_price = pos['target_price']
                    exit_reason = "2.0R TARGET HIT"
                    is_closed = True
                elif high_p >= pos['current_stop']:
                    exit_price = pos['current_stop']
                    exit_reason = "STOP LOSS HIT" if not pos['fee_locked'] else "FEE-LOCK EXIT"
                    is_closed = True

            # EOD Square-Off at 14:55 IST
            if not is_closed and current_time >= square_off:
                exit_price = close_p
                exit_reason = "14:55 EOD SQUARE-OFF"
                is_closed = True

            if is_closed:
                qty = pos['quantity']
                costs = CostsConfig()
                if pos['side'] == "BUY":
                    gross_pnl = (exit_price - pos['entry_price']) * qty
                    charges = calculate_charges(pos['entry_price'], exit_price, qty, costs)
                else:
                    gross_pnl = (pos['entry_price'] - exit_price) * qty
                    charges = calculate_charges(exit_price, pos['entry_price'], qty, costs)

                net_pnl = gross_pnl - charges['total_charges']

                completed_trades.append({
                    'symbol': sym,
                    'side': pos['side'],
                    'entry_time': pos['entry_time'].strftime('%H:%M'),
                    'exit_time': current_time.strftime('%H:%M'),
                    'entry_price': pos['entry_price'],
                    'exit_price': exit_price,
                    'quantity': qty,
                    'initial_stop': pos['initial_stop'],
                    'target_price': pos['target_price'],
                    'exit_reason': exit_reason,
                    'gross_pnl': round(gross_pnl, 2),
                    'brokerage': round(charges['brokerage'], 2),
                    'stt': round(charges['stt'], 2),
                    'exchange_txn': round(charges['exchange_txn'], 2),
                    'gst': round(charges['gst'], 2),
                    'sebi_charges': round(charges['sebi'], 2),
                    'stamp_duty': round(charges['stamp_duty'], 2),
                    'total_charges': round(charges['total_charges'], 2),
                    'net_pnl': round(net_pnl, 2),
                })
                closed_syms.append(sym)

        for sym in closed_syms:
            del open_positions[sym]

        # Can we enter new positions? (Between 09:30 and 13:00, max 2 active slots)
        if current_time <= entry_cutoff and len(open_positions) < 2:
            # Evaluate candidates for breakout
            nifty_bar = nifty_day.loc[current_dt]
            nifty_close = float(nifty_bar['Close'])

            # Determine NIFTY Market Regime
            if nifty_close > nifty_or_high:
                regime = MarketRegime.BULLISH
            elif nifty_close < nifty_or_low:
                regime = MarketRegime.BEARISH
            else:
                regime = MarketRegime.NEUTRAL

            candidates = []

            for sym, (sym_df, day_bars) in prep_data.items():
                if sym in open_positions:
                    continue
                if current_dt not in day_bars.index:
                    continue

                bar = day_bars.loc[current_dt]
                close_p = float(bar['Close'])
                high_p = float(bar['High'])
                low_p = float(bar['Low'])
                volume = int(bar['Volume'])
                vwap = float(bar['VWAP'])
                adx = float(bar['ADX']) if not np.isnan(bar['ADX']) else 25.0
                atr = float(bar['ATR']) if not np.isnan(bar['ATR']) else (close_p * 0.005)
                vol_avg = float(bar['Vol_Avg']) if not np.isnan(bar['Vol_Avg']) else volume

                # Stock Opening Range (09:15 - 09:30)
                or_bars = day_bars[(day_bars.index.time >= or_start) & (day_bars.index.time < or_end)]
                if or_bars.empty:
                    continue
                stock_or_high = float(or_bars['High'].max())
                stock_or_low = float(or_bars['Low'].min())

                # Check breakout signal
                signal_side = None
                if close_p > stock_or_high and regime in (MarketRegime.BULLISH, MarketRegime.NEUTRAL):
                    # Long Breakout
                    if close_p > vwap and adx >= 22.0:
                        signal_side = "BUY"
                elif close_p < stock_or_low and regime in (MarketRegime.BEARISH, MarketRegime.NEUTRAL):
                    # Short Breakout
                    if close_p < vwap and adx >= 22.0:
                        signal_side = "SELL"

                if not signal_side:
                    continue

                # Gate 5: Volume Surge >= 2.0x
                vol_ratio = volume / vol_avg if vol_avg > 0 else 1.0
                if vol_ratio < 2.0:
                    continue

                # Gate 9: Stop Width & Structural Stop
                if signal_side == "BUY":
                    swing_low = float(day_bars.loc[:current_dt].tail(5)['Low'].min())
                    initial_sl = floor_to_tick(swing_low - 1.5 * atr)
                    r_dist = close_p - initial_sl
                    r_pct = r_dist / close_p
                    target = ceil_to_tick(close_p + 2.0 * r_dist)
                else:
                    swing_high = float(day_bars.loc[:current_dt].tail(5)['High'].max())
                    initial_sl = ceil_to_tick(swing_high + 1.5 * atr)
                    r_dist = initial_sl - close_p
                    r_pct = r_dist / close_p
                    target = floor_to_tick(close_p - 2.0 * r_dist)

                # Gate 9A & 9B: 0.45% <= R% <= 0.85%
                if not (0.0045 <= r_pct <= 0.0085):
                    continue

                # Relative Strength vs NIFTY
                stock_return = (close_p - float(or_bars['Close'].iloc[0])) / float(or_bars['Close'].iloc[0])
                nifty_return = (nifty_close - float(nifty_or_bars['Close'].iloc[0])) / float(nifty_or_bars['Close'].iloc[0])
                rs = stock_return - nifty_return

                # Alignment check
                if signal_side == "BUY" and rs <= 0:
                    continue
                if signal_side == "SELL" and rs >= 0:
                    continue

                # Conviction score: |RS| * 100 * Vol_Ratio
                conviction = abs(rs) * 100.0 * vol_ratio

                candidates.append({
                    'symbol': sym,
                    'side': signal_side,
                    'entry_price': close_p,
                    'initial_stop': initial_sl,
                    'target_price': target,
                    'atr': atr,
                    'r_dist': r_dist,
                    'conviction': conviction,
                    'vol_ratio': vol_ratio,
                })

            # Sort by conviction score
            candidates.sort(key=lambda x: x['conviction'], reverse=True)

            # Enter top candidates up to available slots
            for cand in candidates:
                if len(open_positions) >= 2:
                    break

                # Second-slot profit condition: If 1 slot is already open, it must be >= +0.50R
                if len(open_positions) == 1:
                    existing_pos = list(open_positions.values())[0]
                    if existing_pos['side'] == "BUY":
                        cur_r = (existing_pos['best_favorable'] - existing_pos['entry_price']) / existing_pos['r_rupees']
                    else:
                        cur_r = (existing_pos['entry_price'] - existing_pos['best_favorable']) / existing_pos['r_rupees']
                    if cur_r < 0.50:
                        continue  # Protect capital, don't open second slot yet

                # Sizing: Rs. 28,500 slot budget, max Rs. 120 risk
                risk_qty = math.floor(max_risk_per_trade / cand['r_dist'])
                margin_qty = math.floor(slot_budget / cand['entry_price'])
                final_qty = max(1, min(risk_qty, margin_qty))

                open_positions[cand['symbol']] = {
                    'symbol': cand['symbol'],
                    'side': cand['side'],
                    'entry_time': current_dt,
                    'entry_price': cand['entry_price'],
                    'initial_stop': cand['initial_stop'],
                    'current_stop': cand['initial_stop'],
                    'target_price': cand['target_price'],
                    'quantity': final_qty,
                    'atr': cand['atr'],
                    'r_rupees': cand['r_dist'],
                    'best_favorable': cand['entry_price'],
                    'fee_locked': False,
                }
                print(f"  [{current_dt.strftime('%H:%M')}] ENTRY: {cand['side']} {cand['symbol']} @ Rs. {cand['entry_price']:.2f} | Qty: {final_qty} | SL: {cand['initial_stop']:.2f} | 2R Target: {cand['target_price']:.2f} | Conviction: {cand['conviction']:.2f}")

    print("\n=======================================================")
    print("                SESSION TRADES COMPLETED                ")
    print("=======================================================")
    
    if not completed_trades:
        print("No trades triggered yesterday (Markets either ranged inside Opening Range or signals were filtered by Gate 9 / Volume).")
        return

    total_gross = sum(t['gross_pnl'] for t in completed_trades)
    total_charges = sum(t['total_charges'] for t in completed_trades)
    total_net = sum(t['net_pnl'] for t in completed_trades)

    trades_df = pd.DataFrame(completed_trades)
    print(trades_df[['symbol', 'side', 'entry_time', 'exit_time', 'entry_price', 'exit_price', 'quantity', 'exit_reason', 'gross_pnl', 'total_charges', 'net_pnl']].to_string(index=False))

    print("\n-------------------------------------------------------")
    print(f"Total Trades: {len(completed_trades)}")
    print(f"Gross P&L: Rs. {total_gross:+,.2f}")
    print(f"Zerodha + Statutory Charges: Rs. {total_charges:,.2f}")
    print(f"Net Realized P&L: Rs. {total_net:+,.2f} ({(total_net / starting_capital) * 100:+.2f}%)")
    print("-------------------------------------------------------")

if __name__ == "__main__":
    simulate_day()
