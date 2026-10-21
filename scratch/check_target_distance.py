import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_clean import simulate, all_dates, data
import pandas as pd
from datetime import time

r = simulate(all_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
trades = r['trade_list']
print(f"Total trades: {len(trades)}")

for i, t in enumerate(trades[:20]):
    sym = t['symbol']
    dt = pd.to_datetime(t['date']).date()
    s_candles = [c for c in data[sym]['candles'] if c.timestamp.date() == dt]
    entry_p = t['entry_price']
    side = t['side']
    r_dist = t['gross_pnl'] / (t['r_multiple'] * t['qty']) if t['r_multiple'] != 0 else 0
    tgt_p = entry_p + 2.0 * r_dist if side == 'BUY' else entry_p - 2.0 * r_dist
    
    # Check max high and min low after entry
    post_entry = [c for c in s_candles if c.timestamp.time() >= time(9, 35) and c.timestamp.time() <= time(14, 55)]
    if side == 'BUY':
        best_price = max((c.high for c in post_entry), default=entry_p)
        distance_to_target = tgt_p - entry_p
        achieved_move = best_price - entry_p
        pct_of_target_achieved = (achieved_move / distance_to_target * 100) if distance_to_target > 0 else 0
    else:
        best_price = min((c.low for c in post_entry), default=entry_p)
        distance_to_target = entry_p - tgt_p
        achieved_move = entry_p - best_price
        pct_of_target_achieved = (achieved_move / distance_to_target * 100) if distance_to_target > 0 else 0
        
    print(f"{t['date']} {sym:10s} {side:4s} Entry:{entry_p:8.2f} R:{r_dist:6.2f} Tgt:{tgt_p:8.2f} Best:{best_price:8.2f} %Tgt:{pct_of_target_achieved:5.1f}% Exit:{t['exit_reason']}")
