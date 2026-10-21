import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_clean import simulate, three_month_dates
r = simulate(three_month_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)
for i, t in enumerate(r['trade_list']):
    print(f"{t['date']} {t['symbol']:10s} entry={t['entry_price']} entry_time={t.get('entry_time', '')} exit_reason={t['exit_reason']} R={t['r_multiple']}")
