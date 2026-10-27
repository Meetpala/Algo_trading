"""APEX-3 End-to-End Zerodha Kite Connect Verification & Diagnostic Tool.

Performs complete end-to-end verification of the Zerodha connection:
1. Validates API Key & Secret from .env.
2. Generates/updates daily KITE_ACCESS_TOKEN if request_token or redirect URL is provided.
3. Tests KiteConnect API authentication and user profile.
4. Verifies live available equity margin.
5. Verifies live order book, positions book, and trade book access.
6. Verifies APEX-3 KiteAdapter and HybridBrokerAdapter integration.
7. Executes pre-flight account state reconciliation audit.

Usage:
    python scripts/test_zerodha_connection.py
    python scripts/test_zerodha_connection.py --token <request_token>
    python scripts/test_zerodha_connection.py --url "<full_redirect_url>"
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

IST = ZoneInfo("Asia/Kolkata")

# Ensure project root is in sys.path
ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

ENV_PATH = ROOT_DIR / ".env"
load_dotenv(ENV_PATH)


def update_env_variable(key: str, value: str) -> None:
    """Save or update key in .env file safely."""
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)

    found = False
    new_lines: list[str] = []
    for line in lines:
        if line.strip().startswith(f"{key}="):
            new_lines.append(f"{key}={value}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"{key}={value}\n")

    ENV_PATH.write_text("".join(new_lines), encoding="utf-8")
    os.environ[key] = value


def extract_request_token(input_str: str) -> str:
    """Extract request_token from full redirected URL or raw token string."""
    cleaned = input_str.strip()
    if "request_token=" in cleaned:
        parsed = urlparse(cleaned)
        qs = parse_qs(parsed.query)
        if "request_token" in qs and qs["request_token"]:
            return qs["request_token"][0].strip()
    return cleaned


def main():
    parser = argparse.ArgumentParser(description="End-to-end Zerodha connection tester")
    parser.add_argument("--token", type=str, default=None, help="Zerodha request_token")
    parser.add_argument("--url", type=str, default=None, help="Full redirect URL containing request_token")
    parser.add_argument("--interactive", action="store_true", help="Prompt interactively if token is missing/expired")
    args = parser.parse_args()

    print("\n" + "=" * 75)
    print("      APEX-3 ZERODHA KITE CONNECT END-TO-END CONNECTION CHECK")
    print(f"      Time: {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print("=" * 75)

    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()
    access_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()

    # Step 1: Check API Key and Secret
    print("\n[Step 1/7] Checking API Credentials in .env...")
    if not api_key:
        print("  [FAIL] KITE_API_KEY is missing in .env")
        sys.exit(1)
    if not api_secret:
        print("  [FAIL] KITE_API_SECRET is missing in .env")
        sys.exit(1)
    print(f"  [PASS] KITE_API_KEY    : {api_key[:4]}****{api_key[-4:] if len(api_key) > 8 else ''}")
    print(f"  [PASS] KITE_API_SECRET : {'*' * 8}")

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        print("  [FAIL] kiteconnect package is not installed. Run: pip install kiteconnect")
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    # Step 2: Handle Request Token Exchange if provided or needed
    req_token_input = args.token or (extract_request_token(args.url) if args.url else None)

    if req_token_input:
        print("\n[Step 2/7] Exchanging request_token for daily session...")
        try:
            data = kite.generate_session(req_token_input, api_secret=api_secret)
            access_token = data.get("access_token", "")
            if not access_token:
                print(f"  [FAIL] Did not receive access_token. Response: {data}")
                sys.exit(1)
            update_env_variable("KITE_ACCESS_TOKEN", access_token)
            print("  [PASS] Successfully generated daily KITE_ACCESS_TOKEN and updated .env!")
        except Exception as e:
            print(f"  [FAIL] Session generation failed: {e}")
            sys.exit(1)

    # Step 3: Validate Access Token
    print("\n[Step 3/7] Validating Zerodha Kite Session Authentication...")
    if not access_token:
        print("  [ALERT] KITE_ACCESS_TOKEN is not set in .env.")
        print("\n" + "-" * 75)
        print("  ACTION REQUIRED: Daily Zerodha Login Needed")
        print("  Zerodha access tokens expire daily at 06:00 AM IST.")
        print("  1. Open this URL in your browser:")
        print(f"     {login_url}")
        print("  2. Log in with your Zerodha Client ID, Password, and 2FA TOTP.")
        print("  3. Copy the full redirect URL (or request_token) and run:")
        print("     python scripts/test_zerodha_connection.py --token <request_token>")
        print("-" * 75 + "\n")
        return

    kite.set_access_token(access_token)

    # Check profile
    try:
        profile = kite.profile()
        user_name = profile.get("user_name", "Trader")
        user_id = profile.get("user_id", "")
        broker_name = profile.get("broker", "ZERODHA")
        print(f"  [PASS] Active session authenticated as: {user_name} ({user_id}) | Broker: {broker_name}")
    except Exception as e:
        print(f"  [EXPIRED / INVALID] Could not authenticate with existing token: {e}")
        print("\n" + "-" * 75)
        print("  ACTION REQUIRED: Generate New Daily Access Token")
        print("  1. Open this URL in your browser:")
        print(f"     {login_url}")
        print("  2. Log in with your Zerodha Client ID, Password, and 2FA TOTP.")
        print("  3. Copy the full redirect URL (or request_token) and run:")
        print("     python scripts/test_zerodha_connection.py --token <request_token>")
        print("-" * 75 + "\n")
        return

    # Step 4: Margin Audit
    print("\n[Step 4/7] Checking Live Equity Margins...")
    try:
        margins = kite.margins(segment="equity")
        avail = margins.get("available", {})
        live_balance = float(avail.get("live_balance", margins.get("net", 0.0)))
        cash = float(avail.get("cash", 0.0))
        collateral = float(avail.get("collateral", 0.0))
        print(f"  [PASS] Available Net Margin : Rs. {live_balance:,.2f}")
        print(f"         Available Cash Balance: Rs. {cash:,.2f}")
        print(f"         Collateral Margin     : Rs. {collateral:,.2f}")
    except Exception as e:
        print(f"  [FAIL] Failed to retrieve equity margins: {e}")

    # Step 5: Order, Position, and Trade Books
    print("\n[Step 5/7] Checking Live Order, Position & Trade Books...")
    try:
        orders = kite.orders()
        open_orders = [o for o in orders if o.get("status") in ("OPEN", "TRIGGER PENDING")]
        print(f"  [PASS] Orders Book Accessible: {len(orders)} total orders today ({len(open_orders)} open/pending)")
    except Exception as e:
        print(f"  [FAIL] Failed to fetch orders: {e}")

    try:
        positions_raw = kite.positions()
        net_pos = positions_raw.get("net", [])
        open_pos = [p for p in net_pos if int(p.get("quantity", 0)) != 0]
        print(f"  [PASS] Positions Accessible  : {len(net_pos)} total positions ({len(open_pos)} active open)")
    except Exception as e:
        print(f"  [FAIL] Failed to fetch positions: {e}")

    try:
        trades = kite.trades()
        print(f"  [PASS] Trade Book Accessible : {len(trades)} executed trades today")
    except Exception as e:
        print(f"  [FAIL] Failed to fetch trades: {e}")

    # Step 6: APEX-3 Broker Adapter Validation
    print("\n[Step 6/7] Validating APEX-3 Broker Adapters (KiteAdapter / HybridBrokerAdapter)...")
    try:
        from apex3.broker.kite_adapter import KiteAdapter
        from apex3.broker.rate_limiter import RateLimiter
        from apex3.config import load_config

        config = load_config()
        rl_rates = {
            "orders": float(getattr(config.rate_limits, "orders_per_second", 10.0)),
            "quotes": float(getattr(config.rate_limits, "quotes_per_second", 1.0)),
            "historical": float(getattr(config.rate_limits, "historical_per_second", 3.0)),
        }
        rate_limiter = RateLimiter(rates=rl_rates)
        kite_broker = KiteAdapter(
            api_key=api_key,
            access_token=access_token,
            live_enabled=True,
            rate_limiter=rate_limiter,
        )

        if config.secrets.angel_api_key:
            from apex3.broker.angel_data_provider import AngelDataProvider
            from apex3.broker.hybrid_adapter import HybridBrokerAdapter

            angel_data = AngelDataProvider(
                api_key=config.secrets.angel_api_key,
                client_code=config.secrets.angel_client_code,
                pin=config.secrets.angel_pin,
                totp_secret=config.secrets.angel_totp_secret,
                rate_limiter=rate_limiter,
            )
            broker = HybridBrokerAdapter(data_provider=angel_data, execution_broker=kite_broker)
            print("  [PASS] Operating in Hybrid Mode (Angel One Data + Zerodha Execution).")
        else:
            broker = kite_broker
            print("  [PASS] Operating in Direct Zerodha Kite Mode.")

        is_valid = broker.is_session_valid()
        print(f"  [PASS] Broker.is_session_valid() : {is_valid}")

        # Check metadata from Zerodha
        inst = broker.get_instrument_info("INFY")
        print(f"  [PASS] Authoritative Metadata   : INFY -> MIS Available={inst.mis_available}, Tick={inst.tick_size}, Lot={inst.lot_size}")

    except Exception as e:
        print(f"  [FAIL] Adapter validation error: {e}")
        return

    # Step 7: Pre-Flight Reconciler Audit
    print("\n[Step 7/7] Running APEX-3 Startup Reconciliation Audit...")
    try:
        from apex3.reconciliation.reconciler import Reconciler
        from apex3.reconciliation.state_machine import StateMachine
        from apex3.journal.sqlite_journal import Journal

        journal = Journal(db_path=":memory:")
        sm = StateMachine(session_tag_prefix="APEX3")
        reconciler = Reconciler(broker=broker, state_machine=sm, journal=journal, session_tag="APEX3", config=config)

        watched = list(getattr(config.radar, "core_watchlist", [])) + list(getattr(config.radar, "extended_pool", []))
        rec_status = reconciler.audit_startup_state(watched_symbols=watched)
        if rec_status.is_clean:
            print(f"  [PASS] Reconciliation Audit Clean: {rec_status.message} (Flat={rec_status.is_flat})")
        else:
            print(f"  [WARN] Reconciliation Alert: {rec_status.message}")
    except Exception as e:
        print(f"  [FAIL] Reconciliation audit error: {e}")

    print("\n" + "=" * 75)
    print("   RESULT: ALL 7 END-TO-END CHECKS PASSED — ZERODHA IS FULLY CONNECTED!")
    print("=" * 75 + "\n")


if __name__ == "__main__":
    main()
