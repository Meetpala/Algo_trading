import os, sys
sys.path.insert(0, os.getcwd())
from datetime import time, timedelta, date
import backtest_3months

config = backtest_3months.load_config()
watchlist = backtest_3months.build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = backtest_3months.load_preparsed_data(all_symbols)

target_date = date(2026, 7, 29)
nifty_df = data['NSEI']['df']
nifty_candles = data['NSEI']['candles']
n_sess_mask = (nifty_df['Datetime'].dt.date == target_date)
sess_indices = nifty_df[n_sess_mask].index.tolist()

print(f"Tracing session: {target_date}, num bars: {len(sess_indices)}")

# Check TRENT opening range
s_candles = data['TRENT']['candles']
s_df = data['TRENT']['df']
s_indices = s_df[s_df['Datetime'].dt.date == target_date].index.tolist()
or_indices = [idx for idx in s_indices if s_candles[idx].timestamp.time() < time(9, 30)]
or_high = max(s_candles[i].high for i in or_indices)
or_low = min(s_candles[i].low for i in or_indices)
print(f"TRENT OR: high={or_high}, low={or_low}")
or_range = backtest_3months.OpeningRange(symbol='TRENT', high=or_high, low=or_low, is_complete=True, candle_count=len(or_indices))

for step, n_idx in enumerate(sess_indices):
    c_bar = nifty_candles[n_idx]
    current_dt = c_bar.timestamp
    bar_time = current_dt.time()
    
    # Check TRENT bar at 10:55 or 11:00
    trent_indices = [idx for idx in s_indices if s_candles[idx].timestamp == current_dt]
    if not trent_indices: continue
    t_bar = s_candles[trent_indices[0]]
    
    if bar_time in (time(10, 50), time(10, 55), time(11, 0), time(11, 5)):
        print(f"\n--- TRENT at {current_dt} ---")
        print(f"Bar: O={t_bar.open}, H={t_bar.high}, L={t_bar.low}, C={t_bar.close}, V={t_bar.volume}")
        
        # Check NIFTY regime
        bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
        from apex3.strategy.regime import evaluate_market_regime
        regime_eval = evaluate_market_regime(bench_slice, config.regime)
        print(f"NIFTY Regime: {regime_eval.regime.value}, Reason: {regime_eval.gate.reason}")
        
        # Check signal
        today_indices = [idx for idx in s_indices if s_candles[idx].timestamp <= current_dt]
        today_candles = [s_candles[idx] for idx in today_indices]
        first_open = s_candles[s_indices[0]].open
        from apex3.models import InstrumentInfo, Quote
        inst = InstrumentInfo(symbol='TRENT', exchange="NSE", lot_size=1, tick_size=0.05, upper_circuit=first_open*1.10, lower_circuit=first_open*0.90)
        quote = Quote(symbol='TRENT', exchange="NSE", last_price=t_bar.close, bid=t_bar.close-0.05, ask=t_bar.close+0.05, volume=t_bar.volume, timestamp=current_dt, is_fresh=True)
        from apex3.strategy.signal import evaluate_signal
        sig = evaluate_signal(
            symbol='TRENT', stock_candles_5m=today_candles, benchmark_candles_5m=bench_slice,
            opening_range=or_range, market_regime=regime_eval.regime, instrument=inst,
            quote=quote, config=config.signal, is_entry_window=True
        )
        print(f"Signal Decision: {sig.decision.value}, Reason: {sig.explanation}")
        for g in sig.gates:
            if g.status.value != 'PASS':
                print(f"  Gate FAIL: {g.name}: {g.reason}")
