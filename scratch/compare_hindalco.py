import sys
sys.path.insert(0, '.')
from scratch.test_1dacd9b import res as r_old
from scratch.test_pipeline_clean import simulate, all_dates, data
from datetime import time, date

r_new = simulate(all_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)

t_old = [t for t in r_old['trades'] if t['date'] == '2026-10-08' and t['symbol'] == 'HINDALCO'][0]
t_new = [t for t in r_new['trade_list'] if t['date'] == '2026-10-08' and t['symbol'] == 'HINDALCO'][0]

print("OLD HINDALCO on 2026-10-08:")
print(f"  Entry: {t_old['entry_price']} Exit: {t_old['exit_price']} Reason: {t_old['exit_reason']} Qty: {t_old['qty']} Net: {t_old['net_pnl']}")
print("NEW HINDALCO on 2026-10-08:")
print(f"  Entry: {t_new['entry_price']} Exit: {t_new['exit_price']} Reason: {t_new['exit_reason']} Qty: {t_new['qty']} Net: {t_new['net_pnl']}")

# Check all candles of HINDALCO on 2026-10-08
cands = [c for c in data['HINDALCO']['candles'] if c.timestamp.date() == date(2026, 10, 8)]
print(f"HINDALCO Low of the day: {min(c.low for c in cands)}")
