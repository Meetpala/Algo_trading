import os, sys
sys.path.insert(0, os.path.abspath('.'))
import json
from datetime import time
import pandas as pd
from backtest_3months import (
    load_config, build_active_radar, load_preparsed_data, run_fast_backtest
)

def run_optimization():
    config = load_config()
    watchlist = build_active_radar(config.raw_dict)
    all_symbols = ['NSEI'] + watchlist
    
    print(f"Pre-loading data for {len(all_symbols)} symbols...")
    preloaded = load_preparsed_data(all_symbols)
    all_dates = sorted(set(preloaded['NSEI']['df']['Datetime'].dt.date))
    print(f"Loaded {len(all_dates)} sessions ({all_dates[0]} to {all_dates[-1]}).\n")
    
    # Grid of configurations to test:
    # Capital: 15,000 (Baseline) vs 25,000
    # Slots: 2 vs 3
    # Entry cutoff: 12:30, 13:00, 13:15, 13:30
    
    experiments = [
        # Baseline
        {"name": "Baseline (Rs.15k, 2 slots, cutoff 12:30)", "capital": 15000.0, "slots": 2, "cutoff": time(12, 30)},
        # Rs. 25k with 2 slots
        {"name": "Rs.25k, 2 slots, cutoff 12:30 (Current Rules)", "capital": 25000.0, "slots": 2, "cutoff": time(12, 30)},
        {"name": "Rs.25k, 2 slots, cutoff 13:00", "capital": 25000.0, "slots": 2, "cutoff": time(13, 0)},
        {"name": "Rs.25k, 2 slots, cutoff 13:15 (Euro Open)", "capital": 25000.0, "slots": 2, "cutoff": time(13, 15)},
        {"name": "Rs.25k, 2 slots, cutoff 13:30", "capital": 25000.0, "slots": 2, "cutoff": time(13, 30)},
        # Rs. 25k with 3 slots
        {"name": "Rs.25k, 3 slots, cutoff 12:30 (Multi-Slot)", "capital": 25000.0, "slots": 3, "cutoff": time(12, 30)},
        {"name": "Rs.25k, 3 slots, cutoff 13:00", "capital": 25000.0, "slots": 3, "cutoff": time(13, 0)},
        {"name": "Rs.25k, 3 slots, cutoff 13:15 (Euro Open)", "capital": 25000.0, "slots": 3, "cutoff": time(13, 15)},
        {"name": "Rs.25k, 3 slots, cutoff 13:30", "capital": 25000.0, "slots": 3, "cutoff": time(13, 30)},
    ]
    
    results = []
    
    for exp in experiments:
        cap = exp["capital"]
        slots = exp["slots"]
        cutoff = exp["cutoff"]
        name = exp["name"]
        
        res = run_fast_backtest(
            data=preloaded,
            watchlist_symbols=watchlist,
            target_r=2.0,
            use_fixed_target=True,
            trail_enabled=False,
            max_slots=slots,
            session_dates=all_dates,
            starting_capital=cap,
            entry_cutoff_time=cutoff,
        )
        
        # Calculate max drawdown from daily_stats
        peaks = []
        dd_list = []
        peak = cap
        for ds in res.get("daily_stats", []):
            closing = ds["closing_capital"]
            if closing > peak:
                peak = closing
            dd = peak - closing
            dd_list.append(dd)
        max_dd = max(dd_list) if dd_list else 0.0
        max_dd_pct = (max_dd / cap) * 100.0 if cap > 0 else 0.0
        
        results.append({
            "Experiment": name,
            "Capital": cap,
            "Slots": slots,
            "Cutoff": cutoff.strftime("%H:%M"),
            "Trades": res["total_trades"],
            "Wins": res["winning_trades"],
            "Losses": res["losing_trades"],
            "Win Rate": res["win_rate"],
            "PF": res["profit_factor"],
            "Gross PnL": res["gross_pnl"],
            "Charges": res["total_charges"],
            "Net PnL": res["net_pnl"],
            "Net ROI %": res["profit_pct"],
            "Friction %": res["friction_ratio_pct"],
            "Max DD (Rs)": round(max_dd, 2),
            "Max DD %": round(max_dd_pct, 2),
        })
        print(f"Done: {name} -> Trades: {res['total_trades']}, Win Rate: {res['win_rate']}%, Net: Rs. {res['net_pnl']:+,.2f} ({res['profit_pct']:+.2f}%), Max DD: Rs. {max_dd:.2f}")

    df_res = pd.DataFrame(results)
    print("\n" + "=" * 125)
    print("                     CAPITAL (RS. 25,000) & ENTRY WINDOW OPTIMIZATION MATRIX")
    print("=" * 125)
    print(df_res.to_string(index=False))
    print("=" * 125)
    
    with open("scratch/capital_time_optimization.json", "w") as f:
        json.dump(results, f, indent=2)
    print("\nResults saved to scratch/capital_time_optimization.json")

if __name__ == "__main__":
    run_optimization()
