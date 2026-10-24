import os, sys, glob
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
import pandas as pd
import backtest_3months as b3
from backtest_3months import load_preparsed_data, run_fast_backtest
from apex3.strategy.trail import compute_initial_stop_and_target
from apex3.strategy.signal import TradeSignal

csv_files = sorted(glob.glob("scratch_data/*.csv"))
all_symbols = [os.path.basename(f).replace(".csv", "") for f in csv_files if not f.endswith("NSEI.csv")]

data = load_preparsed_data(["NSEI"] + all_symbols)
dates = sorted(set(data["NSEI"]["df"]["Datetime"].dt.date))
dates_6m = dates
dates_3m = dates[-57:]

print(f"Screening {len(all_symbols)} symbols across 6M ({len(dates_6m)} days) and 3M ({len(dates_3m)} days)...")

def custom_candidate_signal(bar, regime, active_positions_count, cfg):
    sched = cfg.get("session_schedule", cfg.get("schedule", {}))
    gates = cfg.get("signal_gates", cfg.get("signal", {}))
    risk_cfg = cfg.get("risk_management", cfg.get("risk", {}))

    start_t = sched.get("entry_start_time", "09:35")
    cutoff_t = sched.get("entry_cutoff_time", "12:30")
    if not (start_t <= bar.timestamp_hhmm <= cutoff_t):
        return None
    if active_positions_count >= int(risk_cfg.get("max_concurrent_positions", 2)):
        return None

    regime_val = regime.value if hasattr(regime, "value") else str(regime)
    if regime_val == "BULLISH":
        side = "LONG"
    elif regime_val == "BEARISH":
        side = "SHORT"
    else:
        return None

    # Gate 9A: OR Width Cap (<= 1.05%)
    if bar.or_low > 0 and ((bar.or_high - bar.or_low) / bar.or_low) > 0.0105:
        return None

    cushion_mult = float(gates.get("min_breakout_cushion_atr", 0.15))
    min_clv = float(gates.get("min_clv_ratio", 0.70))
    cushion = cushion_mult * bar.atr14
    rel_strength = bar.stock_return_20 - bar.nifty_return_20

    if side == "LONG":
        bar_range = max(bar.bar_high - bar.bar_low, 0.05) if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
        clv = (bar.close - bar.bar_low) / bar_range if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
        if not (bar.close >= (bar.or_high + cushion) and clv >= min_clv and bar.close > bar.session_vwap and rel_strength > 0):
            return None
    else:
        bar_range = max(bar.bar_high - bar.bar_low, 0.05) if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
        clv = (bar.bar_high - bar.close) / bar_range if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
        if not (bar.close <= (bar.or_low - cushion) and clv >= min_clv and bar.close < bar.session_vwap and rel_strength < 0):
            return None

    if bar.avg_volume_20 <= 0 or (bar.bar_volume / bar.avg_volume_20) < 2.0:
        return None
    if bar.bid_ask_spread_bps > 4.0:
        return None

    levels = compute_initial_stop_and_target(
        side=side,
        entry_price=bar.close,
        swing_low_5=bar.swing_low_5,
        swing_high_5=bar.swing_high_5,
        atr14=bar.atr14,
        cfg=cfg,
    )
    if not (0.0055 <= levels.risk_pct <= 0.0090):
        return None

    spread_floor = max(bar.bid_ask_spread_bps, 1.0)
    conviction_score = (abs(rel_strength) * 100.0 * (bar.bar_volume / bar.avg_volume_20)) / spread_floor

    return TradeSignal(
        symbol=bar.symbol,
        side=side,
        entry_price=bar.close,
        levels=levels,
        volume_surge_ratio=round(bar.bar_volume / bar.avg_volume_20, 2),
        relative_strength=round(rel_strength, 5),
        conviction_score=round(conviction_score, 4),
    )

b3.evaluate_candidate_signal = custom_candidate_signal

def test_symbol(sym, sess_dates):
    res = run_fast_backtest(
        data=data,
        watchlist_symbols=[sym],
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=1,
        session_dates=sess_dates,
    )
    return res

# Let's run with standard gates: OR width <= 1.05%, Stop width in [0.55%, 0.90%], Volume >= 2.0x, CLV >= 0.70
rows = []
for sym in all_symbols:
    r6 = test_symbol(sym, dates_6m)
    r3 = test_symbol(sym, dates_3m)
    rows.append({
        "symbol": sym,
        "trades_6m": r6["total_trades"],
        "wins_6m": r6["winning_trades"],
        "win_rate_6m": round(r6["win_rate"], 1),
        "gross_6m": round(r6["gross_pnl"], 2),
        "charges_6m": round(r6["total_charges"], 2),
        "net_6m": round(r6["net_pnl"], 2),
        "friction_6m": round(r6["friction_ratio_pct"], 1) if r6["gross_pnl"] > 0 else 999.0,
        "trades_3m": r3["total_trades"],
        "wins_3m": r3["winning_trades"],
        "win_rate_3m": round(r3["win_rate"], 1),
        "gross_3m": round(r3["gross_pnl"], 2),
        "charges_3m": round(r3["total_charges"], 2),
        "net_3m": round(r3["net_pnl"], 2),
        "friction_3m": round(r3["friction_ratio_pct"], 1) if r3["gross_pnl"] > 0 else 999.0,
    })

df = pd.DataFrame(rows)
df = df.sort_values(by="net_6m", ascending=False)
print("\n=== COMPLETE 31-STOCK PERFORMANCE TABLE ===")
print(df[["symbol", "trades_6m", "win_rate_6m", "net_6m", "friction_6m", "trades_3m", "win_rate_3m", "net_3m", "friction_3m"]].to_string(index=False))

pos = df[(df["net_6m"] > 0) & (df["net_3m"] > 0)]
print("\n=== STOCKS STRICTLY POSITIVE IN BOTH 6M AND 3M ===")
print(pos[["symbol", "trades_6m", "win_rate_6m", "net_6m", "friction_6m", "trades_3m", "win_rate_3m", "net_3m", "friction_3m"]].to_string(index=False))
