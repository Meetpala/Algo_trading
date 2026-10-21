import sys
sys.path.insert(0, '.')
import json
import subprocess
from datetime import time

# Let's inspect the 72 trades from 1dacd9b
raw_72 = subprocess.check_output(['git', 'show', '1dacd9b:three_month_results.json'], text=True)
data_72 = json.loads(raw_72)
trades_72 = data_72['trades']
print(f"Total trades in 1dacd9b: {len(trades_72)}")

# Let's count by symbol and month
from collections import Counter
sym_counts = Counter(t['symbol'] for t in trades_72)
print("Symbols in 1dacd9b:", sym_counts)

# Let's see the time distribution of entries in 1dacd9b
time_counts = Counter(t['entry_time'] for t in trades_72)
print("Entry times in 1dacd9b top 10:", time_counts.most_common(10))

# Let's see the r_multiples and exit reasons in 1dacd9b
reason_counts = Counter(t['exit_reason'] for t in trades_72)
print("Exit reasons in 1dacd9b:", reason_counts)
