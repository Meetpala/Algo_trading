import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from apex3.broker.angel_data_provider import AngelDataProvider

IST = ZoneInfo("Asia/Kolkata")
load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)

provider = AngelDataProvider(
    api_key=os.getenv("ANGEL_API_KEY", "").strip(),
    client_code=os.getenv("ANGEL_CLIENT_CODE", "").strip(),
    pin=os.getenv("ANGEL_PIN", "").strip(),
    totp_secret=os.getenv("ANGEL_TOTP_SECRET", "").strip(),
)

# Test 2 years back for RELIANCE (token 2885)
now = datetime.now(IST)
dt_2y = now - timedelta(days=730)
dt_2y_end = dt_2y + timedelta(days=15)

print(f"Testing Angel One SmartAPI 5min data from {dt_2y.date()} to {dt_2y_end.date()}...")
try:
    candles = provider.get_historical(
        symbol="RELIANCE",
        interval="5min",
        from_dt=dt_2y,
        to_dt=dt_2y_end,
        exchange="NSE",
    )
    print(f"Returned {len(candles)} candles from 2 years ago.")
    if candles:
        print(f"First candle: {candles[0].timestamp}, Last candle: {candles[-1].timestamp}")
except Exception as e:
    print(f"Error fetching 2-year data: {e}")
