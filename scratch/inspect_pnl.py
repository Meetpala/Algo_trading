import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_run import run_simulation

trades = run_simulation(2.0, 57)
for t in trades:
    print(f"{t['date']} {t['symbol']:10} entry_t:{t['entry_time']} exit_t:{t['exit_time']} r:{t['r_multiple']:+5.2f} gross:{t['gross_pnl']:+6.2f} net:{t['net_pnl']:+6.2f} reason:{t['exit_reason']}")
