import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_run import run_simulation
import pandas as pd

trades = run_simulation(2.0, 122)
df = pd.DataFrame(trades)
df['date_dt'] = pd.to_datetime(df['date'])

print("\n--- 6-MONTH SYMBOL BREAKDOWN ---")
for sym, group in df.groupby('symbol'):
    wins = group[group['net_pnl'] > 0]
    wr = len(wins) / len(group) * 100
    print(f"{sym:10} | Trades:{len(group):2} | WinRate:{wr:5.1f}% | Gross:{group['gross_pnl'].sum():+8.2f} | Charges:{group['total_charges'].sum():7.2f} | Net:{group['net_pnl'].sum():+8.2f}")

print("\n--- 6-MONTH MONTHLY BREAKDOWN ---")
for month, group in df.groupby(df['date_dt'].dt.to_period('M')):
    wins = group[group['net_pnl'] > 0]
    wr = len(wins) / len(group) * 100
    print(f"{str(month):7} | Trades:{len(group):2} | WinRate:{wr:5.1f}% | Gross:{group['gross_pnl'].sum():+8.2f} | Charges:{group['total_charges'].sum():7.2f} | Net:{group['net_pnl'].sum():+8.2f}")

print("\n--- 6-MONTH SPLIT-HALF STABILITY ---")
mid_date = pd.to_datetime('2026-07-28')
first_half = df[df['date_dt'] <= mid_date]
second_half = df[df['date_dt'] > mid_date]
for label, half in [("First Half (Apr 27 - Jul 28)", first_half), ("Second Half (Jul 29 - Oct 21)", second_half)]:
    wins = half[half['net_pnl'] > 0]
    wr = len(wins) / len(half) * 100 if len(half) else 0
    print(f"{label:30} | Trades:{len(half):2} | WinRate:{wr:5.1f}% | Gross:{half['gross_pnl'].sum():+8.2f} | Charges:{half['total_charges'].sum():7.2f} | Net:{half['net_pnl'].sum():+8.2f}")
