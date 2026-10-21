import sys
sys.path.insert(0, '.')
from apex3.config import load_config
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from backtest_3months import load_preparsed_data
import pandas as pd
from datetime import time

config = load_config()
data = load_preparsed_data(['NSEI'])
nifty_candles = data['NSEI']['candles']
df = data['NSEI']['df']
all_dates = sorted(set(df['Datetime'].dt.date))
three_m_dates = all_dates[-57:]

regimes = []
for idx, c in enumerate(nifty_candles):
    if c.timestamp.date() in three_m_dates:
        if time(9, 35) <= c.timestamp.time() <= time(12, 30):
            bench_slice = nifty_candles[max(0, idx - 100): idx + 1]
            ev = evaluate_market_regime(bench_slice, config.regime)
            regimes.append(ev.regime.value)

print("Regime distribution during 09:35 - 12:30 in 3-Month:")
print(pd.Series(regimes).value_counts())
