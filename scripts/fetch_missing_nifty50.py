"""Fetch 1-year 5m candles for the 26 missing NIFTY 50 constituents via Angel One SmartAPI."""
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

from apex3.broker.angel_data_provider import AngelDataProvider

IST = ZoneInfo("Asia/Kolkata")

MISSING_NIFTY_50 = [
    'ADANIENT', 'APOLLOHOSP', 'BAJAJFINSV', 'BPCL', 'BRITANNIA',
    'CIPLA', 'COALINDIA', 'DRREDDY', 'EICHERMOT', 'GRASIM',
    'HCLTECH', 'HDFCLIFE', 'HEROMOTOCO', 'INDUSINDBK', 'NTPC',
    'NESTLEIND', 'ONGC', 'POWERGRID', 'SBILIFE', 'SHRIRAMFIN',
    'TATACONSUM', 'TATAMOTORS', 'TECHM', 'TITAN', 'ULTRACEMCO',
    'WIPRO'
]

def authenticate() -> AngelDataProvider:
    load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)
    provider = AngelDataProvider(
        api_key=os.getenv("ANGEL_API_KEY", "").strip(),
        client_code=os.getenv("ANGEL_CLIENT_CODE", "").strip(),
        pin=os.getenv("ANGEL_PIN", "").strip(),
        totp_secret=os.getenv("ANGEL_TOTP_SECRET", "").strip(),
    )
    if not provider._auth_token:
        print("[ERROR] Failed to authenticate Angel One!")
        sys.exit(1)
    print("[SUCCESS] Angel One Authenticated!")
    return provider

def fetch_symbol(provider: AngelDataProvider, symbol: str, token: str, days: int = 365):
    end_dt = datetime(2026, 10, 27, 15, 30, tzinfo=IST)
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
            time.sleep(0.35)
        except Exception as e:
            time.sleep(1.0)
        chunk_start = chunk_end

    # Deduplicate
    seen = set()
    unique = []
    for c in all_candles:
        if c.timestamp not in seen:
            seen.add(c.timestamp)
            unique.append(c)

    unique.sort(key=lambda x: x.timestamp)
    return unique

def save_csv(symbol: str, candles: list):
    out_dir = os.path.join(REPO_ROOT, "scratch_data")
    out_path = os.path.join(out_dir, f"{symbol}.csv")
    rows = []
    for c in candles:
        rows.append({
            "Datetime": f"{c.timestamp.strftime('%Y-%m-%d %H:%M:%S')}+05:30",
            "Open": c.open,
            "High": c.high,
            "Low": c.low,
            "Close": c.close,
            "Volume": c.volume,
        })
    df = pd.DataFrame(rows)
    df = df.sort_values("Datetime").reset_index(drop=True)
    df.to_csv(out_path, index=False)
    print(f"  [SAVED] {symbol}: {len(df)} candles ({df['Datetime'].iloc[0][:10]} to {df['Datetime'].iloc[-1][:10]})")

def main():
    provider = authenticate()
    print(f"Targeting {len(MISSING_NIFTY_50)} missing NIFTY 50 constituents...")

    for i, sym in enumerate(MISSING_NIFTY_50, 1):
        token = provider.resolve_token(sym, "NSE")
        if not token:
            print(f"[{i}/{len(MISSING_NIFTY_50)}] Token not found for {sym}")
            continue
        print(f"[{i}/{len(MISSING_NIFTY_50)}] Fetching 1-year 5m data for {sym} (token {token})...")
        candles = fetch_symbol(provider, sym, token, days=365)
        if candles:
            save_csv(sym, candles)
        else:
            print(f"  [WARN] 0 candles for {sym}")
        time.sleep(0.2)

    print("\n[COMPLETE] All missing NIFTY 50 historical data synchronized!")

if __name__ == "__main__":
    main()

