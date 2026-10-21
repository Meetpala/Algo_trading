import sys
sys.path.insert(0, '.')
from backtest_3months import load_preparsed_data
from datetime import time, date

data = load_preparsed_data(['HINDALCO'])
cands = [c for c in data['HINDALCO']['candles'] if c.timestamp.date() == date(2026, 10, 8)]
for c in cands:
    if time(11, 0) <= c.timestamp.time() <= time(12, 0):
        print(f"{c.timestamp.time()}: O={c.open} H={c.high} L={c.low} C={c.close}")
