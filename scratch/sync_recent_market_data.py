import os
import sys
import time
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
import pandas as pd
from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from apex3.broker.angel_data_provider import AngelDataProvider
from apex3.config import load_config
from apex3.strategy.engine import build_active_radar

IST = ZoneInfo("Asia/Kolkata")
load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)

provider = AngelDataProvider(
    api_key=os.getenv("ANGEL_API_KEY", "").strip(),
    client_code=os.getenv("ANGEL_CLIENT_CODE", "").strip(),
    pin=os.getenv("ANGEL_PIN", "").strip(),
    totp_secret=os.getenv("ANGEL_TOTP_SECRET", "").strip(),
)

cfg = load_config()
watchlist = build_active_radar(cfg.raw_dict)
symbols = ["NSEI"] + watchlist

start_dt = datetime(2026, 11, 18, 9, 15, tzinfo=IST)
end_dt = datetime(2026, 11, 26, 15, 30, tzinfo=IST)

print(f"Syncing candles from {start_dt} to {end_dt} for {len(symbols)} symbols...")

for sym in symbols:
    token = provider.resolve_token(sym, "NSE")
    if not token:
        print(f"Skipping {sym} (No token)")
        continue
    try:
        candles = provider.get_historical(
            symbol=sym,
            interval="5min",
            from_dt=start_dt,
            to_dt=end_dt,
            exchange="NSE",
        )
        print(f"{sym}: fetched {len(candles)} candles")
        if candles:
            # Update scratch_data/{sym}.csv
            path = os.path.join(REPO_ROOT, "scratch_data", f"{sym}.csv")
            rows = {}
            if os.path.exists(path):
                df_old = pd.read_csv(path)
                for _, r in df_old.iterrows():
                    rows[str(r["Datetime"])] = {
                        "Datetime": str(r["Datetime"]),
                        "Open": float(r["Open"]),
                        "High": float(r["High"]),
                        "Low": float(r["Low"]),
                        "Close": float(r["Close"]),
                        "Volume": int(r["Volume"]),
                    }
            for c in candles:
                dt_str = f"{c.timestamp.strftime('%Y-%m-%d %H:%M:%S')}+05:30"
                rows[dt_str] = {
                    "Datetime": dt_str,
                    "Open": c.open,
                    "High": c.high,
                    "Low": c.low,
                    "Close": c.close,
                    "Volume": c.volume,
                }
            df_new = pd.DataFrame(list(rows.values()))
            df_new = df_new.sort_values("Datetime").drop_duplicates(subset=["Datetime"]).reset_index(drop=True)
            df_new.to_csv(path, index=False)
            
            # Also update scratch_data_2y
            path_2y = os.path.join(REPO_ROOT, "scratch_data_2y", f"{sym}.csv")
            if os.path.exists(os.path.dirname(path_2y)):
                df_new.to_csv(path_2y, index=False)
    except Exception as e:
        print(f"Error {sym}: {e}")
    time.sleep(0.35)

print("\n[SUCCESS] Recent candles synchronized!")
