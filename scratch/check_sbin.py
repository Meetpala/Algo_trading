import sys
sys.path.insert(0, '.')
from backtest_3months import load_preparsed_data
from apex3.indicators.atr import compute_atr
import pandas as pd
from datetime import time, date

data = load_preparsed_data(['SBIN'])
candles = [c for c in data['SBIN']['candles'] if c.timestamp.date() == date(2026, 6, 24)]
for i, c in enumerate(candles[:10]):
    print(f"Bar {i} {c.timestamp.time()}: O={c.open} H={c.high} L={c.low} C={c.close}")
