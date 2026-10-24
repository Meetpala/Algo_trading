import os, sys, glob
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
import pandas as pd
from apex3.config import load_config
from backtest_3months import load_preparsed_data, run_fast_backtest

config = load_config()
csv_files = sorted(glob.glob("scratch_data/*.csv"))
all_symbols = [os.path.basename(f).replace(".csv", "") for f in csv_files if not f.endswith("NSEI.csv")]

print(f"Total symbols to screen: {len(all_symbols)}")

# Load all data
data = load_preparsed_data(["NSEI"] + all_symbols)
nifty_df = data["NSEI"]["df"]
dates = sorted(list(set(nifty_df["Datetime"].dt.date)))
dates_6m = dates
dates_3m = dates[-57:]
print(f"6-Month: {len(dates_6m)} sessions ({dates_6m[0]} to {dates_6m[-1]})")
print(f"3-Month: {len(dates_3m)} sessions ({dates_3m[0]} to {dates_3m[-1]})")

results = []
for sym in all_symbols:
    # 6-Month run for single stock
    res_6m = run_fast_backtest(data, [sym], target_r=2.0, use_fixed_target=True, trail_enabled=False, max_slots=1, session_dates=dates_6m)
    # 3-Month run for single stock
    res_3m = run_fast_backtest(data, [sym], target_r=2.0, use_fixed_target=True, trail_enabled=False, max_slots=1, session_dates=dates_3m)
    
    results.append({
        "symbol": sym,
        "trades_6m": res_6m["total_trades"],
        "wins_6m": res_6m["winning_trades"],
        "win_rate_6m": res_6m["win_rate"],
        "gross_pnl_6m": res_6m["gross_pnl"],
        "charges_6m": res_6m["total_charges"],
        "net_pnl_6m": res_6m["net_pnl"],
        "friction_6m": res_6m["friction_ratio_pct"],
        "trades_3m": res_3m["total_trades"],
        "wins_3m": res_3m["winning_trades"],
        "win_rate_3m": res_3m["win_rate"],
        "gross_pnl_3m": res_3m["gross_pnl"],
        "charges_3m": res_3m["total_charges"],
        "net_pnl_3m": res_3m["net_pnl"],
        "friction_3m": res_3m["friction_ratio_pct"],
    })

df_res = pd.DataFrame(results)
df_res = df_res.sort_values(by="net_pnl_6m", ascending=False)
print("\n=== INDIVIDUAL STOCK SCREENING RESULTS (6M & 3M) ===")
print(df_res[["symbol", "trades_6m", "net_pnl_6m", "friction_6m", "trades_3m", "net_pnl_3m", "friction_3m"]].to_string(index=False))

# Identify strictly positive in both
positive_both = df_res[(df_res["net_pnl_6m"] > 0) & (df_res["net_pnl_3m"] > 0)]
print("\n=== STOCKS STRICTLY POSITIVE IN BOTH 6M AND 3M ===")
print(positive_both[["symbol", "trades_6m", "net_pnl_6m", "friction_6m", "trades_3m", "net_pnl_3m", "friction_3m"]].to_string(index=False))
