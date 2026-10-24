import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
import pandas as pd
import backtest_3months as b3
from backtest_3months import load_preparsed_data, run_fast_backtest
from apex3.strategy.trail import compute_initial_stop_and_target
from apex3.strategy.signal import TradeSignal

wl = ['HINDALCO', 'JSWSTEEL', 'RELIANCE', 'MARUTI', 'ITC', 'TCS', 'SBIN', 'TRENT', 'KOTAKBANK', 'SUNPHARMA']
data = load_preparsed_data(['NSEI'] + wl)
dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))[-57:]

matrix = [
    # (name, enforce_or_cap, min_atr, adr_mode)
    ("Baseline (no OR cap, no ATR floor, no ADR cap)", False, 0.0, "none"),
    ("Baseline + const ADR <= 1.20% (0.60*0.02)", False, 0.0, "const_0.02"),
    ("Add OR cap (<= 1.05%)", True, 0.0, "none"),
    ("Add OR cap + const ADR <= 1.20%", True, 0.0, "const_0.02"),
    ("Add Dynamic ADR (<= 60% ADR14)", False, 0.0, "dynamic"),
    ("Add OR cap + Dynamic ADR", True, 0.0, "dynamic"),
    ("Add min_atr=0.0015 + OR cap", True, 0.0015, "none"),
    ("Add min_atr=0.0022 + OR cap", True, 0.0022, "none"),
    ("All 4 enforced (OR cap, min_atr=0.0022, Dynamic ADR)", True, 0.0022, "dynamic"),
]

for name, use_or, atr_val, adr_mode in matrix:
    def custom_eval(bar, regime, active_positions_count, cfg):
        sched = cfg.get("session_schedule", cfg.get("schedule", {}))
        gates = cfg.get("signal_gates", cfg.get("signal", {}))
        risk_cfg = cfg.get("risk_management", cfg.get("risk", {}))

        start_t = sched.get("entry_start_time", "09:35")
        cutoff_t = sched.get("entry_cutoff_time", "12:30")
        if not (start_t <= bar.timestamp_hhmm <= cutoff_t):
            return None
        max_slots = int(risk_cfg.get("max_concurrent_positions", 2))
        if active_positions_count >= max_slots:
            return None

        regime_val = regime.value if hasattr(regime, "value") else str(regime)
        if regime_val == "BULLISH":
            side = "LONG"
        elif regime_val == "BEARISH":
            side = "SHORT"
        else:
            return None

        # OR cap
        if use_or and bar.or_low > 0:
            if (bar.or_high - bar.or_low) / bar.or_low > 0.0105:
                return None

        # Min ATR floor
        if atr_val > 0 and bar.close > 0:
            if (bar.atr14 / bar.close) < atr_val:
                return None

        # ADR exhaustion
        if adr_mode == "dynamic":
            if bar.adr14_pct > 0 and bar.session_low_so_far > 0:
                rng = (bar.session_high_so_far - bar.session_low_so_far) / bar.session_low_so_far
                if rng > 0.60 * bar.adr14_pct:
                    return None
        elif adr_mode == "const_0.02":
            if bar.session_low_so_far > 0:
                rng = (bar.session_high_so_far - bar.session_low_so_far) / bar.session_low_so_far
                if rng > 0.60 * 0.02:
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

        if bar.avg_volume_20 <= 0:
            return None
        vol_ratio = bar.bar_volume / bar.avg_volume_20
        min_vol = float(gates.get("min_volume_surge_ratio", 2.0))
        if vol_ratio < min_vol:
            return None

        if bar.bid_ask_spread_bps > float(gates.get("max_spread_bps", 4.0)):
            return None

        min_circuit_dist = float(gates.get("min_circuit_distance_pct", 0.020))
        if min_circuit_dist > 0.5:
            min_circuit_dist /= 100.0
        if side == "LONG":
            if (bar.upper_circuit - bar.close) / bar.close < min_circuit_dist:
                return None
        else:
            if (bar.close - bar.lower_circuit) / bar.close < min_circuit_dist:
                return None

        levels = compute_initial_stop_and_target(
            side=side,
            entry_price=bar.close,
            swing_low_5=bar.swing_low_5,
            swing_high_5=bar.swing_high_5,
            atr14=bar.atr14,
            cfg=cfg,
        )
        min_r_pct = float(gates.get("min_stop_distance_pct", 0.0055))
        max_r_pct = float(gates.get("max_stop_distance_pct", 0.0090))
        if not (min_r_pct <= levels.risk_pct <= max_r_pct):
            return None

        spread_floor = max(bar.bid_ask_spread_bps, 1.0)
        conviction_score = (abs(rel_strength) * 100.0 * vol_ratio) / spread_floor

        return TradeSignal(
            symbol=bar.symbol,
            side=side,
            entry_price=bar.close,
            levels=levels,
            volume_surge_ratio=round(vol_ratio, 2),
            relative_strength=round(rel_strength, 5),
            conviction_score=round(conviction_score, 4),
        )

    b3.evaluate_candidate_signal = custom_eval
    res = run_fast_backtest(data, wl, target_r=2.0, use_fixed_target=True, trail_enabled=False, max_slots=2, session_dates=dates)
    fric = res["friction_ratio_pct"]
    print(f"{name:55} | Trades={res['total_trades']:2d} Wins={res['winning_trades']:2d} Losses={res['losing_trades']:2d} WinRate={res['win_rate']:5.1f}% Net=Rs.{res['net_pnl']:8.2f} Friction={fric:5.1f}%")
