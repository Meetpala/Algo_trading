import os
import sys
from datetime import time
from pathlib import Path

REPO_ROOT = r"c:\Users\itsme\OneDrive\Desktop\Algo Trading"
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from backtest_3months import load_preparsed_data, run_fast_backtest
from apex3.config import load_config
from apex3.strategy.engine import build_active_radar

cfg = load_config()
active = build_active_radar(cfg.raw_dict)
symbols = ['NSEI'] + active + ['AXISBANK', 'TATAPOWER', 'ICICIBANK']
data = load_preparsed_data(symbols)
dates_6m = sorted(set(data['NSEI']['df']['Datetime'].dt.date))
dates_3m = dates_6m[-57:]

for test_name, test_syms in [
    ('Current Active Radar', active),
    ('+ AXISBANK', active + ['AXISBANK']),
    ('+ TATAPOWER', active + ['TATAPOWER']),
    ('+ AXISBANK & TATAPOWER', active + ['AXISBANK', 'TATAPOWER']),
    ('+ AXISBANK, TATAPOWER, ICICIBANK', active + ['AXISBANK', 'TATAPOWER', 'ICICIBANK']),
]:
    r6 = run_fast_backtest(data, test_syms, 2.0, True, False, 2, time(12,30), session_dates=dates_6m)
    r3 = run_fast_backtest(data, test_syms, 2.0, True, False, 2, time(12,30), session_dates=dates_3m)
    print(f"{test_name:32} | 6M: Rs. {r6['net_pnl']:+8.2f} (WR: {r6['win_rate']:4.1f}%, Fric: {r6['friction_ratio_pct']:5.2f}%) | 3M: Rs. {r3['net_pnl']:+8.2f} (WR: {r3['win_rate']:4.1f}%, Fric: {r3['friction_ratio_pct']:5.2f}%)")
