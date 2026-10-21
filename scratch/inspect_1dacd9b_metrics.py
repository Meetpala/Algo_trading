import sys
sys.path.insert(0, '.')
from scratch.test_1dacd9b import res as r_old
import pandas as pd

df = pd.DataFrame(r_old['trades'])
df['date_dt'] = pd.to_datetime(df['date'])
sub3 = df[df['date_dt'] >= '2026-07-29']
sub1 = df[df['date_dt'] < '2026-07-29']

print("=== 1dacd9b Metrics ===")
print("Full 6-Month:")
print(f"  Trades: {len(df)} | Net: {df['net_pnl'].sum():.2f} | Gross: {df['gross_pnl'].sum():.2f} | Charges: {df['total_charges'].sum():.2f}")
print("First Half:")
print(f"  Trades: {len(sub1)} | Net: {sub1['net_pnl'].sum():.2f} | Gross: {sub1['gross_pnl'].sum():.2f} | Charges: {sub1['total_charges'].sum():.2f}")
print("Second Half (3-Month):")
print(f"  Trades: {len(sub3)} | Net: {sub3['net_pnl'].sum():.2f} | Gross: {sub3['gross_pnl'].sum():.2f} | Charges: {sub3['total_charges'].sum():.2f}")

wins_3 = sub3[sub3['net_pnl'] > 0]
print(f"  Win Rate (3M): {len(wins_3)/len(sub3)*100:.2f}% | Avg Win: {wins_3['gross_pnl'].mean():.2f} | Avg Net Win: {wins_3['net_pnl'].mean():.2f}")
print("  Exit reasons in 3M:")
print(sub3['exit_reason'].value_counts())
