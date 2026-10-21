import sys
sys.path.insert(0, '.')
from scratch.test_complete_simulation import run_simulation
from backtest_3months import load_preparsed_data
from apex3.config import load_config
import json

data = load_preparsed_data(['NSEI'])
dates = sorted(list(set(data['NSEI']['df']['Datetime'].dt.date)))
res = run_simulation(dates[-57:], '3M', 2.0)
for t in res['trades']:
    print(f"{t['date']} {t['symbol']:10} {t['side']:5} entry:{t['entry_time']} exit:{t['exit_time']} gross:{t['gross_pnl']:+6.2f} net:{t['net_pnl']:+6.2f} r:{t['r_multiple']:+5.2f} reason:{t['exit_reason']}")
