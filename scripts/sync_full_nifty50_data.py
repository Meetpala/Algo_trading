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

def download_stock_incremental(provider, symbol, token, target_end_dt=None):
    out_dir = os.path.join(REPO_ROOT, "scratch_data")
    out_path = os.path.join(out_dir, f"{symbol}.csv")
    end_dt = target_end_dt or datetime.now(IST)
    df_old = None
    if os.path.exists(out_path):
        try:
            df_old = pd.read_csv(out_path)
            if not df_old.empty and len(df_old) >= 18000:
                last_dt = pd.to_datetime(df_old["Datetime"].iloc[-1]).to_pydatetime()
                start_dt = last_dt - timedelta(days=1)
            else:
                start_dt = datetime(2025, 10, 27, 9, 15, tzinfo=IST)
        except Exception:
            start_dt = datetime(2025, 10, 27, 9, 15, tzinfo=IST)
    else:
        start_dt = datetime(2025, 10, 27, 9, 15, tzinfo=IST)

    chunk_size = timedelta(days=30)
    curr = start_dt
    query_sym = "NIFTY 50" if symbol == "NSEI" else symbol
    provider._token_cache[query_sym] = token

    all_candles = []
    while curr < end_dt:
        nxt = min(curr + chunk_size, end_dt)
        provider._candle_cache.clear()
        try:
            c = provider.get_historical(query_sym, "5min", curr, nxt, "NSE")
            if c:
                all_candles.extend(c)
        except Exception:
            time.sleep(1.0)
        curr = nxt

    if not all_candles and df_old is not None:
        return len(df_old)

    rows = [
        {
            "Datetime": f"{c.timestamp.strftime('%Y-%m-%d %H:%M:%S')}+05:30",
            "Open": c.open,
            "High": c.high,
            "Low": c.low,
            "Close": c.close,
            "Volume": c.volume,
        }
        for c in all_candles
    ]
    df_new = pd.DataFrame(rows)
    if df_old is not None and not df_old.empty:
        df = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df = df_new
    df = df.drop_duplicates(subset=["Datetime"], keep="last").sort_values("Datetime").reset_index(drop=True)
    df.to_csv(out_path, index=False)
    return len(df)


def download_stock_year(provider, symbol, token):
    return download_stock_incremental(provider, symbol, token)

def main():
    from apex3.broker.angel_data_provider import DEFAULT_ANGEL_TOKENS
    from apex3.config import load_config
    from apex3.strategy.engine import build_active_radar

    cfg = load_config()
    radar = build_active_radar(cfg.raw_dict)
    elite_extra = ["ITC", "TCS", "SBIN", "SUNPHARMA", "HDFCBANK", "ICICIBANK", "INFY", "AXISBANK", "LT", "TITAN", "TATASTEEL", "NTPC", "POWERGRID", "WIPRO", "TECHM", "HCLTECH", "ASIANPAINT"]
    target_symbols = ["NSEI"] + sorted(set(radar + elite_extra))

    tokens = dict(DEFAULT_ANGEL_TOKENS)
    tokens["NSEI"] = DEFAULT_ANGEL_TOKENS.get("NIFTY 50", "99926000")

    provider = authenticate()
    to_download = []
    for s in target_symbols:
        p = os.path.join(REPO_ROOT, "scratch_data", f"{s}.csv")
        if not os.path.exists(p):
            to_download.append(s)
        else:
            try:
                line_count = sum(1 for _ in open(p, encoding="utf-8", errors="ignore"))
                with open(p, "rb") as f:
                    f.seek(max(0, os.path.getsize(p) - 256))
                    tail = f.read().decode("utf-8", errors="ignore").strip().splitlines()
                    last_line = tail[-1] if tail else ""
                if not last_line.startswith("2026-11-18") or line_count < 18000:
                    to_download.append(s)
            except Exception:
                to_download.append(s)

    print(f"Total instruments to incrementally refresh through latest session: {len(to_download)}")
    print(f"Instruments: {', '.join(to_download)}\n")

    for idx, s in enumerate(to_download, 1):
        tok = tokens.get(s) or provider.resolve_token("NIFTY 50" if s == "NSEI" else s, "NSE")
        if not tok:
            print(f"[{idx}/{len(to_download)}] No token for {s}, skipping")
            continue
        print(f"[{idx}/{len(to_download)}] Refreshing 5m data for {s} (token {tok})...")
        t0 = time.time()
        cnt = download_stock_incremental(provider, s, tok)
        elapsed = time.time() - t0
        print(f"  [SAVED] {s}: {cnt} candles in {elapsed:.1f}s")

    print("\n[ALL COMPLETE] Historical Dataset Synchronized through latest completed session!")

if __name__ == "__main__":
    main()

