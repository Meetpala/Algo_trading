"""Angel One SmartAPI 6-Month 5-Minute Historical Candle Fetcher for APEX-3.

Pulls 6 months (~120 trading sessions) of 5-minute historical candles for the
audited Elite 10 institutional trend leaders + NIFTY 50 into scratch_data/.
"""
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from dotenv import load_dotenv

# Ensure repo root is on sys.path
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from apex3.broker.angel_data_provider import AngelDataProvider, DEFAULT_ANGEL_TOKENS

IST = ZoneInfo("Asia/Kolkata")
ELITE_10 = [
    "HINDALCO",
    "JSWSTEEL",
    "RELIANCE",
    "MARUTI",
    "ITC",
    "TCS",
    "SBIN",
    "TRENT",
    "KOTAKBANK",
    "SUNPHARMA",
]


def check_and_authenticate() -> AngelDataProvider:
    env_path = os.path.join(REPO_ROOT, ".env")
    load_dotenv(env_path, override=True)

    api_key = os.getenv("ANGEL_API_KEY", "").strip()
    client_code = os.getenv("ANGEL_CLIENT_CODE", "").strip()
    pin = os.getenv("ANGEL_PIN", "").strip()
    totp_secret = os.getenv("ANGEL_TOTP_SECRET", "").strip()

    placeholders = {
        "your_angel_api_key_here",
        "your_angel_client_code_here",
        "your_angel_pin_here",
        "your_angel_totp_secret_here",
        "",
    }

    missing = []
    if api_key in placeholders:
        missing.append("ANGEL_API_KEY")
    if client_code in placeholders:
        missing.append("ANGEL_CLIENT_CODE")
    if pin in placeholders:
        missing.append("ANGEL_PIN")
    if totp_secret in placeholders:
        missing.append("ANGEL_TOTP_SECRET")

    if missing:
        print("=" * 65)
        print("  ANGEL ONE CREDENTIALS MISSING OR UNMODIFIED IN .env")
        print("=" * 65)
        print(f"The following keys in '{env_path}' are still placeholders or empty:")
        for m in missing:
            print(f"  - {m}")
        print("\nPlease update lines 19-22 in your .env file and press Ctrl+S to save.")
        sys.exit(1)

    print(f"Connecting to Angel One SmartAPI for Client: {client_code}...")
    provider = AngelDataProvider(
        api_key=api_key,
        client_code=client_code,
        pin=pin,
        totp_secret=totp_secret,
    )

    if not provider._auth_token:
        print("[ERROR] Angel One SmartAPI authentication failed. Check your API Key, PIN, and TOTP Secret.")
        sys.exit(1)

    print("[SUCCESS] Angel One SmartAPI authenticated successfully!\n")
    return provider


def fetch_symbol_chunks(provider: AngelDataProvider, symbol: str, token: str, days: int = 180):
    """Download 5-minute candles in 30-day chunks to adhere to API limits."""
    end_dt = datetime.now(IST)
    start_dt = end_dt - timedelta(days=days)

    all_candles = []
    chunk_start = start_dt
    chunk_size = timedelta(days=30)

    while chunk_start < end_dt:
        chunk_end = min(chunk_start + chunk_size, end_dt)
        try:
            candles = provider.get_historical(
                symbol=symbol,
                interval="5min",
                from_dt=chunk_start,
                to_dt=chunk_end,
                exchange="NSE",
            )
            if candles:
                all_candles.extend(candles)
            time.sleep(0.35)  # Respect rate limits (3 requests/sec)
        except Exception as e:
            print(f"  Warning fetching {symbol} chunk {chunk_start.date()} to {chunk_end.date()}: {e}")
        chunk_start = chunk_end

    # Deduplicate by timestamp
    seen = set()
    unique_candles = []
    for c in all_candles:
        if c.timestamp not in seen:
            seen.add(c.timestamp)
            unique_candles.append(c)

    unique_candles.sort(key=lambda x: x.timestamp)
    return unique_candles


def save_candles_to_csv(symbol: str, candles: list):
    out_dir = os.path.join(REPO_ROOT, "scratch_data")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{symbol}.csv")

    rows = []
    for c in candles:
        rows.append({
            "Datetime": c.timestamp.strftime("%Y-%m-%d %H:%M:%S%z"),
            "Open": c.open,
            "High": c.high,
            "Low": c.low,
            "Close": c.close,
            "Volume": c.volume,
        })

    df = pd.DataFrame(rows)
    df.to_csv(out_path, index=False)
    print(f"Saved {symbol}: {len(df)} candles ({df['Datetime'].iloc[0][:10]} -> {df['Datetime'].iloc[-1][:10]})")


def main():
    provider = check_and_authenticate()
    symbols_to_fetch = [("NSEI", "26000")] + [(s, DEFAULT_ANGEL_TOKENS[s]) for s in ELITE_10]

    print(f"Downloading 6-month 5m candles for {len(symbols_to_fetch)} instruments...")
    for sym, token in symbols_to_fetch:
        print(f"Fetching {sym} (Token: {token})...")
        candles = fetch_symbol_chunks(provider, sym, token, days=180)
        if candles:
            save_candles_to_csv(sym, candles)
        else:
            print(f"  [WARN] No candles returned for {sym}")

    print("\nDownload completed! Ready for 6-month simulation.")


if __name__ == "__main__":
    main()
