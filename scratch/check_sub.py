import sys
sys.path.insert(0, '.')
from scratch.test_1dacd9b import res
import pandas as pd
df = pd.DataFrame(res['trades'])
df['date_dt'] = pd.to_datetime(df['date'])
sub = df[df['date_dt'] >= '2026-07-29']
print(f"Trades in 3m: {len(sub)}")
print(sub['exit_reason'].value_counts())
print(f"Gross: {sub['gross_pnl'].sum():.2f} Charges: {sub['total_charges'].sum():.2f} Net: {sub['net_pnl'].sum():.2f}")
