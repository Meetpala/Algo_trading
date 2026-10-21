import os, sys
sys.path.insert(0, os.getcwd())
from backtest_3months import run_fast_backtest, load_preparsed_data, build_active_radar, load_config

config = load_config()
watchlist = build_active_radar(config.raw_dict)
data = load_preparsed_data(['NSEI'] + watchlist)
three_month_dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))[-57:]

import inspect
import backtest_3months

# Let's inspect run_fast_backtest lines 810-950 and 550-650
# Let's see how many times pending_entry is set vs can_fill vs quantity
counts = {
    "cands_found": 0,
    "pending_set": 0,
    "pending_evaluated": 0,
    "can_fill_true": 0,
    "can_fill_false": 0,
    "fill_acceptable_true": 0,
    "fill_acceptable_false": 0,
    "gate9_true": 0,
    "gate9_false": 0,
    "quantity_positive": 0,
    "quantity_zero": 0,
    "positions_opened": 0,
    "trades_closed": 0,
}

# We can monkeypatch or inspect directly
old_size = backtest_3months.size_backtest_quantity
def debug_size(*args, **kwargs):
    q = old_size(*args, **kwargs)
    if q > 0:
        counts["quantity_positive"] += 1
    else:
        counts["quantity_zero"] += 1
    return q
backtest_3months.size_backtest_quantity = debug_size

old_sig = backtest_3months.evaluate_candidate_signal
def debug_sig(*args, **kwargs):
    s = old_sig(*args, **kwargs)
    if s is not None:
        counts["cands_found"] += 1
    return s
backtest_3months.evaluate_candidate_signal = debug_sig

res = backtest_3months.run_fast_backtest(
    data=data,
    watchlist_symbols=watchlist,
    target_r=2.0,
    session_dates=three_month_dates,
)
print("Counters:", counts)
print("Total trades:", res['total_trades'])
