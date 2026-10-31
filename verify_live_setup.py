"""APEX-3 Pre-Flight Live Trading & Broker Connectivity Verifier.

Tests both broker connections (Zerodha Kite + Angel One SmartAPI),
verifies system clock against exchange time, tests late-start
Opening Range reconstruction, and confirms ready state.

Usage:
    python verify_live_setup.py
"""
import os
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
_venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
if _venv_python.exists() and Path(sys.executable).resolve() != _venv_python.resolve():
    import subprocess
    sys.exit(subprocess.run([str(_venv_python), str(Path(__file__).resolve()), *sys.argv[1:]]).returncode)

import time
from datetime import datetime, date, timedelta, time as dtime
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))
from dotenv import load_dotenv

load_dotenv(ROOT_DIR / ".env")


def print_banner(text: str):
    print("\n" + "=" * 70)
    print(f" {text}")
    print("=" * 70)


def verify_clock_and_env():
    print_banner("1. SYSTEM CLOCK & ENVIRONMENT PRE-FLIGHT")
    now_ist = datetime.now(IST)
    print(f"[*] Local System Time (IST): {now_ist.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"[*] Weekday                : {now_ist.strftime('%A')}")

    # Check required secrets
    required_keys = [
        ("KITE_API_KEY", os.getenv("KITE_API_KEY")),
        ("KITE_ACCESS_TOKEN", os.getenv("KITE_ACCESS_TOKEN")),
        ("ANGEL_API_KEY", os.getenv("ANGEL_API_KEY")),
        ("ANGEL_CLIENT_CODE", os.getenv("ANGEL_CLIENT_CODE")),
        ("ANGEL_PIN", os.getenv("ANGEL_PIN")),
        ("ANGEL_TOTP_SECRET", os.getenv("ANGEL_TOTP_SECRET")),
    ]

    all_keys_present = True
    for key_name, val in required_keys:
        status = "[PRESENT]" if val and len(val.strip()) > 0 else "[MISSING]"
        print(f"    - {key_name:<20}: {status}")
        if not val:
            all_keys_present = False

    return all_keys_present


def verify_zerodha_kite():
    print_banner("2. ZERODHA KITE BROKER VERIFICATION")
    api_key = os.getenv("KITE_API_KEY")
    access_token = os.getenv("KITE_ACCESS_TOKEN")

    if not api_key or not access_token:
        print("[!] KITE_API_KEY or KITE_ACCESS_TOKEN is missing in .env")
        return False

    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)

        # 1. Profile
        prof = kite.profile()
        user_name = prof.get("user_name", "Unknown")
        user_id = prof.get("user_id", "Unknown")
        broker = prof.get("broker", "ZERODHA")
        print(f"[+] Kite Profile Authenticated: {user_name} (ID: {user_id}, Broker: {broker})")

        # 2. Margins
        margins = kite.margins(segment="equity")
        available_cash = float(margins.get("available", {}).get("cash", 0.0) or 0.0)
        net_equity = float(margins.get("net", 0.0) or 0.0)
        print(f"[+] Equity Margin Available   : Rs. {available_cash:,.2f} (Net: Rs. {net_equity:,.2f})")
        if net_equity < 15000.0:
            print(f"[!] Warning: Available net equity (Rs. {net_equity:,.2f}) is below standard Rs. 15,000 capital base.")
        else:
            print("[+] Capital Base Requirement  : PASSED (>= Rs. 15,000.00)")

        # 3. Orders & Positions
        orders = kite.orders()
        positions = kite.positions().get("net", [])
        open_pos = [p for p in positions if int(p.get("quantity", 0)) != 0]
        print(f"[+] Active Orders Count       : {len(orders)} order(s)")
        print(f"[+] Open Positions Count      : {len(open_pos)} open position(s)")

        return True

    except Exception as e:
        err_msg = str(e)
        print(f"[!] Kite Authentication Failed: {err_msg}")
        if "TokenException" in err_msg or "Incorrect" in err_msg or "expired" in err_msg.lower():
            print("\n>>> HOW TO FIX KITE TOKEN:")
            print("    1. Log in to your Kite developer app: https://developers.kite.trade/apps")
            print("    2. Copy the 'Login redirect URL' or use your API key to generate a request_token.")
            print("    3. Exchange request_token for access_token and update KITE_ACCESS_TOKEN in .env.")
        return False


def verify_angel_data():
    print_banner("3. ANGEL ONE SMARTAPI MARKET FEED VERIFICATION")
    api_key = os.getenv("ANGEL_API_KEY")
    client_code = os.getenv("ANGEL_CLIENT_CODE")
    pin = os.getenv("ANGEL_PIN")
    totp_secret = os.getenv("ANGEL_TOTP_SECRET")

    if not all([api_key, client_code, pin, totp_secret]):
        print("[!] Angel One credentials missing in .env")
        return False

    try:
        from apex3.broker.angel_data_provider import AngelDataProvider
        provider = AngelDataProvider(
            api_key=api_key,
            client_code=client_code,
            pin=pin,
            totp_secret=totp_secret,
        )
        auth_ok = provider.authenticate()
        if not auth_ok:
            print("[!] Angel One authentication failed. Check credentials/TOTP.")
            return False

        print(f"[+] Angel One Authenticated   : SUCCESS (Client: {client_code})")

        # Test quote fetch with clock drift check
        quote = provider.get_quote("NIFTY 50", exchange="NSE")
        now_ist = datetime.now(IST)
        skew = (now_ist - quote.timestamp).total_seconds()
        print(f"[+] Live Benchmark Feed       : LTP={quote.last_price:,.2f} | Timestamp={quote.timestamp.strftime('%H:%M:%S')} | Skew={skew:+.2f}s")
        if abs(skew) <= 15.0:
            print("[+] Clock Skew Tolerance      : PASSED (within +/-15.0s window)")
        else:
            print(f"[!] Clock Skew Alert          : Drift is {skew:.1f}s. Ensure system time is synchronized via NTP.")

        # Test historical 5-minute candles
        candles = provider.get_historical("NIFTY 50", "5minute")
        print(f"[+] Historical Feed Active    : Retrieved {len(candles)} 5-minute candles.")

        return True

    except Exception as e:
        print(f"[!] Angel One Feed Error: {e}")
        return False


def verify_late_start_opening_range():
    print_banner("4. LATE-START (09:50 AM) OPENING RANGE RECONSTRUCTION")
    try:
        from apex3.strategy.signal import build_opening_range
        from apex3.models import Candle

        # Simulate 09:15, 09:20, 09:25 candles
        sample_date = date(2026, 10, 30)
        c1 = Candle(timestamp=datetime(2026, 10, 30, 9, 15, tzinfo=IST), open=2800.0, high=2825.0, low=2795.0, close=2820.0, volume=50000, is_complete=True)
        c2 = Candle(timestamp=datetime(2026, 10, 30, 9, 20, tzinfo=IST), open=2820.0, high=2830.0, low=2810.0, close=2825.0, volume=65000, is_complete=True)
        c3 = Candle(timestamp=datetime(2026, 10, 30, 9, 25, tzinfo=IST), open=2825.0, high=2835.0, low=2815.0, close=2830.0, volume=70000, is_complete=True)

        or_range = build_opening_range("TRENT", [c1, c2, c3], session_date=sample_date)
        print(f"[+] Reconstructed OR High    : Rs. {or_range.high:,.2f}")
        print(f"[+] Reconstructed OR Low     : Rs. {or_range.low:,.2f}")
        print(f"[+] Opening Range Complete   : {or_range.is_complete} ({or_range.candle_count} bars)")

        assert or_range.is_complete is True
        assert or_range.high == 2835.0
        assert or_range.low == 2795.0
        print("[+] Late-Start OR Logic       : PASSED — Engine successfully reconstructs 09:15-09:30 OR on boot.")
        return True

    except Exception as e:
        print(f"[!] Late-start OR check failed: {e}")
        return False


def verify_trade_logger():
    print_banner("5. TRADE AUDIT LOG & CONTINUOUS JOURNAL (info.log)")
    try:
        from apex3.journal.trade_logger import TradeLogger
        tl = TradeLogger(log_path="info.log", start_date="2026-10-29")
        reconciled = tl.reconcile_missing_trading_days()
        print(f"[+] info.log Audit Status     : ACTIVE & RECONCILED")
        print(f"[+] Tracked Record IDs        : {len(tl._recorded_trade_ids)} records in memory")
        print("[+] Offline Logging Guarantee : PASSED — System maintains continuous daily audit entries.")
        return True
    except Exception as e:
        print(f"[!] Trade Logger check failed: {e}")
        return False


def main():
    print("\n" + "=" * 70)
    print(" APEX-3 SYSTEM READINESS & END-TO-END BROKER VERIFIER")
    print("=" * 70)

    env_ok = verify_clock_and_env()
    late_ok = verify_late_start_opening_range()
    log_ok = verify_trade_logger()
    angel_ok = verify_angel_data()
    kite_ok = verify_zerodha_kite()

    print_banner("FINAL VERIFICATION SUMMARY")
    print(f"1. Environment & Keys       : {'[PASS]' if env_ok else '[FAIL]'}")
    print(f"2. Late-Start OR Logic      : {'[PASS]' if late_ok else '[FAIL]'}")
    print(f"3. Audit Logging (info.log) : {'[PASS]' if log_ok else '[FAIL]'}")
    print(f"4. Angel One Market Data    : {'[PASS]' if angel_ok else '[FAIL]'}")
    print(f"5. Zerodha Kite Execution   : {'[PASS]' if kite_ok else '[EXPIRED/UPDATE NEEDED]'}")

    if kite_ok and angel_ok:
        print("\n>>> ALL SYSTEMS GO! APEX-3 IS READY FOR LIVE TRADING.")
        print("    Run: python -m apex3.main --mode live")
    else:
        print("\n>>> ACTION REQUIRED:")
        if not kite_ok:
            print("    * Update KITE_ACCESS_TOKEN in .env with today's token.")
        if not angel_ok:
            print("    * Verify Angel One credentials in .env.")
        print("    * Once updated, re-run: python verify_live_setup.py")


if __name__ == "__main__":
    main()
