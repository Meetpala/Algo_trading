import sys
sys.path.insert(0, '.')
from scratch.orig_1dacd9b import run_fast_backtest, load_preparsed_data
watchlist = ['HINDALCO', 'JSWSTEEL', 'RELIANCE', 'MARUTI', 'ITC', 'TCS', 'SBIN', 'TRENT', 'KOTAKBANK', 'SUNPHARMA']
data = load_preparsed_data(['NSEI'] + watchlist)
res = run_fast_backtest(data, watchlist)
print("1dacd9b 3-Month Results:")
print(f"Trades: {res['total_trades']} Wins: {res['winning_trades']} Net: Rs. {res['net_pnl']} Gross: Rs. {res['gross_pnl']}")
import pandas as pd
df = pd.DataFrame(res['trades'])
print(df['exit_reason'].value_counts())
print("\nSamples of TARGET_HIT:")
print(df[df['exit_reason'].str.contains('TARGET')][['date', 'symbol', 'side', 'entry_price', 'exit_price', 'r_multiple', 'gross_pnl', 'net_pnl']].head(10).to_string())
