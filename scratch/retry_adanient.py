import os
import sys
import pandas as pd
from datetime import datetime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

REPO_ROOT = "."
from apex3.broker.angel_data_provider import AngelDataProvider

IST = ZoneInfo("Asia/Kolkata")
load_dotenv(".env", override=True)

provider = AngelDataProvider(
    api_key=os.getenv("ANGEL_API_KEY", "").strip(),
    client_code=os.getenv("ANGEL_CLIENT_CODE", "").strip(),
    pin=os.getenv("ANGEL_PIN", "").strip(),
    totp_secret=os.getenv("ANGEL_TOTP_SECRET", "").strip(),
)

start_dt = datetime(2026, 11, 18, 9, 15, tzinfo=IST)
end_dt = datetime(2026, 11, 26, 15, 30, tzinfo=IST)
candles = provider.get_historical("ADANIENT", "5min", start_dt, end_dt, "NSE")
print(f"ADANIENT: fetched {len(candles)} candles")
if candles:
    for folder in ["scratch_data", "scratch_data_2y"]:
        p = os.path.join(folder, "ADANIENT.csv")
        rows = {}
        if os.path.exists(p):
            df_old = pd.read_csv(p)
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
        df_new = pd.DataFrame(list(rows.values())).sort_values("Datetime").drop_duplicates(subset=["Datetime"]).reset_index(drop=True)
        df_new.to_csv(p, index=False)
        print(f"Saved {p}: {len(df_new)} rows")
