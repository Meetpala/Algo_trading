import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_clean import simulate, three_month_dates, data
import pandas as pd
from datetime import time

r = simulate(three_month_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
df = pd.DataFrame(r['trade_list'])
wins = df[df['net_pnl'] > 0]

for _, t in wins.iterrows():
    sym = t['symbol']
    dt = pd.to_datetime(t['date']).date()
    s_candles = [c for c in data[sym]['candles'] if c.timestamp.date() == dt]
    entry_p = t['entry_price']
    side = t['side']
    r_dist = t['gross_pnl'] / (t['r_multiple'] * t['qty']) if t['r_multiple'] != 0 else 0
    tgt_p = entry_p + 2.0 * r_dist if side == 'BUY' else entry_p - 2.0 * r_dist
    
    # check max high / min low after entry
    post_cands = [c for c in s_candles if c.timestamp.time() >= time(9, 35) and c.timestamp.time() <= time(14, 55)]
    if side == 'BUY':
        best_p = max(c.high for c in post_cands)
        max_r = (best_p - entry_p) / r_dist if r_dist > 0 else 0
    else:
        best_p = min(c.low for c in post_cands)
        max_r = (entry_p - best_p) / r_dist if r_dist > 0 else 0
    print(f"{t['date']} {sym:10s} {side:4s} Entry:{entry_p:8.2f} R_dist:{r_dist:5.2f} Tgt:{tgt_p:8.2f} Best:{best_p:8.2f} MaxR:{max_r:4.2f}R ExitR:{t['r_multiple']:4.2f}R ExitNet:{t['net_pnl']:6.2f}")
