import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_clean import simulate, all_dates, three_month_dates, first_half_dates

print("--- Testing target_r = 2.0 ---")
r20 = simulate(three_month_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
print(f"3M (2.0R): Trades={r20['trades']} WinRate={r20['win_rate']}% PF={r20['profit_factor']} Net={r20['net_pnl']} AvgWin={r20['avg_win']}")

r20_6m = simulate(all_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
print(f"6M (2.0R): Trades={r20_6m['trades']} WinRate={r20_6m['win_rate']}% PF={r20_6m['profit_factor']} Net={r20_6m['net_pnl']} AvgWin={r20_6m['avg_win']}")

print("\n--- Testing target_r = 2.25 ---")
r225 = simulate(three_month_dates, target_r=2.25, min_stop_pct=0.0055, max_stop_pct=0.0090)
print(f"3M (2.25R): Trades={r225['trades']} WinRate={r225['win_rate']}% PF={r225['profit_factor']} Net={r225['net_pnl']} AvgWin={r225['avg_win']}")

r225_6m = simulate(all_dates, target_r=2.25, min_stop_pct=0.0055, max_stop_pct=0.0090)
print(f"6M (2.25R): Trades={r225_6m['trades']} WinRate={r225_6m['win_rate']}% PF={r225_6m['profit_factor']} Net={r225_6m['net_pnl']} AvgWin={r225_6m['avg_win']}")
