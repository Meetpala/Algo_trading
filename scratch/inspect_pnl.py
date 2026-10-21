import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_run import run_simulation

trades = run_simulation(2.0, 57)
print(f"Total trades: {len(trades)}")
for t in trades:
    print(f"{t['date']} {t['symbol']:10} {t['side']:5} qty:{t['qty']:3} entry:{t['entry_price']:7.2f} exit:{t['exit_price']:7.2f} gross:{t['gross_pnl']:+7.2f} chg:{t['total_charges']:5.2f} net:{t['net_pnl']:+7.2f} r:{t['r_multiple']:+5.2f} reason:{t['exit_reason']}")
