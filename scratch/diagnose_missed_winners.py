import sys
sys.path.insert(0, '.')
from scratch.test_1dacd9b import res as r_old
from apex3.config import load_config
from backtest_3months import load_preparsed_data, evaluate_market_regime, MarketRegime, compute_atr, compute_session_vwap, OpeningRange
from apex3.strategy.signal import evaluate_signal
from apex3.models import InstrumentInfo, Quote, Side, Decision
from datetime import time, timedelta, datetime
import pandas as pd

config = load_config()
data = load_preparsed_data(['NSEI', 'RELIANCE', 'ITC', 'JSWSTEEL', 'TCS'])

missed_dates_syms = [
    ('2026-08-05', 'RELIANCE'),
    ('2026-08-18', 'ITC'),
    ('2026-08-19', 'ITC'),
    ('2026-09-24', 'RELIANCE'),
    ('2026-10-01', 'JSWSTEEL'),
    ('2026-10-08', 'JSWSTEEL'),
    ('2026-10-13', 'TCS'),
]

df_old = pd.DataFrame(r_old['trades'])

for dt_str, sym in missed_dates_syms:
    old_t = df_old[(df_old['date'] == dt_str) & (df_old['symbol'] == sym)].iloc[0]
    entry_t_str = old_t['entry_time']
    print(f"\nAnalyzing {dt_str} {sym} (Old Entry: {entry_t_str}, Old Exit: {old_t['exit_reason']}, Net: {old_t['net_pnl']}):")
    
    # Check candles around that entry time
    dt = pd.to_datetime(dt_str).date()
    nifty_df = data['NSEI']['df']
    n_sess_mask = (nifty_df['Datetime'].dt.date == dt)
    sess_indices = nifty_df[n_sess_mask].index.tolist()
    
    # OR
    s_candles = data[sym]['candles']
    s_df = data[sym]['df']
    s_indices = s_df[s_df['Datetime'].dt.date == dt].index.tolist()
    or_indices = [idx for idx in s_indices if s_candles[idx].timestamp.time() < time(9, 30)]
    or_high = max(s_candles[i].high for i in or_indices)
    or_low = min(s_candles[i].low for i in or_indices)
    or_range = OpeningRange(symbol=sym, high=or_high, low=or_low, is_complete=True, candle_count=len(or_indices))
    first_open = s_candles[s_indices[0]].open
    inst = InstrumentInfo(symbol=sym, exchange="NSE", lot_size=1, tick_size=0.05, upper_circuit=first_open*1.10, lower_circuit=first_open*0.90)
    
    # Check step around entry time
    for n_idx in sess_indices:
        c_bar = data['NSEI']['candles'][n_idx]
        cur_t = c_bar.timestamp.time()
        bar_close_t = (c_bar.timestamp + timedelta(minutes=5)).time()
        
        # Check regime
        bench_slice = data['NSEI']['candles'][max(0, n_idx - 100): n_idx + 1]
        regime_eval = evaluate_market_regime(bench_slice, config.regime)
        
        # Check symbol
        s_cand = [c for c in s_candles if c.timestamp <= c_bar.timestamp and c.timestamp.date() == dt]
        if len(s_cand) < 5: continue
        s_bar = s_cand[-1]
        quote = Quote(symbol=sym, exchange="NSE", last_price=s_bar.close, bid=s_bar.close-0.05, ask=s_bar.close+0.05, volume=s_bar.volume, timestamp=c_bar.timestamp, is_fresh=True)
        
        sig = evaluate_signal(
            symbol=sym, stock_candles_5m=s_cand, benchmark_candles_5m=bench_slice,
            opening_range=or_range, market_regime=regime_eval.regime, instrument=inst,
            quote=quote, config=config.signal, is_entry_window=True
        )
        
        # If this is within 10 minutes of old entry time:
        entry_t = pd.to_datetime(entry_t_str).time()
        if abs((datetime.combine(dt, cur_t) - datetime.combine(dt, entry_t)).total_seconds()) <= 600:
            print(f"  At {cur_t} (close {bar_close_t}): Regime={regime_eval.regime.value}, SigDec={sig.decision.value}, Reasons={sig.reason_codes}, Expl={sig.explanation}")
