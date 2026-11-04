"""Execute full 1-year APEX-3 historical backtest across ALL 50 NIFTY 50 constituents.

Evaluates performance under exact production quantitative invariants:
- Base capital: Rs. 15,000.00
- MIS Leverage: 4.0x (Rs. 57,000 buying power, 2 slots concurrent, Rs. 28,500/trade)
- Risk per trade cap: 0.80% (Rs. 120 max loss)
- Daily loss limit: -1.60% (-Rs. 240)
- Fixed 2.0R limit targets
- Structural swing stop anchor (1.5x ATR)
- Fee lock at +1.60R (locks +0.60R) and 14:15 IST (+1.15R locks +0.35R)
- Conservative next-bar execution with 4 bps spread
- EOD square-off at 14:55 IST
- Exact Zerodha brokerage, STT, NSE txn, SEBI, GST (18%), and stamp duty
"""
import os
import sys
import json
from datetime import time
from pathlib import Path
import pandas as pd

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from backtest_3months import load_preparsed_data, run_fast_backtest

NIFTY_50 = [
    'ADANIENT', 'ADANIPORTS', 'APOLLOHOSP', 'ASIANPAINT', 'AXISBANK',
    'BAJAJ-AUTO', 'BAJFINANCE', 'BAJAJFINSV', 'BPCL', 'BHARTIARTL',
    'BRITANNIA', 'CIPLA', 'COALINDIA', 'DRREDDY', 'EICHERMOT',
    'GRASIM', 'HCLTECH', 'HDFCBANK', 'HDFCLIFE', 'HEROMOTOCO',
    'HINDALCO', 'HINDUNILVR', 'ICICIBANK', 'ITC', 'INDUSINDBK',
    'INFY', 'JSWSTEEL', 'KOTAKBANK', 'LT', 'M&M',
    'MARUTI', 'NTPC', 'NESTLEIND', 'ONGC', 'POWERGRID',
    'RELIANCE', 'SBILIFE', 'SHRIRAMFIN', 'SBIN', 'SUNPHARMA',
    'TCS', 'TATACONSUM', 'TATAMOTORS', 'TATASTEEL', 'TECHM',
    'TITAN', 'TRENT', 'ULTRACEMCO', 'WIPRO', 'BEL'
]

def main():
    # Verify all NIFTY 50 CSVs exist
    available = []
    missing = []
    for s in NIFTY_50:
        p = os.path.join(REPO_ROOT, "scratch_data", f"{s}.csv")
        if os.path.exists(p) and os.path.getsize(p) > 100000:
            available.append(s)
        else:
            missing.append(s)

    print(f"Verified NIFTY 50 Data: {len(available)} / {len(NIFTY_50)} stocks available")
    if missing:
        print(f"Missing / incomplete: {', '.join(missing)}")

    all_symbols = ['NSEI'] + available
    print(f"Loading 1-year historical data for {len(all_symbols)} instruments...")
    data = load_preparsed_data(all_symbols)
    dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))
    print(f"Data loaded into memory: {len(dates)} trading sessions ({dates[0]} to {dates[-1]})\n")

    print(f"Executing APEX-3 1-Year Backtest across {len(available)} NIFTY 50 stocks...")
    res = run_fast_backtest(
        data=data,
        watchlist_symbols=available,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=2,
        session_dates=dates,
        entry_cutoff_time=time(12, 30),
    )

    print("\n" + "=" * 75)
    print("   APEX-3 1-YEAR COMPLETE NIFTY 50 UNIVERSE BACKTEST REPORT   ")
    print("=" * 75)
    print(f"Audited Universe:            {len(available)} NIFTY 50 Constituents")
    print(f"Evaluation Period:           {dates[0]} to {dates[-1]} ({len(dates)} sessions)")
    print(f"Starting Base Capital:       Rs. 15,000.00")
    print(f"Ending Portfolio Equity:     Rs. {res['ending_capital']:,.2f}")
    print(f"Net Realized P&L:            Rs. {res['net_pnl']:+,.2f} ({res['profit_pct']:+.2f}%)")
    print(f"Total Completed Trades:      {res['total_trades']}")
    print(f"Winning Trades / Losses:     {res['winning_trades']} Wins | {res['losing_trades']} Losses")
    print(f"Win Rate (Probability):      {res['win_rate']:.2f}%")
    print(f"Profit Factor:               {res['profit_factor']:.2f}")
    print(f"Gross P&L:                   Rs. {res['gross_pnl']:+,.2f}")
    print(f"Zerodha & Statutory Fees:    Rs. {res['total_charges']:,.2f}")
    print(f"Statutory Friction Ratio:    {res['friction_ratio_pct']:.2f}% (Limit: < 26.0%)")
    print("=" * 75)

    # Save to json
    clean_trades = []
    for t in res.get('trades', []):
        clean_trades.append({
            'date': str(t['date']),
            'symbol': t['symbol'],
            'side': str(t['side']),
            'qty': t['qty'],
            'entry_time': str(t['entry_time']),
            'entry_price': t['entry_price'],
            'exit_time': str(t['exit_time']),
            'exit_price': t['exit_price'],
            'exit_reason': str(t['exit_reason']),
            'gross_pnl': t['gross_pnl'],
            'total_charges': t['total_charges'],
            'net_pnl': t['net_pnl'],
            'r_multiple': t.get('r_multiple', 0.0),
        })

    out_file = os.path.join(REPO_ROOT, "nifty50_full_1year_audit.json")
    with open(out_file, 'w') as f:
        json.dump({
            'universe_count': len(available),
            'sessions_count': len(dates),
            'starting_capital': 15000.0,
            'ending_capital': res['ending_capital'],
            'net_pnl': res['net_pnl'],
            'profit_pct': res['profit_pct'],
            'total_trades': res['total_trades'],
            'winning_trades': res['winning_trades'],
            'losing_trades': res['losing_trades'],
            'win_rate': res['win_rate'],
            'profit_factor': res['profit_factor'],
            'gross_pnl': res['gross_pnl'],
            'total_charges': res['total_charges'],
            'friction_ratio_pct': res['friction_ratio_pct'],
            'charge_breakdown': res['charge_breakdown'],
            'trades': clean_trades,
        }, f, indent=2)
    print(f"[SAVED] Results saved to nifty50_full_1year_audit.json")

if __name__ == '__main__':
    main()

