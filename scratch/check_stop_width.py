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

stop_too_narrow = 0
stop_too_wide = 0

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
            session_vwap = compute_session_vwap([c.high for c in today_candles], [c.low for c in today_candles], [c.close for c in today_candles], [c.volume for c in today_candles])
            
            regime = regime_eval.regime
            regime_val = regime.value if hasattr(regime, "value") else str(regime)
            side = "LONG" if regime_val == "BULLISH" else "SHORT" if regime_val == "BEARISH" else None
            if side is None: continue
            
            cushion = 0.15 * atr_val
            rel_strength = stock_return_20 - nifty_return_20
            bar_range = max(s_bar.high - s_bar.low, 0.05)
            
            if side == "LONG":
                clv = (s_bar.close - s_bar.low) / bar_range
                if not (s_bar.close >= (or_range.high + cushion) and clv >= 0.70 and s_bar.close > session_vwap and rel_strength > 0):
                    continue
            else:
                clv = (s_bar.high - s_bar.close) / bar_range
                if not (s_bar.close <= (or_range.low - cushion) and clv >= 0.70 and s_bar.close < session_vwap and rel_strength < 0):
                    continue
            vol_ratio = s_bar.volume / average_volume
            if vol_ratio < 2.0: continue
            
            levels = compute_initial_stop_and_target(
                side=side, entry_price=s_bar.close,
                swing_low_5=min(c.low for c in swing_bars),
                swing_high_5=max(c.high for c in swing_bars),
                atr14=atr_val, cfg=config.raw_dict
            )
            if levels.risk_pct < 0.0055:
                stop_too_narrow += 1
            elif levels.risk_pct > 0.0090:
                stop_too_wide += 1

print(f"Stop too narrow (< 0.55%): {stop_too_narrow}")
print(f"Stop too wide (> 0.90%): {stop_too_wide}")
