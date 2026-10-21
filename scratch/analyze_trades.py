import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_clean import simulate, all_dates
res = simulate(all_dates)
import pandas as pd
df = pd.DataFrame(res['trade_list'])
print("Total trades:", len(df))
for i, t in enumerate(res['trade_list'][:15]):
    print(f"Trade {i}: {t['date']} {t['symbol']} {t['side']} qty={t['qty']} entry={t['entry_price']} exit={t['exit_price']} reason={t['exit_reason']} R={t['r_multiple']} gross={t['gross_pnl']}")
