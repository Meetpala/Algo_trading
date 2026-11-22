import os, sys
sys.path.insert(0, os.path.abspath('.'))
import json
from collections import Counter
import pandas as pd
from backtest_3months import (
    load_config, build_active_radar, load_preparsed_data,
    evaluate_benchmark_regime, evaluate_candidate_signal,
    compute_initial_stop_and_target, size_backtest_quantity,
    calculate_target_price, Side, Candle, CandidateSignal
)

def run_diagnostics():
    config = load_config()
    watchlist = build_active_radar(config.raw_dict)
    all_symbols = ['NSEI'] + watchlist
    preloaded = load_preparsed_data(all_symbols)
    
    nifty = preloaded['NSEI']
    nifty_df = nifty['df']
    all_dates = sorted(set(nifty_df['Datetime'].dt.date))
    
    print(f"Analyzing {len(all_dates)} sessions across {len(watchlist)} stocks...")
    
    regime_counter = Counter()
    total_bars_in_window = 0
    raw_breakouts = 0
    gate_failures = Counter()
    gate_sole_failures = Counter()
    qualified_signals = 0
    
    adr14_map = {}
    for sym in watchlist:
        c_list = preloaded[sym]['candles']
        if len(c_list) >= 14 * 75:
            day_ranges = []
            for d in all_dates[-14:]:
                d_bars = [c for c in c_list if c.timestamp.date() == d]
                if d_bars:
                    dh = max(c.high for c in d_bars)
                    dl = min(c.low for c in d_bars)
                    dc = d_bars[0].open
                    if dc > 0:
                        day_ranges.append((dh - dl) / dc)
            adr14_map[sym] = max(0.025, sum(day_ranges) / len(day_ranges) if day_ranges else 0.025)
        else:
            adr14_map[sym] = 0.025

    for session_date in all_dates:
        n_bars = [c for c in nifty['candles'] if c.timestamp.date() == session_date]
        if len(n_bars) < 73:
            continue
            
        # Check regime throughout the day
        for s_idx in range(4, 40): # 09:35 to 12:30
            n_bar = n_bars[s_idx]
            reg_eval = evaluate_benchmark_regime(
                s_idx, n_bars, nifty['ema20'], nifty['ema50'], nifty['adx14'],
                config.raw_dict
            )
            regime_counter[reg_eval.regime.name] += 1
            
            for sym in watchlist:
                total_bars_in_window += 1
                s_bars = [c for c in preloaded[sym]['candles'] if c.timestamp.date() == session_date]
                if len(s_bars) < 73 or s_idx >= len(s_bars):
                    continue
                s_bar = s_bars[s_idx]
                
                # Check OR
                or_bars = s_bars[:3]
                or_high = max(c.high for c in or_bars)
                or_low = min(c.low for c in or_bars)
                first_open = or_bars[0].open
                or_width = (or_high - or_low) / first_open if first_open > 0 else 0.0
                
                is_long_breakout = s_bar.close > or_high
                is_short_breakout = s_bar.close < or_low
                
                if not (is_long_breakout or is_short_breakout):
                    continue
                    
                raw_breakouts += 1
                
                # Test individual gates
                failed_gates = []
                
                # Regime gate
                if reg_eval.regime.name not in ('BULLISH', 'BEARISH'):
                    failed_gates.append('Regime_Neutral_Chop')
                elif is_long_breakout and reg_eval.regime.name != 'BULLISH':
                    failed_gates.append('Regime_Mismatch_LongInBear')
                elif is_short_breakout and reg_eval.regime.name != 'BEARISH':
                    failed_gates.append('Regime_Mismatch_ShortInBull')
                    
                # Gate 1 / 4A: OR width
                if or_width > 0.0125:
                    failed_gates.append('Gate4A_OR_Width_Too_Wide')
                    
                # Gate 5: Volume Surge
                v_slice = s_bars[max(0, s_idx-20):s_idx]
                avg_vol = sum(c.volume for c in v_slice) / len(v_slice) if v_slice else 1.0
                vol_ratio = s_bar.volume / avg_vol if avg_vol > 0 else 0.0
                if vol_ratio < 1.70:
                    failed_gates.append('Gate5_Volume_Surge_Low')
                    
                # Gate 8: CLV and Cushion
                bar_range = s_bar.high - s_bar.low
                atr_val = preloaded[sym]['atr14'][s_idx]
                if bar_range > 0:
                    clv = (s_bar.close - s_bar.low) / bar_range if is_long_breakout else (s_bar.high - s_bar.close) / bar_range
                else:
                    clv = 0.5
                cushion = (s_bar.close - or_high) if is_long_breakout else (or_low - s_bar.close)
                if clv < 0.65:
                    failed_gates.append('Gate8_CLV_Weak')
                if cushion < 0.15 * atr_val:
                    failed_gates.append('Gate8_Cushion_Weak')
                    
                # Gate 9A: ATR Volatility Floor
                if atr_val / s_bar.close < 0.0022:
                    failed_gates.append('Gate9A_ATR_Too_Sluggish')
                    
                # Gate 9B: Stop bounds
                swing_bars = s_bars[max(0, s_idx-5):s_idx]
                swing_l = min(c.low for c in swing_bars) if swing_bars else s_bar.low
                swing_h = max(c.high for c in swing_bars) if swing_bars else s_bar.high
                
                side = Side.BUY if is_long_breakout else Side.SELL
                stop_and_target = compute_initial_stop_and_target(
                    side=side,
                    entry_price=s_bar.close,
                    swing_low_5=swing_l,
                    swing_high_5=swing_h,
                    atr14=atr_val,
                    cfg=config.raw_dict
                )
                r_pct = stop_and_target.risk_pct
                if r_pct < 0.0055:
                    failed_gates.append('Gate9B_Stop_Too_Tight')
                elif r_pct > 0.0090:
                    failed_gates.append('Gate9B_Stop_Too_Wide')
                    
                # Gate 9C: ADR Consumed
                session_bars_so_far = s_bars[:s_idx+1]
                s_high = max(c.high for c in session_bars_so_far)
                s_low = min(c.low for c in session_bars_so_far)
                adr_pct = adr14_map.get(sym, 0.025)
                adr_consumed = (s_high - s_low) / (first_open * adr_pct) if first_open > 0 else 0.0
                if adr_consumed > 0.60:
                    failed_gates.append('Gate9C_ADR_Exhausted')
                    
                for fg in failed_gates:
                    gate_failures[fg] += 1
                if len(failed_gates) == 1:
                    gate_sole_failures[failed_gates[0]] += 1
                elif len(failed_gates) == 0:
                    qualified_signals += 1

    print("\n=== BOTTLENECK DIAGNOSTIC RESULTS ===")
    print(f"Total Candidate Bar Evaluations: {total_bars_in_window}")
    print(f"NIFTY 50 Regime Distribution: {dict(regime_counter)}")
    print(f"Raw Breakout Bars (Close > OR High or < OR Low): {raw_breakouts}")
    print(f"Fully Qualified Signals (Passed all 9 Gates): {qualified_signals}")
    
    print("\n--- Gate Failures (Frequency across all Raw Breakouts) ---")
    for g, count in gate_failures.most_common():
        pct = (count / raw_breakouts * 100) if raw_breakouts > 0 else 0
        print(f"  {g:<30}: {count:>5d} ({pct:>5.1f}%)")
        
    print("\n--- Sole Gate Failures (Breakout passed EVERY gate EXCEPT this one) ---")
    for g, count in gate_sole_failures.most_common():
        print(f"  {g:<30}: {count:>5d} missed trades")

if __name__ == '__main__':
    run_diagnostics()
