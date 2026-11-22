import sys
from datetime import time, datetime
from pathlib import Path
import pandas as pd
import json

from apex3.config import load_config
from backtest_3months import load_preparsed_data, build_active_radar, run_fast_backtest

def run_grid_search():
    cfg = load_config()
    watchlist = build_active_radar(cfg.raw_dict)
    all_symbols = ['NSEI'] + watchlist

    print("Loading data for grid search...")
    data = load_preparsed_data(all_symbols)
    dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))
    dates_1y = dates
    dates_3m = dates[-57:]

    print(f"Total sessions: {len(dates_1y)} (1Y: {dates_1y[0]} to {dates_1y[-1]}), 3M: {len(dates_3m)}")

    cutoffs = [
        time(11, 30),
        time(12, 0),
        time(12, 30),
        time(13, 0),
        time(13, 15),
        time(13, 30),
        time(14, 0),
    ]

    results_3m = []
    results_1y = []

    print("\n--- Testing 57-Session (3-Month) Period with Rs. 25,000 ---")
    for cutoff in cutoffs:
        res = run_fast_backtest(
            data=data,
            watchlist_symbols=watchlist,
            target_r=2.0,
            use_fixed_target=True,
            trail_enabled=False,
            max_slots=2,
            session_dates=dates_3m,
            starting_capital=25000.0,
            entry_cutoff_time=cutoff,
        )
        results_3m.append({
            'cutoff': cutoff.strftime("%H:%M"),
            'trades': res['total_trades'],
            'win_rate': f"{res['win_rate']:.1f}%",
            'gross': res['gross_pnl'],
            'charges': res['total_charges'],
            'net': res['net_pnl'],
            'pf': res['profit_factor'],
            'friction_pct': f"{res['friction_ratio_pct']:.1f}%",
            'roi_pct': f"{res['profit_pct']:.2f}%"
        })

    print(pd.DataFrame(results_3m).to_string(index=False))

    print("\n--- Testing Full 1-Year (264 Sessions) Period with Rs. 25,000 ---")
    for cutoff in cutoffs:
        res = run_fast_backtest(
            data=data,
            watchlist_symbols=watchlist,
            target_r=2.0,
            use_fixed_target=True,
            trail_enabled=False,
            max_slots=2,
            session_dates=dates_1y,
            starting_capital=25000.0,
            entry_cutoff_time=cutoff,
        )
        results_1y.append({
            'cutoff': cutoff.strftime("%H:%M"),
            'trades': res['total_trades'],
            'win_rate': f"{res['win_rate']:.1f}%",
            'gross': res['gross_pnl'],
            'charges': res['total_charges'],
            'net': res['net_pnl'],
            'pf': res['profit_factor'],
            'friction_pct': f"{res['friction_ratio_pct']:.1f}%",
            'roi_pct': f"{(res['net_pnl'] / 25000.0) * 100:.2f}%"
        })

    print(pd.DataFrame(results_1y).to_string(index=False))

if __name__ == '__main__':
    run_grid_search()
