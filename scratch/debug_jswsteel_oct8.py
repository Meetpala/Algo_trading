import sys
sys.path.insert(0, '.')
from apex3.config import load_config
from backtest_3months import load_preparsed_data, evaluate_market_regime, MarketRegime, compute_atr, compute_session_vwap, OpeningRange
from apex3.strategy.signal import evaluate_signal
from apex3.strategy.trail import compute_initial_stop_and_target
from apex3.models import InstrumentInfo, Quote, Side, Decision
from datetime import time, timedelta, date

config = load_config()
data = load_preparsed_data(['NSEI', 'JSWSTEEL', 'HINDALCO'])
dt = date(2026, 10, 8)
nifty_candles = data['NSEI']['candles']
df = data['NSEI']['df']
sess_indices = df[df['Datetime'].dt.date == dt].index.tolist()

for n_idx in sess_indices:
    c_bar = nifty_candles[n_idx]
    if time(10, 40) <= c_bar.timestamp.time() <= time(11, 20):
        bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
        regime_eval = evaluate_market_regime(bench_slice, config.regime)
        
        # Check JSWSTEEL
        s_cands = [c for c in data['JSWSTEEL']['candles'] if c.timestamp <= c_bar.timestamp and c.timestamp.date() == dt]
        or_cands = [c for c in data['JSWSTEEL']['candles'] if c.timestamp.date() == dt and c.timestamp.time() < time(9, 30)]
        or_range = OpeningRange('JSWSTEEL', max(c.high for c in or_cands), min(c.low for c in or_cands), True, len(or_cands))
        inst = InstrumentInfo('JSWSTEEL', exchange="NSE", lot_size=1, tick_size=0.05, upper_circuit=1500, lower_circuit=1000)
        s_bar = s_cands[-1]
        quote = Quote('JSWSTEEL', exchange="NSE", last_price=s_bar.close, bid=s_bar.close-0.05, ask=s_bar.close+0.05, volume=s_bar.volume, timestamp=c_bar.timestamp, is_fresh=True)
        
        sig = evaluate_signal('JSWSTEEL', s_cands, bench_slice, or_range, regime_eval.regime, inst, quote, config.signal)
        
        # Check Stop Width
        stock_history = data['JSWSTEEL']['candles'][:data['JSWSTEEL']['candles'].index(s_bar)+1]
        atr_series = compute_atr([c.high for c in stock_history[-60:]], [c.low for c in stock_history[-60:]], [c.close for c in stock_history[-60:]], 14)
        atr_val = atr_series[-1]
        swing_bars = s_cands[-5:]
        levels = compute_initial_stop_and_target(
            side=Side.SELL, entry_price=s_bar.close,
            swing_low_5=min(c.low for c in swing_bars), swing_high_5=max(c.high for c in swing_bars),
            atr14=atr_val, cfg=config.raw_dict
        )
        print(f"Time: {c_bar.timestamp.time()} | Regime: {regime_eval.regime.value} | Sig: {sig.decision.value} {sig.reason_codes} | R_pct: {levels.risk_pct*100:.3f}% (0.55-0.90%) | ATR: {atr_val:.2f}")
