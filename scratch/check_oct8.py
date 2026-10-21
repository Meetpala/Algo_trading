import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_clean import simulate, data
from datetime import date, time

r = simulate([date(2026, 10, 8)], target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
print(f"Trades on 2026-10-08 in test_pipeline_clean: {len(r['trade_list'])}")
for t in r['trade_list']:
    print(t)
