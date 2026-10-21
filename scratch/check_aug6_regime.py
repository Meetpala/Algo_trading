import sys
sys.path.insert(0, '.')
from apex3.config import load_config
from apex3.strategy.regime import evaluate_market_regime
from backtest_3months import load_preparsed_data
from datetime import time, date

config = load_config()
data = load_preparsed_data(['NSEI'])
target_date = date(2026, 8, 6)
nifty_candles = data['NSEI']['candles']

for idx, c in enumerate(nifty_candles):
    if c.timestamp.date() == target_date and c.timestamp.time() == time(12, 20):
        bench_slice = nifty_candles[max(0, idx - 100): idx + 1]
        ev = evaluate_market_regime(bench_slice, config.regime)
        print("Regime:", ev.regime)
        print("Gate reason:", ev.gate.reason)
        print("Data:", ev.gate.data)
