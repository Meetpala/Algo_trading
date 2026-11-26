import os
import sys
import json
from datetime import date, time
import pandas as pd

REPO_ROOT = "."
from apex3.config import load_config
from backtest_3months import load_preparsed_data, build_active_radar, run_fast_backtest

cfg = load_config()
watchlist = build_active_radar(cfg.raw_dict)
all_symbols = ["NSEI"] + watchlist

print("Loading recent data...")
data = load_preparsed_data(all_symbols)
dates = sorted(set(data["NSEI"]["df"]["Datetime"].dt.date))
recent_dates = [d for d in dates if d >= date(2026, 11, 23)]
print(f"Auditing sessions: {recent_dates}")

for sess in recent_dates:
    is_hol = cfg.schedule.is_holiday(sess)
    is_spec, name = cfg.schedule.is_special_session(sess)
    print(f"\n--- SESSION: {sess} ({sess.strftime('%A')}) | Holiday: {is_hol} | Special: {is_spec} {name} ---")
    if is_hol:
        print("  MARKET CLOSED (NSE Official Holiday)")
        continue

    res = run_fast_backtest(
        data=data,
        watchlist_symbols=watchlist,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=2,
        session_dates=[sess],
        starting_capital=25000.0,
        entry_cutoff_time=time(12, 0),
    )
    print(f"  Trades: {len(res['trades'])}")
    if res['trades']:
        for t in res['trades']:
            print(f"  -> {t['symbol']} | {t['side']} | Qty: {t['qty']} | Entry: {t['entry_time']} @ {t['entry_price']} | Exit: {t['exit_time']} @ {t['exit_price']} | Reason: {t['exit_reason']} | Gross: {t['gross_pnl']:.2f} | Charges: {t['total_charges']:.2f} | Net: {t['net_pnl']:.2f}")
    else:
        print("  NO QUALIFYING BREAKOUTS — 0 trades executed. Capital preserved.")
