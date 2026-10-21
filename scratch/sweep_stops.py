import os, sys
sys.path.insert(0, os.getcwd())
import scratch.test_relaxation as tr

print("=================================================")
print("SWEEPING max_stop_pct ON 3-MONTH AND 6-MONTH DATA")
print("=================================================")

for max_s in [0.0090, 0.0110, 0.0130, 0.0150, 0.0180, 0.0250]:
    print(f"\n>>> TESTING max_stop_distance_pct = {max_s:.4f} ({max_s*100:.2f}%) <<<")
    print("--- 3 Months (57 sessions) ---")
    res_3m = tr.run_test_sim(target_r=2.0, min_stop_pct=0.0055, max_stop_pct=max_s, dates=tr.three_month_dates)
    
    all_dates = sorted(set(tr.data['NSEI']['df']['Datetime'].dt.date))
    print("--- 6 Months (122 sessions) ---")
    res_6m = tr.run_test_sim(target_r=2.0, min_stop_pct=0.0055, max_stop_pct=max_s, dates=all_dates)
