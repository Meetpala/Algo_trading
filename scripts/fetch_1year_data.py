"""Download 1-Year 5-Minute Historical Candle Data for APEX-3 Active Radar + Benchmark."""
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


def check_and_authenticate() -> AngelDataProvider:
    env_path = os.path.join(REPO_ROOT, ".env")
    load_dotenv(env_path, override=True)

    api_key = os.getenv("ANGEL_API_KEY", "").strip()
    client_code = os.getenv("ANGEL_CLIENT_CODE", "").strip()
    pin = os.getenv("ANGEL_PIN", "").strip()
    totp_secret = os.getenv("ANGEL_TOTP_SECRET", "").strip()

    provider = AngelDataProvider(
        api_key=api_key,
        client_code=client_code,
        pin=pin,
        totp_secret=totp_secret,
    )
    if not provider._auth_token:
        print("[ERROR] Angel One authentication failed!")
        sys.exit(1)
    print("[SUCCESS] Angel One authenticated successfully!")
    return provider


def fetch_symbol_chunks(provider: AngelDataProvider, symbol: str, token: str, days: int = 365):
    """Download 5-minute candles in 30-day chunks."""
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
            time.sleep(0.35)
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

    rows = {}
    if os.path.exists(out_path):
        try:
            old_df = pd.read_csv(out_path)
            for _, r in old_df.iterrows():
                dt_key = str(r["Datetime"])
                rows[dt_key] = {
                    "Datetime": dt_key,
                    "Open": float(r["Open"]),
                    "High": float(r["High"]),
                    "Low": float(r["Low"]),
                    "Close": float(r["Close"]),
                    "Volume": int(r["Volume"]),
                }
        except Exception as err:
            print(f"  Note: error reading old file {out_path}: {err}")

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

    df = pd.DataFrame(list(rows.values()))
    df = df.sort_values("Datetime").reset_index(drop=True)
    df.to_csv(out_path, index=False)
    print(f"Saved {symbol}: {len(df)} candles ({df['Datetime'].iloc[0][:10]} -> {df['Datetime'].iloc[-1][:10]})")


def main():
    provider = check_and_authenticate()
    cfg = load_config()
    watchlist = build_active_radar(cfg.raw_dict)
    
    # Priority symbols: Benchmark + Active Radar Universe
    primary_symbols = ["NSEI"] + watchlist
    
    # Secondary symbols: Other known tokens in DEFAULT_ANGEL_TOKENS
    secondary_symbols = [s for s in DEFAULT_ANGEL_TOKENS.keys() if s not in primary_symbols and not s.startswith("^") and s != "NIFTY" and s != "NIFTY 50"]

    all_to_fetch = primary_symbols + secondary_symbols
    print(f"Targeting {len(all_to_fetch)} instruments for 1-year historical fetch (365 days)...")
    print(f"Primary Active Radar ({len(primary_symbols)}): {', '.join(primary_symbols)}")

    for i, sym in enumerate(all_to_fetch, 1):
        token = provider.resolve_token(sym, "NSE")
        if not token:
            print(f"[{i}/{len(all_to_fetch)}] Skipping {sym} (No token)")
            continue
        print(f"[{i}/{len(all_to_fetch)}] Fetching 365 days for {sym} (Token: {token})...")
        candles = fetch_symbol_chunks(provider, sym, token, days=365)
        if candles:
            save_candles_to_csv(sym, candles)
        else:
            print(f"  [WARN] No candles returned for {sym}")

    print("\n[COMPLETE] 1-Year Historical Data downloaded and synchronized in scratch_data/.")


if __name__ == "__main__":
    main()
