import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
import pandas as pd
from datetime import time
import backtest_3months as b3
from backtest_3months import load_preparsed_data, run_fast_backtest
from apex3.strategy.trail import compute_initial_stop_and_target
from apex3.strategy.signal import TradeSignal

core_wl = ['HINDALCO', 'JSWSTEEL', 'RELIANCE', 'MARUTI', 'ITC', 'TCS', 'SBIN', 'TRENT', 'KOTAKBANK', 'SUNPHARMA']
ext_radar = ['BHARTIARTL', 'JIOFIN', 'TATASTEEL']
all_syms = core_wl + ext_radar
data = load_preparsed_data(['NSEI'] + all_syms)

dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))
dates_6m = dates
dates_3m = dates[-57:]

configs = [
    ("Elite 10 baseline", core_wl, None),
    ("Elite 10 + Extended Radar", core_wl + ext_radar, None),
    ("Exclude ITC (bleeder -Rs.219)", [s for s in core_wl if s != "ITC"] + ext_radar, None),
    ("Exclude ITC + SBIN", [s for s in core_wl if s not in {"ITC", "SBIN"}] + ext_radar, None),
    ("Exclude ITC + SBIN + Noon Cutoff", [s for s in core_wl if s not in {"ITC", "SBIN"}] + ext_radar, time(12, 0)),
]

for name, wl, cutoff in configs:
    def eval_fn(bar, regime, active_positions_count, cfg):
        sched = cfg.get("session_schedule", cfg.get("schedule", {}))
        gates = cfg.get("signal_gates", cfg.get("signal", {}))
        risk_cfg = cfg.get("risk_management", cfg.get("risk", {}))

        start_t = sched.get("entry_start_time", "09:35")
        cutoff_t = sched.get("entry_cutoff_time", "12:30")
        if not (start_t <= bar.timestamp_hhmm <= cutoff_t): return None
        if active_positions_count >= int(risk_cfg.get("max_concurrent_positions", 2)): return None

        regime_val = regime.value if hasattr(regime, "value") else str(regime)
        if regime_val == "BULLISH": side = "LONG"
        elif regime_val == "BEARISH": side = "SHORT"
        else: return None

        if bar.or_low > 0 and ((bar.or_high - bar.or_low) / bar.or_low) > 0.0105: return None

        is_core = bar.symbol in core_wl
        cushion_mult = float(gates.get("min_breakout_cushion_atr", 0.15))
        min_clv = float(gates.get("min_clv_ratio", 0.70)) if is_core else 0.75
        min_vol = float(gates.get("min_volume_surge_ratio", 2.0)) if is_core else 2.5
        cushion = cushion_mult * bar.atr14
        rel_strength = bar.stock_return_20 - bar.nifty_return_20

        if side == "LONG":
            bar_range = max(bar.bar_high - bar.bar_low, 0.05) if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
            clv = (bar.close - bar.bar_low) / bar_range if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
            if not (bar.close >= (bar.or_high + cushion) and clv >= min_clv and bar.close > bar.session_vwap and rel_strength > 0): return None
        else:
            bar_range = max(bar.bar_high - bar.bar_low, 0.05) if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
            clv = (bar.bar_high - bar.close) / bar_range if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
            if not (bar.close <= (bar.or_low - cushion) and clv >= min_clv and bar.close < bar.session_vwap and rel_strength < 0): return None

        if bar.avg_volume_20 <= 0: return None
        vol_ratio = bar.bar_volume / bar.avg_volume_20
        if vol_ratio < min_vol: return None
        if bar.bid_ask_spread_bps > 4.0: return None

        levels = compute_initial_stop_and_target(side=side, entry_price=bar.close, swing_low_5=bar.swing_low_5, swing_high_5=bar.swing_high_5, atr14=bar.atr14, cfg=cfg)
        if not (0.0055 <= levels.risk_pct <= 0.0090): return None

        spread_floor = max(bar.bid_ask_spread_bps, 1.0)
        core_mult = 1.35 if is_core else 1.0
        conviction_score = core_mult * ((abs(rel_strength) * 100.0 * vol_ratio) / spread_floor)

        return TradeSignal(
            symbol=bar.symbol, side=side, entry_price=bar.close, levels=levels,
            volume_surge_ratio=round(vol_ratio, 2), relative_strength=round(rel_strength, 5),
            conviction_score=round(conviction_score, 4),
        )

    b3.evaluate_candidate_signal = eval_fn

    res_6m = run_fast_backtest(data, wl, target_r=2.0, use_fixed_target=True, trail_enabled=False, max_slots=2, entry_cutoff_time=cutoff, session_dates=dates_6m)
    res_3m = run_fast_backtest(data, wl, target_r=2.0, use_fixed_target=True, trail_enabled=False, max_slots=2, entry_cutoff_time=cutoff, session_dates=dates_3m)

    print(f"\n=== {name} ===")
    print(f"6-Month: Trades={res_6m['total_trades']:2d} Wins={res_6m['winning_trades']:2d} WinRate={res_6m['win_rate']:5.1f}% PF={res_6m['profit_factor']:.2f} Gross=Rs.{res_6m['gross_pnl']:+8.2f} Charges=Rs.{res_6m['total_charges']:6.2f} Net=Rs.{res_6m['net_pnl']:+8.2f} Friction={res_6m['friction_ratio_pct']:5.1f}%")
    print(f"3-Month: Trades={res_3m['total_trades']:2d} Wins={res_3m['winning_trades']:2d} WinRate={res_3m['win_rate']:5.1f}% PF={res_3m['profit_factor']:.2f} Gross=Rs.{res_3m['gross_pnl']:+8.2f} Charges=Rs.{res_3m['total_charges']:6.2f} Net=Rs.{res_3m['net_pnl']:+8.2f} Friction={res_3m['friction_ratio_pct']:5.1f}%")
