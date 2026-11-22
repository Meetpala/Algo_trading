import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from apex3.broker.angel_data_provider import AngelDataProvider, DEFAULT_ANGEL_TOKENS
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

out_dir_2y = os.path.join(REPO_ROOT, "scratch_data_2y")
os.makedirs(out_dir_2y, exist_ok=True)

# Date range for the prior year: 2024-11-20 to 2025-10-27
fetch_start = datetime(2024, 11, 20, 9, 15, tzinfo=IST)
fetch_end = datetime(2025, 10, 27, 15, 30, tzinfo=IST)

print(f"Targeting {len(symbols)} symbols from {fetch_start.date()} to {fetch_end.date()}...")

for idx, sym in enumerate(symbols, 1):
    token = provider.resolve_token(sym, "NSE")
    if not token:
        print(f"[{idx}/{len(symbols)}] Skipping {sym} (No token)")
        continue

    print(f"[{idx}/{len(symbols)}] Fetching 2024-2025 history for {sym} (Token: {token})...")
    curr_start = fetch_start
    chunk_size = timedelta(days=30)
    new_candles = []

    while curr_start < fetch_end:
        curr_end = min(curr_start + chunk_size, fetch_end)
        try:
            c_list = provider.get_historical(
                symbol=sym,
                interval="5min",
                from_dt=curr_start,
                to_dt=curr_end,
                exchange="NSE",
            )
            if c_list:
                new_candles.extend(c_list)
        except Exception as e:
            print(f"  [ERROR] {sym} chunk {curr_start.date()} to {curr_end.date()}: {e}")
        curr_start = curr_end
        time.sleep(0.35)  # Rate limit courtesy

    print(f"  Retrieved {len(new_candles)} older candles for {sym}")

    # Combine with existing scratch_data
    existing_path = os.path.join(REPO_ROOT, "scratch_data", f"{sym}.csv")
    out_path = os.path.join(out_dir_2y, f"{sym}.csv")

    rows = {}
    if os.path.exists(existing_path):
        df_exist = pd.read_csv(existing_path)
        for _, r in df_exist.iterrows():
            dt_key = str(r["Datetime"])
            rows[dt_key] = {
                "Datetime": dt_key,
                "Open": float(r["Open"]),
                "High": float(r["High"]),
                "Low": float(r["Low"]),
                "Close": float(r["Close"]),
                "Volume": int(r["Volume"]),
            }

    for c in new_candles:
        dt_str = f"{c.timestamp.strftime('%Y-%m-%d %H:%M:%S')}+05:30"
        rows[dt_str] = {
            "Datetime": dt_str,
            "Open": c.open,
            "High": c.high,
            "Low": c.low,
            "Close": c.close,
            "Volume": c.volume,
        }

    df_combined = pd.DataFrame(list(rows.values()))
    df_combined = df_combined.sort_values("Datetime").drop_duplicates(subset=["Datetime"]).reset_index(drop=True)
    df_combined.to_csv(out_path, index=False)
    print(f"  Combined {sym}: {len(df_combined)} candles ({df_combined['Datetime'].iloc[0][:10]} -> {df_combined['Datetime'].iloc[-1][:10]})")

print("\n[SUCCESS] All 21 symbols synchronized with full 2-year data in scratch_data_2y/!")
