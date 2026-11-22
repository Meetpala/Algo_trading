from apex3.config import load_config
from backtest_3months import load_preparsed_data, build_active_radar, run_fast_backtest
import pandas as pd
import json

cfg = load_config()
w = build_active_radar(cfg.raw_dict)
data = load_preparsed_data(['NSEI'] + w)
dates_1y = sorted(set(data['NSEI']['df']['Datetime'].dt.date))

res_1y = run_fast_backtest(data, w, session_dates=dates_1y, starting_capital=25000.0)

print("--- APEX-3 FULL 1-YEAR (264 SESSIONS) AT RS. 25,000 & 12:00 CUTOFF ---")
print(f"Starting Capital: Rs. {res_1y['starting_capital']:,.2f}")
print(f"Total Completed Trades: {res_1y['total_trades']}")
print(f"Winning Trades: {res_1y['winning_trades']} | Losing Trades: {res_1y['losing_trades']}")
print(f"Win Rate: {res_1y['win_rate']:.2f}%")
print(f"Profit Factor: {res_1y['profit_factor']:.2f}")
print(f"Gross P&L: Rs. {res_1y['gross_pnl']:+,.2f}")
print(f"Total Charges: Rs. {res_1y['total_charges']:,.2f}")
print(f"Net Realized P&L: Rs. {res_1y['net_pnl']:+,.2f} ({res_1y['profit_pct']:+.2f}%)")
print(f"Friction / Gross Winning Profit: {res_1y['friction_ratio_pct']:.2f}%")

df_trades = pd.DataFrame(res_1y['trades'])
if not df_trades.empty:
    df_trades['date_dt'] = pd.to_datetime(df_trades['date'])
    monthly = []
    for period, m_trades in df_trades.groupby(df_trades['date_dt'].dt.to_period('M')):
        w_cnt = len(m_trades[m_trades['net_pnl'] > 0])
        tot = len(m_trades)
        g = round(m_trades['gross_pnl'].sum(), 2)
        c = round(m_trades['total_charges'].sum(), 2)
        n = round(g - c, 2)
        monthly.append({
            'Month': str(period),
            'Trades': tot,
            'Win Rate': f"{round(w_cnt / tot * 100, 1)}%",
            'Gross': g,
            'Charges': c,
            'Net': n,
            'ROI (%)': f"{round((n / 25000.0) * 100, 2)}%"
        })
    print("\nMONTHLY BREAKDOWN:")
    print(pd.DataFrame(monthly).to_string(index=False))

with open("full_1year_trades_25k.json", "w") as f:
    json.dump(res_1y['trades'], f, indent=2)
