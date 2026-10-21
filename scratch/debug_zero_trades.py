import os, sys
sys.path.insert(0, os.getcwd())
import backtest_3months

config = backtest_3months.load_config()
watchlist = backtest_3months.build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = backtest_3months.load_preparsed_data(all_symbols)
three_month_dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))[-57:]

print(f"Watchlist: {watchlist}")
print(f"Num test dates: {len(three_month_dates)}")

# Let's inspect adr_by_symbol
adr_by_symbol = {}
import pandas as pd
for sym in watchlist:
    df = data[sym]['df'].copy()
    df['session_date'] = df['Datetime'].dt.date
    daily = df.groupby('session_date', sort=True).agg(
        high=('High', 'max'),
        low=('Low', 'min'),
        close=('Close', 'last'),
    )
    prior_close = daily['close'].shift(1)
    daily_ranges = (daily['high'] - daily['low']) / prior_close
    adr_values = daily_ranges.rolling(14, min_periods=1).mean().shift(1)
    adr_by_symbol[sym] = {
        date: (float(value) if pd.notna(value) else 0.0)
        for date, value in adr_values.items()
    }
    sample_adr = [adr_by_symbol[sym][d] for d in three_month_dates[:5]]
    print(f"{sym} first 5 ADRs: {sample_adr}")

# Now let's trace signals in run_fast_backtest
pending_entries = 0
signals_evaluated = 0
signals_passed = 0
fills_attempted = 0
fills_succeeded = 0

# Let's run a instrumented loop over test dates
for session_date in three_month_dates:
    nifty_df = data['NSEI']['df']
    nifty_candles = data['NSEI']['candles']
    n_sess_mask = (nifty_df['Datetime'].dt.date == session_date)
    sess_indices = nifty_df[n_sess_mask].index.tolist()
    if len(sess_indices) < 6: continue

    cutoff_time = config.schedule.get_time("entry_cutoff")
    
    # Opening ranges
    opening_ranges = {}
    stock_sess_map = {}
    stock_sess_indices = {}
    from datetime import time, timedelta
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

    pending_entry = None
    open_positions = {}
    for step, n_idx in enumerate(sess_indices):
        c_bar = nifty_candles[n_idx]
        current_dt = c_bar.timestamp
        bar_close_dt = current_dt + timedelta(minutes=5)
        
        if pending_entry is not None:
            pending = pending_entry
            pending_entry = None
            fill_dt = pending['fill_time']
            sym = pending['symbol']
            s_idx = stock_sess_map[sym].get(current_dt)
            fills_attempted += 1
            print(f"Attempting fill for {sym} at {current_dt}, fill_dt={fill_dt}, s_idx={s_idx}")
            s_bar = data[sym]['candles'][s_idx]
            acceptable = backtest_3months.is_entry_fill_acceptable(pending['signal_price'], s_bar.open, config.entry.max_slippage_pct)
            print(f"  Slippage acceptable: {acceptable} (sig={pending['signal_price']}, open={s_bar.open})")
            
            side = pending['side']
            entry_price = round(s_bar.open + 0.05 if side == backtest_3months.Side.BUY else s_bar.open - 0.05, 2)
            from apex3.strategy.trail import compute_initial_stop_and_target
            levels = compute_initial_stop_and_target(
                side=side, entry_price=entry_price,
                swing_low_5=pending['swing_low_5'], swing_high_5=pending['swing_high_5'],
                atr14=pending['atr'], cfg=config.raw_dict
            )
            stop_pct = levels.risk_pct
            atr_pct = pending['atr'] / entry_price if entry_price > 0 else 0.0
            adr14 = adr_by_symbol[sym].get(session_date, 0.0)
            fill_session_high = max(pending['session_high'], s_bar.open)
            fill_session_low = min(pending['session_low'], s_bar.open)
            range_used = (fill_session_high - fill_session_low) / fill_session_low if fill_session_low > 0 else float('inf')
            adr_consumed = range_used / adr14 if adr14 > 0 else float('inf')
            gate9_pass = (
                adr14 > 0
                and atr_pct >= 0.0022
                and 0.0055 <= stop_pct <= 0.0090
                and adr_consumed <= 0.60
            )
            print(f"  Gate 9 pass: {gate9_pass} (adr14={adr14}, atr_pct={atr_pct}, stop_pct={stop_pct}, adr_consumed={adr_consumed})")
            if gate9_pass:
                quantity = backtest_3months.size_backtest_quantity(
                    entry_price=entry_price, stop_price=levels.initial_stop, side=side,
                    slot_budget=28500.0, risk_cap=120.0, target_r=2.0, costs=config.costs, spread_bps=4.0
                )
                print(f"  Quantity sized: {quantity}")
                if quantity > 0:
                    fills_succeeded += 1

        next_n_idx = sess_indices[step + 1] if step + 1 < len(sess_indices) else None
        if (
            next_n_idx is not None
            and len(open_positions) < 2
            and backtest_3months.is_before_entry_cutoff(nifty_candles[next_n_idx].timestamp.time(), cutoff_time)
            and bar_close_dt.time() >= time(9, 35)
        ):
            bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
            from apex3.strategy.regime import evaluate_market_regime, MarketRegime
            regime_eval = evaluate_market_regime(bench_slice, config.regime)
            if regime_eval.regime == MarketRegime.NEUTRAL:
                continue

            cands = []
            next_fill_dt = nifty_candles[next_n_idx].timestamp
            for sym in watchlist:
                s_idx = stock_sess_map[sym].get(current_dt)
                next_s_idx = stock_sess_map[sym].get(next_fill_dt)
                if s_idx is None or next_s_idx is None:
                    continue
                s_bar = data[sym]['candles'][s_idx]
                today_indices = [idx for idx in stock_sess_indices[sym] if data[sym]['candles'][idx].timestamp <= current_dt]
                today_candles = [data[sym]['candles'][idx] for idx in today_indices]
                if len(today_candles) < 5: continue
                or_range = opening_ranges[sym]
                if not or_range.is_complete: continue
                first_open = data[sym]['candles'][stock_sess_indices[sym][0]].open
                history_start = max(0, s_idx - 60)
                stock_history = data[sym]['candles'][history_start:s_idx + 1]
                from apex3.indicators.atr import compute_atr
                atr_series = compute_atr([c.high for c in stock_history], [c.low for c in stock_history], [c.close for c in stock_history], period=14)
                if not atr_series: continue
                atr_val = atr_series[-1]
                if atr_val <= 0: continue
                prior_volume_bars = data[sym]['candles'][max(0, s_idx - 20):s_idx]
                if len(prior_volume_bars) < 20: continue
                average_volume = sum(c.volume for c in prior_volume_bars) / len(prior_volume_bars)
                if average_volume <= 0: continue
                adr14 = adr_by_symbol[sym].get(session_date, 0.0)
                if adr14 <= 0: continue
                stock_closes = [c.close for c in stock_history[-20:]]
                bench_closes = [c.close for c in bench_slice[-20:]]
                if len(stock_closes) < 20 or len(bench_closes) < 20: continue
                stock_return_20 = (stock_closes[-1] - stock_closes[0]) / stock_closes[0] * 100.0
                nifty_return_20 = (bench_closes[-1] - bench_closes[0]) / bench_closes[0] * 100.0
                swing_bars = today_candles[-5:]
                session_high = max(c.high for c in today_candles)
                session_low = min(c.low for c in today_candles)
                from apex3.indicators.vwap import compute_session_vwap
                session_vwap = compute_session_vwap([c.high for c in today_candles], [c.low for c in today_candles], [c.close for c in today_candles], [c.volume for c in today_candles])
                fill_time_text = next_fill_dt.strftime('%H:%M')
                candidate_bar = backtest_3months.StockCandidateBar(
                    symbol=sym, timestamp_hhmm=fill_time_text, close=s_bar.close,
                    or_high=or_range.high, or_low=or_range.low, session_vwap=session_vwap,
                    stock_return_20=stock_return_20, nifty_return_20=nifty_return_20,
                    bar_volume=s_bar.volume, avg_volume_20=average_volume,
                    bid_ask_spread_bps=4.0, upper_circuit=first_open * 1.10, lower_circuit=first_open * 0.90,
                    swing_low_5=min(c.low for c in swing_bars), swing_high_5=max(c.high for c in swing_bars),
                    atr14=atr_val, session_high_so_far=session_high, session_low_so_far=session_low,
                    adr14_pct=adr14, bar_high=s_bar.high, bar_low=s_bar.low,
                )
                signals_evaluated += 1
                signal = backtest_3months.evaluate_candidate_signal(
                    bar=candidate_bar, regime=regime_eval.regime, active_positions_count=len(open_positions), cfg=config.raw_dict
                )
                if signal is None:
                    # Let's see which gate returned None in evaluate_candidate_signal
                    sched = config.raw_dict.get("session_schedule", config.raw_dict.get("schedule", {}))
                    gates = config.raw_dict.get("signal_gates", config.raw_dict.get("signal", {}))
                    start_t = sched.get("entry_start_time", sched.get("entry_start", "09:35"))
                    cutoff_t = sched.get("entry_cutoff_time", sched.get("entry_cutoff", "12:30"))
                    if start_t <= candidate_bar.timestamp_hhmm <= cutoff_t:
                        regime_val = regime_eval.regime.value
                        side = "LONG" if regime_val == "BULLISH" else "SHORT"
                        cushion_mult = float(gates.get("min_breakout_cushion_atr", 0.15))
                        min_clv = float(gates.get("min_clv_ratio", 0.70))
                        cushion = cushion_mult * candidate_bar.atr14
                        rel_strength = candidate_bar.stock_return_20 - candidate_bar.nifty_return_20
                        if side == "LONG":
                            bar_range = max(candidate_bar.bar_high - candidate_bar.bar_low, 0.05) if (candidate_bar.bar_high > 0 and candidate_bar.bar_low > 0) else 1.0
                            clv = (candidate_bar.close - candidate_bar.bar_low) / bar_range if (candidate_bar.bar_high > 0 and candidate_bar.bar_low > 0) else 1.0
                            or_ok = candidate_bar.close >= (candidate_bar.or_high + cushion)
                            clv_ok = clv >= min_clv
                            vwap_ok = candidate_bar.close > candidate_bar.session_vwap
                            rs_ok = rel_strength > 0
                        else:
                            bar_range = max(candidate_bar.bar_high - candidate_bar.bar_low, 0.05) if (candidate_bar.bar_high > 0 and candidate_bar.bar_low > 0) else 1.0
                            clv = (candidate_bar.bar_high - candidate_bar.close) / bar_range if (candidate_bar.bar_high > 0 and candidate_bar.bar_low > 0) else 1.0
                            or_ok = candidate_bar.close <= (candidate_bar.or_low - cushion)
                            clv_ok = clv >= min_clv
                            vwap_ok = candidate_bar.close < candidate_bar.session_vwap
                            rs_ok = rel_strength < 0
                        vol_ratio = candidate_bar.bar_volume / candidate_bar.avg_volume_20
                        if or_ok and clv_ok and vwap_ok and rs_ok and vol_ratio >= 2.0:
                            from apex3.strategy.trail import compute_initial_stop_and_target
                            lvls = compute_initial_stop_and_target(
                                side=side, entry_price=candidate_bar.close, swing_low_5=candidate_bar.swing_low_5,
                                swing_high_5=candidate_bar.swing_high_5, atr14=candidate_bar.atr14, cfg=config.raw_dict
                            )
                            intraday_range_pct = (candidate_bar.session_high_so_far - candidate_bar.session_low_so_far) / candidate_bar.session_low_so_far
                            atr_p = candidate_bar.atr14 / candidate_bar.close
                            reasons = []
                            if candidate_bar.bid_ask_spread_bps > float(gates.get('max_spread_bps', 4.0)): reasons.append(f"SPREAD({candidate_bar.bid_ask_spread_bps})")
                            if atr_p < float(gates.get('min_atr_pct', 0.0022)): reasons.append(f"ATR({atr_p:.4f}<0.0022)")
                            if not (float(gates.get('min_stop_distance_pct', 0.0055)) <= lvls.risk_pct <= float(gates.get('max_stop_distance_pct', 0.0090))):
                                reasons.append(f"RISK_PCT({lvls.risk_pct:.4f} not in [0.0055, 0.0090])")
                            if (intraday_range_pct / candidate_bar.adr14_pct) > float(gates.get('max_adr_consumed_pct', 0.60)):
                                reasons.append(f"ADR({intraday_range_pct / candidate_bar.adr14_pct:.2f}>0.60)")
                            print(f"{sym} at {current_dt} failed: {', '.join(reasons)}")

                if signal is not None:
                    signals_passed += 1
                    cands.append({
                        'symbol': sym,
                        'side': backtest_3months.Side.BUY if signal.side == "LONG" else backtest_3months.Side.SELL,
                        'signal_price': s_bar.close,
                        'signal_time': bar_close_dt,
                        'fill_time': next_fill_dt,
                        'swing_low_5': candidate_bar.swing_low_5,
                        'swing_high_5': candidate_bar.swing_high_5,
                        'atr': atr_val,
                        'session_high': session_high,
                        'session_low': session_low,
                        'conviction': signal.conviction_score,
                    })
            if cands:
                cands.sort(key=lambda x: x['conviction'], reverse=True)
                pending_entry = cands[0]
                pending_entries += 1

print(f"Summary: signals_evaluated={signals_evaluated}, signals_passed={signals_passed}, pending_entries={pending_entries}, fills_attempted={fills_attempted}, fills_succeeded={fills_succeeded}")
