import sys
sys.path.insert(0, '.')
import json
from scratch.test_pipeline_run import run_simulation
from collections import Counter

# Modify run_simulation slightly to return trades
import scratch.test_pipeline_run as tpr

def inspect():
    # Let's inspect trades
    # We can inspect scratch/analyze_trade_mfe.py which already inspected trades!
    pass

if __name__ == '__main__':
    with open('scratch/analyze_trade_mfe.py', 'r') as f:
        print(f.read()[:1000])
