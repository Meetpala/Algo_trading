import json
from datetime import time
from apex3.config import load_config
from backtest_3months import load_preparsed_data, build_active_radar, run_fast_backtest
import pandas as pd

cfg = load_config()
watchlist = build_active_radar(cfg.raw_dict)
data = load_preparsed_data(['NSEI'] + watchlist)
dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))

res_1200 = run_fast_backtest(data, watchlist, target_r=2.0, max_slots=2, session_dates=dates, starting_capital=25000.0, entry_cutoff_time=time(12, 0))
res_1230 = run_fast_backtest(data, watchlist, target_r=2.0, max_slots=2, session_dates=dates, starting_capital=25000.0, entry_cutoff_time=time(12, 30))

t_1200_set = {(t['symbol'], t['date'], t['entry_time']) for t in res_1200['trades']}
diff_trades = [t for t in res_1230['trades'] if (t['symbol'], t['date'], t['entry_time']) not in t_1200_set]
print('Trades taken between 12:00 and 12:30:')
for t in diff_trades:
    print(f"Symbol: {t['symbol']} | Date: {t['date']} | Entry: {t['entry_time']} | Side: {t['side']} | EntryPx: {t['entry_price']} | ExitPx: {t['exit_price']} | Reason: {t['exit_reason']} | Gross: {t['gross_pnl']:.2f} | Charges: {t['total_charges']:.2f} | Net: {t['net_pnl']:.2f}")
