import sys
sys.path.insert(0, '.')
from apex3.config import load_config
from apex3.strategy.engine import build_active_radar
from apex3.models import InstrumentInfo, Quote, Side, Decision, OpeningRange
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import evaluate_signal
from apex3.strategy.trail import compute_initial_stop_and_target
from backtest_3months import load_preparsed_data
import pandas as pd
from datetime import time, date

config = load_config()
watchlist = build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = load_preparsed_data(all_symbols)

target_date = date(2026, 8, 6)
sess_mask = (data['NSEI']['df']['Datetime'].dt.date == target_date)
sess_indices = data['NSEI']['df'][sess_mask].index.tolist()

nifty_candles = data['NSEI']['candles']

# Opening range for TCS
s_candles = data['TCS']['candles']
s_df = data['TCS']['df']
s_indices = s_df[s_df['Datetime'].dt.date == target_date].index.tolist()
or_indices = [idx for idx in s_indices if s_candles[idx].timestamp.time() < time(9, 30)]
or_high = max(s_candles[i].high for i in or_indices)
or_low = min(s_candles[i].low for i in or_indices)
or_range = OpeningRange(symbol='TCS', high=or_high, low=or_low, is_complete=True, candle_count=len(or_indices))
first_open = s_candles[s_indices[0]].open
inst = InstrumentInfo(symbol='TCS', exchange="NSE", lot_size=1, tick_size=0.05, upper_circuit=first_open*1.10, lower_circuit=first_open*0.90)

print(f"2026-08-06 TCS OR: High={or_high}, Low={or_low}")

for step, n_idx in enumerate(sess_indices):
    c_bar = nifty_candles[n_idx]
    current_dt = c_bar.timestamp
    bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
    regime_eval = evaluate_market_regime(bench_slice, config.regime)
    
    # Check TCS
    tcs_cands = [c for c in s_candles if c.timestamp <= current_dt and c.timestamp.date() == target_date]
    if len(tcs_cands) < 3: continue
    s_bar = tcs_cands[-1]
    quote = Quote(symbol='TCS', exchange="NSE", last_price=s_bar.close, bid=s_bar.close-0.05, ask=s_bar.close+0.05, volume=s_bar.volume, timestamp=current_dt, is_fresh=True)
    
    sig = evaluate_signal(
        symbol='TCS', stock_candles_5m=tcs_cands, benchmark_candles_5m=bench_slice,
        opening_range=or_range, market_regime=regime_eval.regime, instrument=inst,
        quote=quote, config=config.signal, is_entry_window=True
    )
    if sig.decision != Decision.WAIT:
        print(f"Signal at {current_dt.time()}: {sig.decision.value} {sig.side}")
    else:
        # Check reasons around 12:00 - 12:30
        if time(12, 0) <= current_dt.time() <= time(12, 30):
            print(f"At {current_dt.time()}: Regime={regime_eval.regime.value}, Reason={sig.reason_codes}, Expl={sig.explanation}")
