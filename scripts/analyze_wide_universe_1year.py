"""1-Year Backtest Analysis on Wide Universe (32 Large-Cap / NIFTY Heavyweights).

Evaluates the exact quantitative performance if the watchlist was expanded from
the active 11 stocks to all 32 available large-cap equities over the past 1 year (247 sessions).
"""
import os
import sys
import json
from pathlib import Path
from datetime import time
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from backtest_3months import load_preparsed_data, run_fast_backtest

def main():
    symbols = sorted([p.stem for p in Path('scratch_data').glob('*.csv') if p.stem != 'NSEI'])
    all_needed = ['NSEI'] + symbols
    print(f"Loading 1-year historical data for {len(symbols)} stocks + benchmark...")
    data = load_preparsed_data(all_needed)
    dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))
    print(f"Data loaded: {len(dates)} trading sessions ({dates[0]} to {dates[-1]})\n")

    print(f"Running full 1-year replay across all {len(symbols)} stocks with production rules...")
    res = run_fast_backtest(
        data=data,
        watchlist_symbols=symbols,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=2,
        session_dates=dates,
        entry_cutoff_time=time(12, 30),
    )

    print("\n" + "=" * 70)
    print("   APEX-3 1-YEAR WIDE WATCHLIST AUDIT (32 LARGE-CAP STOCKS)")
    print("=" * 70)
    print(f"Starting Capital:            Rs. 15,000.00")
    print(f"Ending Portfolio Equity:     Rs. {res['ending_capital']:,.2f}")
    print(f"Net Realized P&L:            Rs. {res['net_pnl']:+,.2f} ({res['profit_pct']:+.2f}%)")
    print(f"Total Completed Trades:      {res['total_trades']}")
    print(f"Winning Trades / Losses:     {res['winning_trades']} Wins | {res['losing_trades']} Losses")
    print(f"Win Rate:                    {res['win_rate']:.2f}%")
    print(f"Profit Factor:               {res['profit_factor']:.2f}")
    print(f"Gross P&L:                   Rs. {res['gross_pnl']:+,.2f}")
    print(f"Zerodha & Statutory Fees:    Rs. {res['total_charges']:,.2f}")
    print(f"Charges Ratio / Gross Win:   {res['friction_ratio_pct']:.2f}% (Limit: < 26.0%)")
    print("=" * 70)

    # Save details
    with open('wide_watchlist_1year_audit.json', 'w') as f:
        # Convert non-serializable objects
        clean_res = {
            'total_trades': res['total_trades'],
            'winning_trades': res['winning_trades'],
            'losing_trades': res['losing_trades'],
            'win_rate': res['win_rate'],
            'profit_factor': res['profit_factor'],
            'gross_pnl': res['gross_pnl'],
            'total_charges': res['total_charges'],
            'net_pnl': res['net_pnl'],
            'profit_pct': res['profit_pct'],
            'ending_capital': res['ending_capital'],
            'friction_ratio_pct': res['friction_ratio_pct'],
            'charge_breakdown': res['charge_breakdown'],
            'trades': res.get('trades', []),
        }
        json.dump(clean_res, f, indent=2, default=str)
    print("[SAVED] Written to wide_watchlist_1year_audit.json")

if __name__ == '__main__':
    main()
