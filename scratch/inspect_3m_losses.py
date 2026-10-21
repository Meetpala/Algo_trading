import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_clean import simulate, three_month_dates
import pandas as pd

r = simulate(three_month_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
df = pd.DataFrame(r['trade_list'])
losses = df[df['net_pnl'] < 0]
print(f"Total losses in 3M: {len(losses)}")
print(losses[['date', 'symbol', 'side', 'qty', 'entry_price', 'exit_price', 'exit_reason', 'r_multiple', 'gross_pnl', 'net_pnl']].to_string())
