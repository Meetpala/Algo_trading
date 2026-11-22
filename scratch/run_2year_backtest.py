import os
import sys
import json
from datetime import time, datetime
from zoneinfo import ZoneInfo
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from apex3.config import load_config
from apex3.models import Candle
from backtest_3months import build_active_radar, run_fast_backtest

IST = ZoneInfo("Asia/Kolkata")

def load_2year_preparsed_data(symbols: list[str]) -> dict:
    data = {}
    for s in symbols:
        path = os.path.join(REPO_ROOT, 'scratch_data_2y', f'{s}.csv')
        df = pd.read_csv(path)
        df['Datetime'] = pd.to_datetime(df['Datetime'])
        df = df.sort_values('Datetime').reset_index(drop=True)

        dts = df['Datetime'].dt.to_pydatetime()
        opens = df['Open'].to_numpy(dtype=float)
        highs = df['High'].to_numpy(dtype=float)
        lows = df['Low'].to_numpy(dtype=float)
        closes = df['Close'].to_numpy(dtype=float)
        vols = df['Volume'].to_numpy(dtype=int)
        candles = [
            Candle(
                timestamp=dts[i],
                open=float(opens[i]),
                high=float(highs[i]),
                low=float(lows[i]),
                close=float(closes[i]),
                volume=int(vols[i]),
                is_complete=True,
            )
            for i in range(len(df))
        ]
        data[s] = {
            'df': df,
            'candles': candles,
            'dt_to_idx': {c.timestamp: i for i, c in enumerate(candles)},
        }
    return data

def main():
    cfg = load_config()
    watchlist = build_active_radar(cfg.raw_dict)
    all_symbols = ['NSEI'] + watchlist

    print(f"Loading 2-year dataset from scratch_data_2y/ for {len(all_symbols)} symbols...")
    data_2y = load_2year_preparsed_data(all_symbols)
    dates_2y = sorted(set(data_2y['NSEI']['df']['Datetime'].dt.date))
    print(f"Total sessions in 2-year data: {len(dates_2y)} (From {dates_2y[0]} to {dates_2y[-1]})")

    print("\nRunning Conservative 2-Year Replay with Rs. 25,000 Capital & 12:00 Cutoff...")
    res_2y = run_fast_backtest(
        data=data_2y,
        watchlist_symbols=watchlist,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=2,
        session_dates=dates_2y,
        starting_capital=25000.0,
        entry_cutoff_time=time(12, 0),
    )

    print("\n=================================================================")
    print("   APEX-3 CONSERVATIVE 2-YEAR HISTORICAL AUDIT REPORT            ")
    print("=================================================================")
    print(f"Starting Capital           : Rs. {res_2y['starting_capital']:,.2f}")
    print(f"Active Radar Universe      : {len(watchlist)} curated stocks")
    print(f"Total Completed Trades     : {res_2y['total_trades']}")
    print(f"Winning Trades             : {res_2y['winning_trades']} | Losing Trades: {res_2y['losing_trades']}")
    print(f"Win Rate (Probability)     : {res_2y['win_rate']:.2f}%")
    print(f"Profit Factor              : {res_2y['profit_factor']:.2f}")
    print(f"Gross P&L                  : Rs. {res_2y['gross_pnl']:+,.2f}")
    print(f"Zerodha & Statutory Charges: Rs. {res_2y['total_charges']:,.2f}")
    print(f"Net Realized P&L           : Rs. {res_2y['net_pnl']:+,.2f} ({res_2y['profit_pct']:+.2f}%)")
    print(f"Statutory Friction Ratio   : {res_2y['friction_ratio_pct']:.2f}% (Limit: < 26.0%)")
    print(f"Ending Capital             : Rs. {res_2y['ending_capital']:,.2f}")
    print("=================================================================\n")

    df_trades = pd.DataFrame(res_2y['trades'])
    if not df_trades.empty:
        df_trades['date_dt'] = pd.to_datetime(df_trades['date'])

        # Yearly summary
        print("YEARLY BREAKDOWN:")
        yearly = []
        for year, y_trades in df_trades.groupby(df_trades['date_dt'].dt.year):
            w = len(y_trades[y_trades['net_pnl'] > 0])
            tot = len(y_trades)
            g = round(y_trades['gross_pnl'].sum(), 2)
            c = round(y_trades['total_charges'].sum(), 2)
            n = round(g - c, 2)
            yearly.append({
                'Year': str(year),
                'Trades': tot,
                'Win Rate': f"{round(w / tot * 100, 1)}%",
                'Gross (Rs)': g,
                'Charges (Rs)': c,
                'Net (Rs)': n,
                'ROI (%)': f"{round((n / 25000.0) * 100, 2)}%"
            })
        print(pd.DataFrame(yearly).to_string(index=False))

        # Monthly summary
        print("\nMONTHLY BREAKDOWN:")
        monthly = []
        for period, m_trades in df_trades.groupby(df_trades['date_dt'].dt.to_period('M')):
            w = len(m_trades[m_trades['net_pnl'] > 0])
            tot = len(m_trades)
            g = round(m_trades['gross_pnl'].sum(), 2)
            c = round(m_trades['total_charges'].sum(), 2)
            n = round(g - c, 2)
            monthly.append({
                'Month': str(period),
                'Trades': tot,
                'Win Rate': f"{round(w / tot * 100, 1)}%",
                'Gross (Rs)': g,
                'Charges (Rs)': c,
                'Net (Rs)': n,
                'ROI (%)': f"{round((n / 25000.0) * 100, 2)}%"
            })
        print(pd.DataFrame(monthly).to_string(index=False))

        with open("full_2year_trades_25k.json", "w") as f:
            json.dump(res_2y['trades'], f, indent=2)

if __name__ == '__main__':
    main()
