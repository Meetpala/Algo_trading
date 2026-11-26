import os
import sys
import pandas as pd
from datetime import date

REPO_ROOT = "."
df_nifty = pd.read_csv("scratch_data/NSEI.csv")
df_nifty["Datetime"] = pd.to_datetime(df_nifty["Datetime"])

for d in [date(2026, 11, 23), date(2026, 11, 25), date(2026, 11, 26)]:
    sub = df_nifty[df_nifty["Datetime"].dt.date == d]
    if not sub.empty:
        day_open = sub.iloc[0]["Open"]
        day_high = sub["High"].max()
        day_low = sub["Low"].min()
        day_close = sub.iloc[-1]["Close"]
        pct_change = ((day_close - day_open) / day_open) * 100.0
        day_range_pct = ((day_high - day_low) / day_low) * 100.0
        print(f"Date {d} ({d.strftime('%A')}): Open {day_open:.2f}, High {day_high:.2f}, Low {day_low:.2f}, Close {day_close:.2f} | Chg: {pct_change:+.2f}%, Range: {day_range_pct:.2f}%")
