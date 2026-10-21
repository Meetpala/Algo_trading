import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_run import run_simulation

trades = run_simulation(2.0, 57)
wins = [t for t in trades if t['net_pnl'] > 0]

print(f"\n{'Date':10} {'Sym':10} {'Side':5} {'Entry':7} {'Stop':7} {'Target':7} {'R_pts':6} {'Qty':3} {'Risk_Rs':7} {'Exit':7} {'Reason':18} {'Gross':7} {'Net':7}")
print("-" * 115)
for t in wins:
    sym = t['symbol']
    # compute initial_stop and target from trade record if available
    r_pts = t['r_multiple']
    qty = t['qty']
    gross = t['gross_pnl']
    net = t['net_pnl']
    entry = t['entry_price']
    exit_p = t['exit_price']
    reason = t['exit_reason']
    side = t['side']
    print(f"{t['date']:10} {sym:10} {side:5} {entry:7.2f} {' ':7} {' ':7} {' ':6} {qty:3d} {' ':7} {exit_p:7.2f} {reason:18} {gross:+7.2f} {net:+7.2f}")
