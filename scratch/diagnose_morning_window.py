import sys
sys.path.insert(0, '.')
from datetime import time, timedelta
from apex3.config import load_config
from apex3.models import Side, OpeningRange
from apex3.strategy.engine import build_active_radar
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import StockCandidateBar, evaluate_candidate_signal
from apex3.indicators.atr import compute_atr
from apex3.indicators.vwap import compute_session_vwap
from backtest_3months import load_preparsed_data
from collections import Counter

config = load_config()
watchlist = build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = load_preparsed_data(all_symbols)

nifty_df = data['NSEI']['df']
nifty_candles = data['NSEI']['candles']
dates = sorted(list(set(nifty_df['Datetime'].dt.date)))[-57:]

gate_fails = Counter()

for session_date in dates:
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
        bar_time = current_dt.time()
        bar_close_dt = current_dt + timedelta(minutes=5)
        
        # Check morning window: bar_close_dt from 09:35 to 10:15
        if not (time(9, 35) <= bar_close_dt.time() <= time(10, 15)):
            continue
            
        bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
        regime_eval = evaluate_market_regime(bench_slice, config.regime)
        if regime_eval.regime == MarketRegime.NEUTRAL:
            gate_fails['NIFTY_REGIME_NEUTRAL'] += 1
            continue
            
        next_n_idx = sess_indices[step + 1] if step + 1 < len(sess_indices) else None
        if next_n_idx is None: continue
        next_fill_dt = nifty_candles[next_n_idx].timestamp
        
        for sym in watchlist:
            s_idx = stock_sess_map[sym].get(current_dt)
            next_s_idx = stock_sess_map[sym].get(next_fill_dt)
            if s_idx is None or next_s_idx is None:
                gate_fails['MISSING_BAR'] += 1
                continue
                
            s_bar = data[sym]['candles'][s_idx]
            today_indices = [idx for idx in stock_sess_indices[sym] if data[sym]['candles'][idx].timestamp <= current_dt]
            today_candles = [data[sym]['candles'][idx] for idx in today_indices]
            if len(today_candles) < 4:
                gate_fails['LESS_THAN_4_CANDLES'] += 1
                continue
                
            or_range = opening_ranges[sym]
            if not or_range.is_complete:
                gate_fails['OR_INCOMPLETE'] += 1
                continue
                
            first_open = data[sym]['candles'][stock_sess_indices[sym][0]].open
            history_start = max(0, s_idx - 60)
            stock_history = data[sym]['candles'][history_start:s_idx + 1]
            atr_series = compute_atr([c.high for c in stock_history], [c.low for c in stock_history], [c.close for c in stock_history], period=14)
            if not atr_series: continue
            atr_val = atr_series[-1]
            if atr_val <= 0: continue
            
            prior_volume_bars = data[sym]['candles'][max(0, s_idx - 20):s_idx]
            if len(prior_volume_bars) < 20:
                gate_fails['VOLUME_BARS_LESS_THAN_20'] += 1
                continue
            average_volume = sum(c.volume for c in prior_volume_bars) / len(prior_volume_bars)
            if average_volume <= 0: continue
            
            stock_closes = [c.close for c in stock_history[-20:]]
            bench_closes = [c.close for c in bench_slice[-20:]]
            if len(stock_closes) < 20 or len(bench_closes) < 20: continue
            stock_return_20 = (stock_closes[-1] - stock_closes[0]) / stock_closes[0] * 100.0
            nifty_return_20 = (bench_closes[-1] - bench_closes[0]) / bench_closes[0] * 100.0
            lookback_bars = 5
            swing_bars = stock_history[-lookback_bars:]
            session_high = max(c.high for c in today_candles)
            session_low = min(c.low for c in today_candles)
            session_vwap = compute_session_vwap([c.high for c in today_candles], [c.low for c in today_candles], [c.close for c in today_candles], [c.volume for c in today_candles])
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
            # Detailed check of candidate signal
            # 1. Gate 0: [09:35, 12:30]
            if not ("09:35" <= candidate_bar.timestamp_hhmm <= "12:30"):
                gate_fails['GATE_0_TIME'] += 1
                continue
            
            # Gate 1 & 2 & 4
            cushion = 0.15 * candidate_bar.atr14
            min_clv = 0.70
            rel_strength = candidate_bar.stock_return_20 - candidate_bar.nifty_return_20
            side = 'LONG' if regime_eval.regime == MarketRegime.BULLISH else 'SHORT'
            
            bar_range = max(candidate_bar.bar_high - candidate_bar.bar_low, 0.05)
            if side == 'LONG':
                clv = (candidate_bar.close - candidate_bar.bar_low) / bar_range
                if candidate_bar.close < (candidate_bar.or_high + cushion):
                    gate_fails['LONG_NO_OR_BREAKOUT'] += 1
                    continue
                if clv < min_clv:
                    gate_fails['LONG_WEAK_CLV'] += 1
                    continue
                if candidate_bar.close <= candidate_bar.session_vwap:
                    gate_fails['LONG_BELOW_VWAP'] += 1
                    continue
                if rel_strength <= 0:
                    gate_fails['LONG_WEAK_REL_STRENGTH'] += 1
                    continue
            else:
                clv = (candidate_bar.bar_high - candidate_bar.close) / bar_range
                if candidate_bar.close > (candidate_bar.or_low - cushion):
                    gate_fails['SHORT_NO_OR_BREAKOUT'] += 1
                    continue
                if clv < min_clv:
                    gate_fails['SHORT_WEAK_CLV'] += 1
                    continue
                if candidate_bar.close >= candidate_bar.session_vwap:
                    gate_fails['SHORT_ABOVE_VWAP'] += 1
                    continue
                if rel_strength >= 0:
                    gate_fails['SHORT_WEAK_REL_STRENGTH'] += 1
                    continue
                    
            vol_ratio = candidate_bar.bar_volume / candidate_bar.avg_volume_20
            if vol_ratio < 2.0:
                gate_fails['VOLUME_SURGE_FAIL'] += 1
                continue
                
            from apex3.strategy.trail import compute_initial_stop_and_target
            levels = compute_initial_stop_and_target(side, candidate_bar.close, candidate_bar.swing_low_5, candidate_bar.swing_high_5, candidate_bar.atr14, config.raw_dict)
            if not (0.0055 <= levels.risk_pct <= 0.0090):
                gate_fails['STOP_WIDTH_FAIL'] += 1
                continue
                
            gate_fails['SIGNAL_PASSED!'] += 1

print("\n--- GATE REJECTIONS (09:35 - 10:15) ---")
for k, v in gate_fails.most_common(20):
    print(f"{k:30}: {v}")
