import sys
sys.path.insert(0, '.')
from scratch.test_pipeline_clean import simulate, all_dates, data
import pandas as pd
from datetime import time

# Let's inspect max R reached during each trade
res = simulate(all_dates)
trades = res['trade_list']

print(f"Total trades: {len(trades)}")
mfes = []
for t in trades:
    sym = t['symbol']
    dt = pd.to_datetime(t['date']).date()
    # Find all candles between entry and exit
    s_candles = data[sym]['candles']
    s_df = data[sym]['df']
    sess_candles = [c for c in s_candles if c.timestamp.date() == dt]
    
    entry_p = t['entry_price']
    side = t['side']
    qty = t['qty']
    r_rupees = t['gross_pnl'] / (t['r_multiple'] * qty) if t['r_multiple'] != 0 else 0
    
    # Calculate MFE
    max_fav_price = entry_p
    for c in sess_candles:
        if c.timestamp.time() >= time(9, 35) and c.timestamp.time() <= time(14, 55):
            if side == "BUY":
                if c.high > max_fav_price: max_fav_price = c.high
            else:
                if c.low < max_fav_price: max_fav_price = c.low
    mfe_r = abs(max_fav_price - entry_p) / r_rupees if r_rupees > 0 else 0
    mfes.append((t['date'], sym, side, entry_p, t['exit_price'], t['exit_reason'], t['r_multiple'], round(mfe_r, 2), round(r_rupees, 2)))

mfe_df = pd.DataFrame(mfes, columns=['date', 'symbol', 'side', 'entry', 'exit', 'reason', 'realized_r', 'mfe_r', 'r_dist'])
print(mfe_df.describe())
print("\nTrades with MFE >= 1.5R:")
print(mfe_df[mfe_df['mfe_r'] >= 1.5].to_string())
print("\nTrades with MFE >= 2.0R:")
print(mfe_df[mfe_df['mfe_r'] >= 2.0].to_string())
