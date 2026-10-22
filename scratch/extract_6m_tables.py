import sys
sys.path.insert(0, '.')
from scratch.test_clean_6m import run_clean_backtest, load_preparsed_data, build_active_radar, load_config
import pandas as pd
import json

config = load_config()
watchlist = build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = load_preparsed_data(all_symbols)
dates = sorted(list(set(data['NSEI']['df']['Datetime'].dt.date)))

res = run_clean_backtest(data, watchlist, target_r=2.0, session_dates=dates)
trades = res['trades']
df = pd.DataFrame(trades)
df['month'] = pd.to_datetime(df['date']).dt.to_period('M').astype(str)

print("\n--- MONTHLY BREAKDOWN ---")
monthly_rows = []
for m, grp in df.groupby('month'):
    wins = grp[grp['net_pnl'] > 0]
    wr = len(wins) / len(grp) * 100.0
    gp = grp['gross_pnl'].sum()
    tc = grp['total_charges'].sum()
    npnl = grp['net_pnl'].sum()
    gw = grp[grp['gross_pnl'] > 0]['gross_pnl'].sum()
    fric = (tc / gw * 100.0) if gw > 0 else 0.0
    monthly_rows.append({
        'Month': m,
        'Trades': len(grp),
        'Win Rate': f"{wr:.1f}%",
        'Gross PnL (Rs)': round(gp, 2),
        'Charges (Rs)': round(tc, 2),
        'Net PnL (Rs)': round(npnl, 2),
        'Charges / Gross Win': f"{fric:.1f}%",
    })
print(pd.DataFrame(monthly_rows).to_string(index=False))

print("\n--- SYMBOL ATTRIBUTION ---")
sym_rows = []
for s, grp in df.groupby('symbol'):
    wins = grp[grp['net_pnl'] > 0]
    wr = len(wins) / len(grp) * 100.0
    gp = grp['gross_pnl'].sum()
    tc = grp['total_charges'].sum()
    npnl = grp['net_pnl'].sum()
    sym_rows.append({
        'Symbol': s,
        'Trades': len(grp),
        'Win Rate': f"{wr:.1f}%",
        'Gross PnL (Rs)': round(gp, 2),
        'Charges (Rs)': round(tc, 2),
        'Net PnL (Rs)': round(npnl, 2),
    })
sym_df = pd.DataFrame(sym_rows).sort_values('Net PnL (Rs)', ascending=False)
print(sym_df.to_string(index=False))

# Split-half comparison
midpoint = len(dates) // 2
dates_h1 = dates[:midpoint]
dates_h2 = dates[midpoint:]

res_h1 = run_clean_backtest(data, watchlist, target_r=2.0, session_dates=dates_h1)
res_h2 = run_clean_backtest(data, watchlist, target_r=2.0, session_dates=dates_h2)

print("\n--- SPLIT HALF COMPARISON ---")
print(f"First Half (Apr 27 - Jul 24, 2026): {res_h1['total_trades']} trades | Win Rate: {res_h1['win_rate']}% | Gross: Rs. {res_h1['gross_pnl']} | Charges: Rs. {res_h1['total_charges']} | Net: Rs. {res_h1['net_pnl']}")
print(f"Second Half (Jul 27 - Oct 21, 2026): {res_h2['total_trades']} trades | Win Rate: {res_h2['win_rate']}% | Gross: Rs. {res_h2['gross_pnl']} | Charges: Rs. {res_h2['total_charges']} | Net: Rs. {res_h2['net_pnl']}")
print(f"Combined 6-Month: {res['total_trades']} trades | Win Rate: {res['win_rate']}% | Gross: Rs. {res['gross_pnl']} | Charges: Rs. {res['total_charges']} | Net: Rs. {res['net_pnl']}")
