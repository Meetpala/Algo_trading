import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_clean import simulate, all_dates, three_month_dates, first_half_dates

print("=== With max_stop_pct = 0.0090 ===")
r3 = simulate(three_month_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
print(f"3-Month: Trades: {r3['trades']} | Win Rate: {r3['win_rate']}% | PF: {r3['profit_factor']}")
print(f"Gross: Rs. {r3['gross_pnl']} | Charges: Rs. {r3['charges']} | Net: Rs. {r3['net_pnl']} | Avg Win: Rs. {r3['avg_win']}")

r1 = simulate(first_half_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
print(f"1st-Half: Trades: {r1['trades']} | Win Rate: {r1['win_rate']}% | PF: {r1['profit_factor']}")
print(f"Gross: Rs. {r1['gross_pnl']} | Charges: Rs. {r1['charges']} | Net: Rs. {r1['net_pnl']}")

r6 = simulate(all_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
print(f"6-Month: Trades: {r6['trades']} | Win Rate: {r6['win_rate']}% | PF: {r6['profit_factor']}")
print(f"Gross: Rs. {r6['gross_pnl']} | Charges: Rs. {r6['charges']} | Net: Rs. {r6['net_pnl']} | Avg Win: Rs. {r6['avg_win']}")
