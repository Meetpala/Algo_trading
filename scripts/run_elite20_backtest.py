"""Run 3-Month Backtest on the exact Elite 20 Universe.

Period: July 29, 2026 - October 19, 2026 (57 Sessions)
Strategy: APEX-3 Fixed 2.0R Target + Structural SL + Fee Lock
Capital: Rs. 15,000 (4.0x Zerodha MIS Leverage)
"""
import sys
import os
import math
import json
import pandas as pd
from datetime import datetime, time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backtest_3months import load_preparsed_data, run_fast_backtest

ELITE_20 = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "SBIN", "AXISBANK",
    "KOTAKBANK", "BAJFINANCE", "INFY", "TCS", "MARUTI",
    "M&M", "BAJAJ-AUTO", "TATASTEEL", "HINDALCO", "JSWSTEEL",
    "LT", "BHARTIARTL", "BEL", "HAL", "SUNPHARMA", "ITC", "TRENT"
]

def main():
    all_needed = ["NSEI"] + ELITE_20
    print(f"Loading 3-month historical data for {len(all_needed)} symbols...")
    data = load_preparsed_data(all_needed)
    print("Data loaded successfully!")

    print("\n--- Running 3-Month Backtest for Elite 20 Universe ---")
    res = run_fast_backtest(
        data=data,
        watchlist_symbols=ELITE_20,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        initial_atr_mult=1.5,
    )

    print("\n=======================================================")
    print("      ELITE 20 UNIVERSE: 3-MONTH PERFORMANCE REPORT     ")
    print("=======================================================")
    print(f"Starting Capital: Rs. 15,000.00")
    print(f"Total Trading Sessions: 57")
    print(f"Total Trades Taken: {res['total_trades']}")
    print(f"Winning Trades: {res['winning_trades']} | Losing Trades: {res['losing_trades']}")
    print(f"Win Rate: {res['win_rate']:.2f}%")
    print(f"Profit Factor: {res['profit_factor']:.2f}")
    print(f"Gross P&L: Rs. {res['gross_pnl']:+,.2f}")
    print(f"Total Zerodha + Statutory Charges: Rs. {res['total_charges']:,.2f}")
    print(f"Net Realized P&L: Rs. {res['net_pnl']:+,.2f} ({res['profit_pct']:+.2f}%)")
    print(f"Ending Capital: Rs. {res['ending_capital']:,.2f}")
    print("=======================================================\n")

    # Monthly Breakdown
    trades_df = pd.DataFrame(res['trades'])
    if not trades_df.empty:
        trades_df['session_date'] = pd.to_datetime(trades_df['entry_time']).dt.date
        
        # Split into Month 1, Month 2, Month 3
        # Dates: July 29 to Oct 19
        unique_dates = sorted(trades_df['session_date'].unique())
        n_dates = len(unique_dates)
        d_m1 = set(unique_dates[:n_dates // 3])
        d_m2 = set(unique_dates[n_dates // 3: 2 * n_dates // 3])
        d_m3 = set(unique_dates[2 * n_dates // 3:])

        months = [
            ("Month 1 (Early)", d_m1),
            ("Month 2 (Mid)", d_m2),
            ("Month 3 (Recent 30 Days)", d_m3),
        ]

        print("MONTHLY BREAKDOWN TABLE:")
        print(f"{'Period':<25} {'Trades':<8} {'Win Rate':<10} {'Gross (Rs)':<12} {'Charges (Rs)':<14} {'Net (Rs)':<10} {'ROI (%)':<8}")
        for label, m_dates in months:
            m_trades = trades_df[trades_df['session_date'].isin(m_dates)]
            t_cnt = len(m_trades)
            if t_cnt > 0:
                wins = len(m_trades[m_trades['net_pnl'] > 0])
                wr = (wins / t_cnt) * 100.0
                gross = m_trades['gross_pnl'].sum()
                charges = sum(t['charges']['total_charges'] for _, t in m_trades.iterrows())
                net = gross - charges
                roi = (net / 15000.0) * 100.0
                print(f"{label:<25} {t_cnt:<8} {wr:.2f}%     Rs. {gross:>8.2f}  Rs. {charges:>9.2f}    Rs. {net:>7.2f}  {roi:>+5.2f}%")
        print(f"{'Full 3 Months Total':<25} {res['total_trades']:<8} {res['win_rate']:.2f}%     Rs. {res['gross_pnl']:>8.2f}  Rs. {res['total_charges']:>9.2f}    Rs. {res['net_pnl']:>7.2f}  {res['profit_pct']:>+5.2f}%")

if __name__ == "__main__":
    main()
