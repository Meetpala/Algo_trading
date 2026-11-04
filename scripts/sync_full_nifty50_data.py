"""Download complete 1-Year 5-minute historical candles for all missing NIFTY 50 stocks."""
import os
import sys
import time
import urllib.request
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pandas as pd
from dotenv import load_dotenv

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from apex3.broker.angel_data_provider import AngelDataProvider

IST = ZoneInfo("Asia/Kolkata")

NIFTY_50 = [
    'ADANIENT', 'ADANIPORTS', 'APOLLOHOSP', 'ASIANPAINT', 'AXISBANK',
    'BAJAJ-AUTO', 'BAJFINANCE', 'BAJAJFINSV', 'BPCL', 'BHARTIARTL',
    'BRITANNIA', 'CIPLA', 'COALINDIA', 'DRREDDY', 'EICHERMOT',
    'GRASIM', 'HCLTECH', 'HDFCBANK', 'HDFCLIFE', 'HEROMOTOCO',
    'HINDALCO', 'HINDUNILVR', 'ICICIBANK', 'ITC', 'INDUSINDBK',
    'INFY', 'JSWSTEEL', 'KOTAKBANK', 'LT', 'M&M',
    'MARUTI', 'NTPC', 'NESTLEIND', 'ONGC', 'POWERGRID',
    'RELIANCE', 'SBILIFE', 'SHRIRAMFIN', 'SBIN', 'SUNPHARMA',
    'TCS', 'TATACONSUM', 'TATAMOTORS', 'TATASTEEL', 'TECHM',
    'TITAN', 'TRENT', 'ULTRACEMCO', 'WIPRO', 'BEL'
]

def get_token_map():
    url = 'https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json'
    print("Fetching master tokens from Angel One CDN...")
    with urllib.request.urlopen(url, timeout=15) as resp:
        data = json.loads(resp.read().decode('utf-8'))
    tokens = {}
    for item in data:
        if item.get('exch_seg') == 'NSE' and item.get('symbol', '').endswith('-EQ'):
            sym = item.get('name')
            if sym in NIFTY_50:
                tokens[sym] = str(item.get('token'))
    # Manual mappings for demerged or special symbols
    tokens['TATAMOTORS'] = '3456'  # TMPV-EQ
    return tokens

def authenticate():
    load_dotenv(os.path.join(REPO_ROOT, ".env"), override=True)
    return AngelDataProvider(
        api_key=os.getenv("ANGEL_API_KEY", "").strip(),
        client_code=os.getenv("ANGEL_CLIENT_CODE", "").strip(),
        pin=os.getenv("ANGEL_PIN", "").strip(),
        totp_secret=os.getenv("ANGEL_TOTP_SECRET", "").strip(),
    )

def download_stock_year(provider, symbol, token):
    end_dt = datetime(2026, 10, 26, 15, 30, tzinfo=IST)
    start_dt = end_dt - timedelta(days=365)
    chunk_size = timedelta(days=30)
    curr = start_dt

    all_candles = []
    while curr < end_dt:
        nxt = min(curr + chunk_size, end_dt)
        provider._candle_cache.clear()
        try:
            c = provider.get_historical(symbol, "5min", curr, nxt, "NSE")
            if c:
                all_candles.extend(c)
            time.sleep(0.35)
        except Exception as e:
            time.sleep(1.0)
        curr = nxt

    # Deduplicate
    seen = set()
    unique = []
    for c in all_candles:
        if c.timestamp not in seen:
            seen.add(c.timestamp)
            unique.append(c)
    unique.sort(key=lambda x: x.timestamp)

    # Save to scratch_data
    out_dir = os.path.join(REPO_ROOT, "scratch_data")
    out_path = os.path.join(out_dir, f"{symbol}.csv")
    rows = []
    for c in unique:
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
    return len(df)

def main():
    tokens = get_token_map()
    provider = authenticate()
    
    # Check which NIFTY 50 symbols need downloading (less than 15,000 candles)
    to_download = []
    for s in NIFTY_50:
        p = os.path.join(REPO_ROOT, "scratch_data", f"{s}.csv")
        if not os.path.exists(p):
            to_download.append(s)
        else:
            try:
                line_count = sum(1 for _ in open(p))
                if line_count < 15000:
                    to_download.append(s)
            except Exception:
                to_download.append(s)

    print(f"Total NIFTY 50 to download / refresh: {len(to_download)} stocks")
    print(f"Stocks: {', '.join(to_download)}\n")

    for idx, s in enumerate(to_download, 1):
        tok = tokens.get(s)
        if not tok:
            print(f"[{idx}/{len(to_download)}] No token for {s}, skipping")
            continue
        provider._token_cache[s] = tok
        print(f"[{idx}/{len(to_download)}] Fetching 1-year 5m data for {s} (token {tok})...")
        t0 = time.time()
        cnt = download_stock_year(provider, s, tok)
        elapsed = time.time() - t0
        print(f"  [SAVED] {s}: {cnt} candles in {elapsed:.1f}s")
        time.sleep(0.2)

    print("\n[ALL COMPLETE] Full NIFTY 50 1-Year Dataset Synchronized!")

if __name__ == "__main__":
    main()

