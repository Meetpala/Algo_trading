"""APEX-3 6-Month Backtest Engine for Deployed Elite 10 Strategy.

Executes the exact deployed StrategyEngine rules across all historical sessions:
- Universe: Audited Elite 10 Institutional Trend Leaders
- Capital: Rs. 15,000.00 base, 4x MIS leverage (Rs. 28,500/slot budget)
- Invariants: 2 max slots, Slot-2 locked until Slot-1 >= +0.50R, max 1 entry/bar
- Risk: Rs. 120.00 max risk (0.80%), -Rs. 240.00 daily stop (1.60%)
- Stops & Targets: 1.5x ATR structural swing anchor, Fixed 2.0R target
- Fee Lock: Entry +/- 0.15% at +1.65R or 13:45 IST >= +0.75R
- Statutory Friction: Exact Zerodha brokerage + STT + NSE 0.00318% + SEBI + Stamp + GST
"""
import os
import sys
import json
from datetime import datetime, time
import pandas as pd
import numpy as np

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backtest_3months import load_preparsed_data, run_fast_backtest
from apex3.config import load_config

ELITE_10 = [
    "HINDALCO",
    "JSWSTEEL",
    "RELIANCE",
    "MARUTI",
    "ITC",
    "TCS",
    "SBIN",
    "TRENT",
    "KOTAKBANK",
    "SUNPHARMA",
]


def run_6month_audit():
    print("=" * 70)
    print("   APEX-3 DEPLOYED STRATEGY PERFORMANCE AUDIT (ELITE 10)")
    print("=" * 70)

    symbols_needed = ["NSEI"] + ELITE_10
    for s in symbols_needed:
        p = os.path.join("scratch_data", f"{s}.csv")
        if not os.path.exists(p):
            print(f"[ERROR] Historical data file missing: {p}")
            print("Run 'python scripts/fetch_angel_history.py' to download candles.")
            return

    print("Loading historical candles into memory...")
    data = load_preparsed_data(symbols_needed)

    # Date range info
    nifty_df = data["NSEI"]["df"]
    min_date = nifty_df["Datetime"].min()
    max_date = nifty_df["Datetime"].max()
    unique_days = nifty_df["Datetime"].dt.date.nunique()
    print(f"Data Span: {min_date.strftime('%d-%b-%Y')} to {max_date.strftime('%d-%b-%Y')} ({unique_days} trading sessions)")

    # Execute simulation
    result = run_fast_backtest(
        data=data,
        watchlist_symbols=ELITE_10,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        starting_capital=15000.0,
    )

    trades = result["trades"]
    df_trades = pd.DataFrame(trades)

    print("\n" + "=" * 70)
    print(f"   OVERALL STRATEGY PERFORMANCE ({unique_days} SESSIONS)")
    print("=" * 70)
    print(f"Starting Capital:            Rs. 15,000.00")
    print(f"Total Completed Trades:      {result['total_trades']}")
    print(f"Winning Trades:              {result['winning_trades']} ({result['win_rate']:.2f}%)")
    print(f"Losing Trades:               {result['losing_trades']}")
    print(f"Profit Factor:               {result['profit_factor']:.2f}")
    print(f"Gross P&L:                   Rs. +{result['gross_pnl']:.2f}")
    print(f"Zerodha & Statutory Charges: Rs. {result['total_charges']:.2f}")
    print(f"Net Realized P&L:            Rs. +{result['net_pnl']:.2f} (+{result['profit_pct']:.2f}% ROI)")
    total_volume_pnl = result['gross_profit'] + result['gross_loss']
    friction_pct = (result['total_charges'] / total_volume_pnl * 100) if total_volume_pnl > 0 else 0
    print(f"Statutory Friction Ratio:    {friction_pct:.2f}% (Limit: < 26.0%)")
    print("=" * 70)

    # Monthly breakdown
    if not df_trades.empty:
        df_trades["date_dt"] = pd.to_datetime(df_trades["date"])
        df_trades["month_year"] = df_trades["date_dt"].dt.to_period("M")
        
        monthly_summary = []
        for period, group in df_trades.groupby("month_year"):
            w = len(group[group["net_pnl"] > 0])
            tot = len(group)
            wr = w / tot * 100.0
            g = group["gross_pnl"].sum()
            c = group["total_charges"].sum()
            n = group["net_pnl"].sum()
            monthly_summary.append({
                "Month": str(period),
                "Trades": tot,
                "Win Rate": f"{wr:.1f}%",
                "Gross (Rs)": round(g, 2),
                "Charges (Rs)": round(c, 2),
                "Net (Rs)": round(n, 2),
                "Net ROI (%)": f"{round(n / 15000.0 * 100, 2)}%",
            })

        print("\nMONTH-BY-MONTH BREAKDOWN:")
        print(pd.DataFrame(monthly_summary).to_string(index=False))

        # Stock breakdown
        stock_summary = []
        for sym, group in df_trades.groupby("symbol"):
            w = len(group[group["net_pnl"] > 0])
            tot = len(group)
            wr = w / tot * 100.0
            g = group["gross_pnl"].sum()
            c = group["total_charges"].sum()
            n = group["net_pnl"].sum()
            stock_summary.append({
                "Symbol": sym,
                "Trades": tot,
                "Win Rate": f"{wr:.1f}%",
                "Gross (Rs)": round(g, 2),
                "Charges (Rs)": round(c, 2),
                "Net (Rs)": round(n, 2),
            })
        print("\nSYMBOL-BY-SYMBOL ATTRIBUTION (ELITE 10):")
        print(pd.DataFrame(stock_summary).sort_values("Net (Rs)", ascending=False).to_string(index=False))

    return result


if __name__ == "__main__":
    run_6month_audit()
