import sys
sys.path.insert(0, '.')
from apex3.config import load_config
from apex3.strategy.engine import build_active_radar
from backtest_3months import load_preparsed_data, evaluate_market_regime, MarketRegime, compute_atr, compute_session_vwap, StockCandidateBar, OpeningRange
from apex3.strategy.trail import compute_initial_stop_and_target
from datetime import time, timedelta
import pandas as pd

config = load_config()
watchlist = build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = load_preparsed_data(all_symbols)

nifty_df = data['NSEI']['df']
nifty_candles = data['NSEI']['candles']
three_m_dates = sorted(set(nifty_df['Datetime'].dt.date))

gates_failed = {}

for session_date in three_m_dates:
    n_sess_mask = (nifty_df['Datetime'].dt.date == session_date)
    sess_indices = nifty_df[n_sess_mask].index.tolist()
    if len(sess_indices) < 6: continue
    
    opening_ranges = {}
    stock_sess_map = {}
    stock_sess_indices = {}
    for sym in watchlist:
        s_candles = data[sym]['candles']
        s_df = data[sym]['df']
        s_indices = s_df[s_df['Datetime'].dt.date == session_date].index.tolist()
        stock_sess_indices[sym] = s_indices
        stock_sess_map[sym] = {s_candles[idx].timestamp: idx for idx in s_indices}
        or_indices = [idx for idx in s_indices if s_candles[idx].timestamp.time() < time(9, 30)]
        if len(or_indices) >= 3:
            or_high = max(s_candles[i].high for i in or_indices)
            or_low = min(s_candles[i].low for i in or_indices)
            opening_ranges[sym] = OpeningRange(symbol=sym, high=or_high, low=or_low, is_complete=True, candle_count=len(or_indices))
        else:
            opening_ranges[sym] = OpeningRange(symbol=sym, high=0.0, low=0.0, is_complete=False)

    for step, n_idx in enumerate(sess_indices):
        c_bar = nifty_candles[n_idx]
        current_dt = c_bar.timestamp
        bar_close_dt = current_dt + timedelta(minutes=5)
        if not (time(9, 35) <= bar_close_dt.time() <= time(12, 30)):
            continue
        bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
        regime_eval = evaluate_market_regime(bench_slice, config.regime)
        if regime_eval.regime == MarketRegime.NEUTRAL:
            continue
        next_n_idx = sess_indices[step + 1] if step + 1 < len(sess_indices) else None
        if next_n_idx is None: continue
        next_fill_dt = nifty_candles[next_n_idx].timestamp
        
        for sym in watchlist:
            s_idx = stock_sess_map[sym].get(current_dt)
            next_s_idx = stock_sess_map[sym].get(next_fill_dt)
            if s_idx is None or next_s_idx is None: continue
            s_bar = data[sym]['candles'][s_idx]
            today_indices = [idx for idx in stock_sess_indices[sym] if data[sym]['candles'][idx].timestamp <= current_dt]
            today_candles = [data[sym]['candles'][idx] for idx in today_indices]
            if len(today_candles) < 5: continue
            or_range = opening_ranges[sym]
            if not or_range.is_complete: continue
            
            first_open = data[sym]['candles'][stock_sess_indices[sym][0]].open
            history_start = max(0, s_idx - 60)
            stock_history = data[sym]['candles'][history_start:s_idx + 1]
            atr_series = compute_atr([c.high for c in stock_history], [c.low for c in stock_history], [c.close for c in stock_history], period=14)
            if not atr_series: continue
            atr_val = atr_series[-1]
            prior_volume_bars = data[sym]['candles'][max(0, s_idx - 20):s_idx]
            if len(prior_volume_bars) < 20: continue
            average_volume = sum(c.volume for c in prior_volume_bars) / len(prior_volume_bars)
            stock_closes = [c.close for c in stock_history[-20:]]
            bench_closes = [c.close for c in bench_slice[-20:]]
            stock_return_20 = (stock_closes[-1] - stock_closes[0]) / stock_closes[0] * 100.0
            nifty_return_20 = (bench_closes[-1] - bench_closes[0]) / bench_closes[0] * 100.0
            swing_bars = today_candles[-5:]
            session_high = max(c.high for c in today_candles)
            session_low = min(c.low for c in today_candles)
            session_vwap = compute_session_vwap([c.high for c in today_candles], [c.low for c in today_candles], [c.close for c in today_candles], [c.volume for c in today_candles])
            fill_time_text = next_fill_dt.strftime('%H:%M')
            bar = StockCandidateBar(
                symbol=sym, timestamp_hhmm=fill_time_text, close=s_bar.close,
                or_high=or_range.high, or_low=or_range.low, session_vwap=session_vwap,
                stock_return_20=stock_return_20, nifty_return_20=nifty_return_20,
                bar_volume=s_bar.volume, avg_volume_20=average_volume,
                bid_ask_spread_bps=4.0, upper_circuit=first_open * 1.10, lower_circuit=first_open * 0.90,
                swing_low_5=min(c.low for c in swing_bars), swing_high_5=max(c.high for c in swing_bars),
                atr14=atr_val, session_high_so_far=session_high, session_low_so_far=session_low,
                adr14_pct=0.02, bar_high=s_bar.high, bar_low=s_bar.low,
            )
            
            # Step by step through evaluate_candidate_signal:
            cfg = config.raw_dict
            sched = cfg.get("session_schedule", cfg.get("schedule", {}))
            gates = cfg.get("signal_gates", cfg.get("signal", {}))
            start_t = sched.get("entry_start_time", sched.get("entry_start", "09:35"))
            cutoff_t = sched.get("entry_cutoff_time", sched.get("entry_cutoff", "12:30"))
            if not (start_t <= bar.timestamp_hhmm <= cutoff_t):
                gates_failed['TIME_WINDOW'] = gates_failed.get('TIME_WINDOW', 0) + 1
                continue
            regime = regime_eval.regime
            regime_val = regime.value if hasattr(regime, "value") else str(regime)
            side = "LONG" if regime_val == "BULLISH" else "SHORT" if regime_val == "BEARISH" else None
            if side is None:
                gates_failed['REGIME'] = gates_failed.get('REGIME', 0) + 1
                continue
            cushion_mult = float(gates.get("min_breakout_cushion_atr", 0.15))
            min_clv = float(gates.get("min_clv_ratio", 0.70))
            cushion = cushion_mult * bar.atr14
            rel_strength = bar.stock_return_20 - bar.nifty_return_20
            
            if side == "LONG":
                bar_range = max(bar.bar_high - bar.bar_low, 0.05) if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
                clv = (bar.close - bar.bar_low) / bar_range if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
                if not (bar.close >= (bar.or_high + cushion)):
                    gates_failed['OR_CUSHION'] = gates_failed.get('OR_CUSHION', 0) + 1
                    continue
                if not (clv >= min_clv):
                    gates_failed['CLV'] = gates_failed.get('CLV', 0) + 1
                    continue
                if not (bar.close > bar.session_vwap):
                    gates_failed['VWAP'] = gates_failed.get('VWAP', 0) + 1
                    continue
                if not (rel_strength > 0):
                    gates_failed['REL_STRENGTH'] = gates_failed.get('REL_STRENGTH', 0) + 1
                    continue
            else:
                bar_range = max(bar.bar_high - bar.bar_low, 0.05) if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
                clv = (bar.bar_high - bar.close) / bar_range if (bar.bar_high > 0 and bar.bar_low > 0) else 1.0
                if not (bar.close <= (bar.or_low - cushion)):
                    gates_failed['OR_CUSHION'] = gates_failed.get('OR_CUSHION', 0) + 1
                    continue
                if not (clv >= min_clv):
                    gates_failed['CLV'] = gates_failed.get('CLV', 0) + 1
                    continue
                if not (bar.close < bar.session_vwap):
                    gates_failed['VWAP'] = gates_failed.get('VWAP', 0) + 1
                    continue
                if not (rel_strength < 0):
                    gates_failed['REL_STRENGTH'] = gates_failed.get('REL_STRENGTH', 0) + 1
                    continue
            vol_ratio = bar.bar_volume / bar.avg_volume_20
            min_vol = float(gates.get("min_volume_surge_ratio", gates.get("min_relative_volume", 2.0)))
            if vol_ratio < min_vol:
                gates_failed['VOLUME_SURGE'] = gates_failed.get('VOLUME_SURGE', 0) + 1
                continue
            min_atr = float(gates.get("min_atr_pct", 0.0022))
            if False and (bar.atr14 / bar.close) < min_atr:
                gates_failed['MIN_ATR'] = gates_failed.get('MIN_ATR', 0) + 1
                continue
            levels = compute_initial_stop_and_target(side=side, entry_price=bar.close, swing_low_5=bar.swing_low_5, swing_high_5=bar.swing_high_5, atr14=bar.atr14, cfg=cfg)
            min_r_pct = float(gates.get("min_stop_distance_pct", 0.0055))
            max_r_pct = float(gates.get("max_stop_distance_pct", 0.0090))
            if not (min_r_pct <= levels.risk_pct <= max_r_pct):
                gates_failed['STOP_WIDTH'] = gates_failed.get('STOP_WIDTH', 0) + 1
                continue
            max_adr = float(gates.get("max_adr_consumed_pct", 0.60))
            if bar.adr14_pct > 0 and bar.session_low_so_far > 0:
                intraday_range_pct = (bar.session_high_so_far - bar.session_low_so_far) / bar.session_low_so_far
                if False and (intraday_range_pct / bar.adr14_pct) > max_adr:
                    gates_failed['ADR_CONSUMED'] = gates_failed.get('ADR_CONSUMED', 0) + 1
                    continue
            gates_failed['PASSED'] = gates_failed.get('PASSED', 0) + 1

print(gates_failed)
