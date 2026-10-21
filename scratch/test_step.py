import os, sys
sys.path.insert(0, os.getcwd())
from datetime import time, timedelta
from backtest_3months import load_preparsed_data, build_active_radar, load_config
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import StockCandidateBar, evaluate_candidate_signal
from apex3.indicators.atr import compute_atr
from apex3.indicators.vwap import compute_session_vwap
import backtest_3months

config = load_config()
watchlist = build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = load_preparsed_data(all_symbols)
three_month_dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))[-57:]
nifty_df = data['NSEI']['df']
nifty_candles = data['NSEI']['candles']

# Let's inspect the first day that has trending regime
found = False
for session_date in three_month_dates:
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
            opening_ranges[sym] = backtest_3months.OpeningRange(symbol=sym, high=or_high, low=or_low, is_complete=True, candle_count=len(or_indices))
        else:
            opening_ranges[sym] = backtest_3months.OpeningRange(symbol=sym, high=0.0, low=0.0, is_complete=False)

    for step, n_idx in enumerate(sess_indices):
        c_bar = nifty_candles[n_idx]
        current_dt = c_bar.timestamp
        bar_close_dt = current_dt + timedelta(minutes=5)
        next_n_idx = sess_indices[step + 1] if step + 1 < len(sess_indices) else None
        if next_n_idx is None: continue
        next_fill_dt = nifty_candles[next_n_idx].timestamp
        
        bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
        regime_eval = evaluate_market_regime(bench_slice, config.regime)
        if regime_eval.regime == MarketRegime.NEUTRAL: continue
        
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
            if len(stock_closes) < 20 or len(bench_closes) < 20: continue
            stock_return_20 = (stock_closes[-1] - stock_closes[0]) / stock_closes[0] * 100.0
            nifty_return_20 = (bench_closes[-1] - bench_closes[0]) / bench_closes[0] * 100.0
            swing_bars = today_candles[-5:]
            session_high = max(c.high for c in today_candles)
            session_low = min(c.low for c in today_candles)
            session_vwap = compute_session_vwap(
                [c.high for c in today_candles], [c.low for c in today_candles],
                [c.close for c in today_candles], [c.volume for c in today_candles]
            )
            fill_time_text = next_fill_dt.strftime('%H:%M')
            candidate_bar = StockCandidateBar(
                symbol=sym,
                timestamp_hhmm=fill_time_text,
                close=s_bar.close,
                or_high=or_range.high,
                or_low=or_range.low,
                session_vwap=session_vwap,
                stock_return_20=stock_return_20,
                nifty_return_20=nifty_return_20,
                bar_volume=s_bar.volume,
                avg_volume_20=average_volume,
                bid_ask_spread_bps=4.0,
                upper_circuit=first_open * 1.10,
                lower_circuit=first_open * 0.90,
                swing_low_5=min(c.low for c in swing_bars),
                swing_high_5=max(c.high for c in swing_bars),
                atr14=atr_val,
                session_high_so_far=session_high,
                session_low_so_far=session_low,
                adr14_pct=0.02,
                bar_high=s_bar.high,
                bar_low=s_bar.low,
            )

            # Let's inspect each line of evaluate_candidate_signal
            cfg = config.raw_dict
            sched = cfg.get("session_schedule", cfg.get("schedule", {}))
            gates = cfg.get("signal_gates", cfg.get("signal", {}))
            risk_cfg = cfg.get("risk_management", cfg.get("risk", {}))

            start_t = sched.get("entry_start_time", sched.get("entry_start", "09:35"))
            cutoff_t = sched.get("entry_cutoff_time", sched.get("entry_cutoff", "12:30"))
            if not (start_t <= candidate_bar.timestamp_hhmm <= cutoff_t):
                continue

            max_slots = int(risk_cfg.get("max_concurrent_positions", 2))
            regime_val = regime_eval.regime.value
            side = "LONG" if regime_val == "BULLISH" else "SHORT"
            cushion_mult = float(gates.get("min_breakout_cushion_atr", 0.15))
            min_clv = float(gates.get("min_clv_ratio", 0.70))
            cushion = cushion_mult * candidate_bar.atr14
            rel_strength = candidate_bar.stock_return_20 - candidate_bar.nifty_return_20
            
            if side == "LONG":
                bar_range = max(candidate_bar.bar_high - candidate_bar.bar_low, 0.05) if (candidate_bar.bar_high > 0 and candidate_bar.bar_low > 0) else 1.0
                clv = (candidate_bar.close - candidate_bar.bar_low) / bar_range if (candidate_bar.bar_high > 0 and candidate_bar.bar_low > 0) else 1.0
                g1 = candidate_bar.close >= (candidate_bar.or_high + cushion)
                g1b = clv >= min_clv
                g2 = candidate_bar.close > candidate_bar.session_vwap
                g4 = rel_strength > 0
                if not (g1 and g1b and g2 and g4):
                    continue
            else:
                bar_range = max(candidate_bar.bar_high - candidate_bar.bar_low, 0.05) if (candidate_bar.bar_high > 0 and candidate_bar.bar_low > 0) else 1.0
                clv = (candidate_bar.bar_high - candidate_bar.close) / bar_range if (candidate_bar.bar_high > 0 and candidate_bar.bar_low > 0) else 1.0
                g1 = candidate_bar.close <= (candidate_bar.or_low - cushion)
                g1b = clv >= min_clv
                g2 = candidate_bar.close < candidate_bar.session_vwap
                g4 = rel_strength < 0
                if not (g1 and g1b and g2 and g4):
                    continue

            vol_ratio = candidate_bar.bar_volume / candidate_bar.avg_volume_20
            min_vol = float(gates.get("min_volume_surge_ratio", gates.get("min_relative_volume", 2.0)))
            if vol_ratio < min_vol:
                continue

            print("PASSED UP TO VOL! Now testing rest...")
            from apex3.strategy.trail import compute_initial_stop_and_target
            levels = compute_initial_stop_and_target(
                side=side,
                entry_price=candidate_bar.close,
                swing_low_5=candidate_bar.swing_low_5,
                swing_high_5=candidate_bar.swing_high_5,
                atr14=candidate_bar.atr14,
                cfg=cfg,
            )
            min_r_pct = float(gates.get("min_stop_distance_pct", 0.0055))
            max_r_pct = float(gates.get("max_stop_distance_pct", 0.0090))
            print("Testing each gate in evaluate_candidate_signal directly:")
            print(f"start_t={start_t}, bar.timestamp_hhmm={candidate_bar.timestamp_hhmm}, cutoff_t={cutoff_t}")
            print(f"session window check: {start_t <= candidate_bar.timestamp_hhmm <= cutoff_t}")
            print(f"active_positions_count=0 < max_slots={max_slots}")
            print(f"regime_val={regime_val}")
            print(f"side={side}, cushion={cushion}, min_clv={min_clv}")
            print(f"g1={g1}, g1b={g1b}, g2={g2}, g4={g4}")
            print(f"vol_ratio={vol_ratio}, min_vol={min_vol}")
            print(f"bar.bid_ask_spread_bps={candidate_bar.bid_ask_spread_bps}, max_sprd={float(gates.get('max_spread_bps', 4.0))}")
            min_circuit_dist = float(gates.get("min_circuit_distance_pct", 0.020))
            if min_circuit_dist > 0.5: min_circuit_dist /= 100.0
            print(f"circuit dist check: side={side}, min={min_circuit_dist}")
            min_atr = float(gates.get("min_atr_pct", 0.0022))
            if (candidate_bar.atr14 / candidate_bar.close) < min_atr:
                print(f"FAILED ATR_PCT: {candidate_bar.atr14 / candidate_bar.close} < {min_atr}")
                continue
            if not (min_r_pct <= levels.risk_pct <= max_r_pct):
                print(f"FAILED RISK_PCT: {levels.risk_pct} not in [{min_r_pct}, {max_r_pct}]")
                continue
            max_adr = float(gates.get("max_adr_consumed_pct", 0.60))
            intraday_range_pct = (candidate_bar.session_high_so_far - candidate_bar.session_low_so_far) / candidate_bar.session_low_so_far
            if (intraday_range_pct / candidate_bar.adr14_pct) > max_adr:
                print(f"FAILED ADR: {intraday_range_pct / candidate_bar.adr14_pct} > {max_adr}")
                continue
            sig = evaluate_candidate_signal(candidate_bar, regime_eval.regime, 0, cfg)
            print(f"EVALUATE_CANDIDATE_SIGNAL RETURNED: {sig}")
            found = True
            break
        if found: break
    if found: break
