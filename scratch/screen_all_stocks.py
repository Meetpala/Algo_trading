import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
import pandas as pd
import math
from apex3.config import load_config
from backtest_3months import load_preparsed_data, run_fast_backtest

config = load_config()

available_symbols = sorted(
    path.stem for path in Path("scratch_data").glob("*.csv")
    if path.stem != "NSEI"
)
print(f"Available symbols ({len(available_symbols)}): {available_symbols}")

# Load all data
all_needed = ['NSEI'] + available_symbols
data = load_preparsed_data(all_needed)

all_dates = sorted(set(data["NSEI"]["df"]["Datetime"].dt.date))
three_month_dates = all_dates[-57:]
six_month_dates = all_dates

print(f"6-Month: {len(six_month_dates)} sessions ({six_month_dates[0]} to {six_month_dates[-1]})")
print(f"3-Month: {len(three_month_dates)} sessions ({three_month_dates[0]} to {three_month_dates[-1]})")

results = []

for sym in available_symbols:
    # Run 6M
    res_6m = run_fast_backtest(
        data=data,
        watchlist_symbols=[sym],
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=1,
        session_dates=six_month_dates,
    )
    # Run 3M
    res_3m = run_fast_backtest(
        data=data,
        watchlist_symbols=[sym],
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=1,
        session_dates=three_month_dates,
    )
    
    t6 = res_6m['total_trades']
    w6 = res_6m['win_rate']
    net6 = res_6m['net_pnl']
    fric6 = res_6m['friction_ratio_pct']
    
    t3 = res_3m['total_trades']
    w3 = res_3m['win_rate']
    net3 = res_3m['net_pnl']
    fric3 = res_3m['friction_ratio_pct']
    
    results.append({
        'Symbol': sym,
        '6M_Trades': t6,
        '6M_WinRate': f"{w6:.1f}%",
        '6M_NetPnL': net6,
        '6M_Friction': f"{fric6:.1f}%" if math.isfinite(fric6) else "N/A",
        '3M_Trades': t3,
        '3M_WinRate': f"{w3:.1f}%",
        '3M_NetPnL': net3,
        '3M_Friction': f"{fric3:.1f}%" if math.isfinite(fric3) else "N/A",
        'PositiveBoth': (net6 > 0 and net3 > 0),
    })

df_res = pd.DataFrame(results)
df_sorted = df_res.sort_values(by=['6M_NetPnL', '3M_NetPnL'], ascending=False)
print("\n" + "="*80)
print("ALL 32 STOCKS EXPECTANCY AUDIT (6-MONTH & 3-MONTH)")
print("="*80)
print(df_sorted.to_string(index=False))

positives = df_sorted[df_sorted['PositiveBoth'] == True]
print("\n" + "="*80)
print("STOCKS WITH STRICTLY POSITIVE NET P&L IN BOTH 6M AND 3M:")
print("="*80)
print(positives.to_string(index=False))
