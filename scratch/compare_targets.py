import sys
sys.path.insert(0, '.')
from scratch.test_1dacd9b import res as r_old
from scratch.test_pipeline_clean import simulate, all_dates
import pandas as pd

r_new = simulate(all_dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090)

df_old = pd.DataFrame(r_old['trades'])
df_new = pd.DataFrame(r_new['trade_list'])

print(f"Old trades: {len(df_old)}, New trades: {len(df_new)}")
old_targets = df_old[df_old['exit_reason'].str.contains('TARGET')]
print(f"\nOld TARGET_HIT trades ({len(old_targets)}):")
for _, t in old_targets.iterrows():
    # Check if this trade was taken in new
    match = df_new[(df_new['date'] == t['date']) & (df_new['symbol'] == t['symbol'])]
    if not match.empty:
        m = match.iloc[0]
        print(f"MATCH: {t['date']} {t['symbol']:10s} OldExit: {t['exit_reason']} OldNet: {t['net_pnl']:6.2f} | NewExit: {m['exit_reason']} NewNet: {m['net_pnl']:6.2f} NewR: {m['r_multiple']}")
    else:
        print(f"MISSED: {t['date']} {t['symbol']:10s} OldExit: {t['exit_reason']} OldNet: {t['net_pnl']:6.2f} (Not taken in New)")
