import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_run import run_simulation
from backtest_3months import load_preparsed_data
from apex3.config import load_config
from apex3.strategy.engine import build_active_radar
from apex3.models import Side
from datetime import time
import pandas as pd

config = load_config()
watchlist = build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = load_preparsed_data(all_symbols)

trades = run_simulation(2.0, 122)

print("\n--- MFE (MAX FAVORABLE EXCURSION) ANALYSIS ---")
for t in trades[:25]:
    sym = t['symbol']
    side = Side.BUY if t['side'] == 'BUY' else Side.SELL
    date_str = t['date']
    df = data[sym]['df']
    sess_df = df[df['Datetime'].dt.strftime('%Y-%m-%d') == date_str]
    entry_dt = pd.to_datetime(f"{date_str} {t['entry_time']}")
    sess_df = sess_df.copy()
    sess_df['dt_naive'] = sess_df['Datetime'].dt.tz_localize(None)
    after_entry = sess_df[sess_df['dt_naive'] >= entry_dt]
    
    entry_p = t['entry_price']
    if side == Side.BUY:
        max_fav = after_entry['High'].max()
        r_dist = abs(entry_p - t['exit_price']) if t['exit_reason'] == 'STOP_LOSS' else (t['exit_price'] - entry_p) / 2.0
    else:
        max_fav = after_entry['Low'].min()
        r_dist = abs(entry_p - t['exit_price']) if t['exit_reason'] == 'STOP_LOSS' else (entry_p - t['exit_price']) / 2.0
        
    fav_dist = abs(max_fav - entry_p)
    mfe_r = fav_dist / (entry_p * 0.007)
    print(f"{date_str} {sym:10} {t['side']:5} Entry:{entry_p:7.2f} Exit:{t['exit_price']:7.2f} Net:{t['net_pnl']:+7.2f} Reason:{t['exit_reason']:16} MaxFav:{max_fav:7.2f} MFE_pts:{fav_dist:5.2f}")
