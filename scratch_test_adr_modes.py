import sys
from pathlib import Path
sys.path.insert(0, str(Path(".").resolve()))
import backtest_3months as b3
from backtest_3months import load_preparsed_data, run_fast_backtest
from apex3.strategy.trail import compute_initial_stop_and_target
from apex3.strategy.signal import TradeSignal

wl = ['HINDALCO', 'JSWSTEEL', 'RELIANCE', 'MARUTI', 'ITC', 'TCS', 'SBIN', 'TRENT', 'KOTAKBANK', 'SUNPHARMA']
data = load_preparsed_data(['NSEI'] + wl)
dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))[-57:]

for floor in [0.020, 0.022, 0.025, 0.028, 0.030]:
    def custom_eval(bar, regime, active_positions_count, cfg):
        sched = cfg.get('session_schedule', cfg.get('schedule', {}))
        gates = cfg.get('signal_gates', cfg.get('signal', {}))
        risk_cfg = cfg.get('risk_management', cfg.get('risk', {}))
        start_t = sched.get('entry_start_time', '09:35')
        cutoff_t = sched.get('entry_cutoff_time', '12:30')
        if not (start_t <= bar.timestamp_hhmm <= cutoff_t): return None
        if active_positions_count >= int(risk_cfg.get('max_concurrent_positions', 2)): return None
        regime_val = regime.value if hasattr(regime, 'value') else str(regime)
        if regime_val not in ('BULLISH', 'BEARISH'): return None
        side = 'LONG' if regime_val == 'BULLISH' else 'SHORT'
        if bar.or_low > 0 and (bar.or_high - bar.or_low) / bar.or_low > 0.0105: return None
        if bar.close > 0 and (bar.atr14 / bar.close) < 0.0022: return None

        # ADR Exhaustion with floor
        effective_adr = max(floor, bar.adr14_pct)
        rng = (bar.session_high_so_far - bar.session_low_so_far) / bar.session_low_so_far
        if rng > 0.60 * effective_adr:
            return None

        cushion = float(gates.get('min_breakout_cushion_atr', 0.15)) * bar.atr14
        min_clv = float(gates.get('min_clv_ratio', 0.70))
        rel_strength = bar.stock_return_20 - bar.nifty_return_20
        if side == 'LONG':
            bar_range = max(bar.bar_high - bar.bar_low, 0.05) if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
            clv = (bar.close - bar.bar_low) / bar_range if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
            if not (bar.close >= (bar.or_high + cushion) and clv >= min_clv and bar.close > bar.session_vwap and rel_strength > 0): return None
        else:
            bar_range = max(bar.bar_high - bar.bar_low, 0.05) if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
            clv = (bar.bar_high - bar.close) / bar_range if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
            if not (bar.close <= (bar.or_low - cushion) and clv >= min_clv and bar.close < bar.session_vwap and rel_strength < 0): return None

        if bar.avg_volume_20 <= 0 or (bar.bar_volume / bar.avg_volume_20) < 2.0: return None
        if bar.bid_ask_spread_bps > 4.0: return None
        levels = compute_initial_stop_and_target(side=side, entry_price=bar.close, swing_low_5=bar.swing_low_5, swing_high_5=bar.swing_high_5, atr14=bar.atr14, cfg=cfg)
        if not (0.0055 <= levels.risk_pct <= 0.0090): return None
        return TradeSignal(symbol=bar.symbol, side=side, entry_price=bar.close, levels=levels, volume_surge_ratio=round(bar.bar_volume / bar.avg_volume_20, 2), relative_strength=round(rel_strength, 5), conviction_score=1.0)

    b3.evaluate_candidate_signal = custom_eval
    res = run_fast_backtest(data, wl, target_r=2.0, use_fixed_target=True, trail_enabled=False, max_slots=2, session_dates=dates)
    fric = res['friction_ratio_pct']
    print(f"Floor={floor:.3f}: Trades={res['total_trades']} Wins={res['winning_trades']} Losses={res['losing_trades']} WinRate={res['win_rate']:.1f}% Net=Rs.{res['net_pnl']:.2f} Friction={fric:.1f}%")
