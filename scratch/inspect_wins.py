import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_run import run_simulation

trades = run_simulation(2.0, 57)
print("\nALL 3-MONTH TRADES:")
for t in trades:
    print(f"{t['date']} {t['symbol']:10} {t['side']:5} Entry:{t['entry_time']} @ {t['entry_price']:7.2f} Exit:{t['exit_time']} @ {t['exit_price']:7.2f} R_mult:{t['r_multiple']:+5.2f} Gross:{t['gross_pnl']:+7.2f} Net:{t['net_pnl']:+7.2f} Reason:{t['exit_reason']}")
