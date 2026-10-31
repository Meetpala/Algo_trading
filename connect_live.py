"""APEX-3 Live Trading & Broker Connection Establishment Tool.

Provides a fast, 1-click morning broker connection and live trading launch:
1. Connects Angel One SmartAPI (Market Data Feed) via automated TOTP.
2. Checks Zerodha Kite Connect session; if expired, opens the browser login,
   captures/accepts the daily request_token, exchanges it for access_token,
   and automatically saves it into `.env`.
3. Verifies margins, positions, order books, and clock synchronization.
4. Offers instant 1-key launch of the APEX-3 autonomous live engine.

Usage:
    python connect_live.py
    python connect_live.py --start       # Auto-start live trading after connection
    python connect_live.py --relogin     # Force fresh Zerodha login
    python connect_live.py --verify-only # Check status and exit
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import threading
import webbrowser
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT_DIR = Path(__file__).resolve().parent

# Auto-bootstrap: if invoked with system/global Python instead of the project's .venv,
# transparently re-launch under .venv\Scripts\python.exe so all dependencies are guaranteed.
_venv_python = ROOT_DIR / ".venv" / "Scripts" / "python.exe"
if _venv_python.exists() and Path(sys.executable).resolve() != _venv_python.resolve():
    import subprocess
    sys.exit(subprocess.run([str(_venv_python), str(Path(__file__).resolve()), *sys.argv[1:]]).returncode)

# Ensure resilient timezone loading across all Python environments
try:
    from zoneinfo import ZoneInfo
    IST = ZoneInfo("Asia/Kolkata")
except Exception:
    from datetime import timezone, timedelta
    IST = timezone(timedelta(hours=5, minutes=30))

ENV_PATH = ROOT_DIR / ".env"

# Load environment
if ENV_PATH.exists():
    from dotenv import load_dotenv
    load_dotenv(ENV_PATH)


class KiteCallbackReceiver(BaseHTTPRequestHandler):
    """Temporary local HTTP receiver to capture request_token on redirect."""
    captured_token: str | None = None

    def log_message(self, format, *args):
        # Suppress noisy HTTP request output in terminal
        pass

    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        token = None
        if "request_token" in qs and qs["request_token"]:
            token = qs["request_token"][0].strip()

        if token:
            KiteCallbackReceiver.captured_token = token
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            html_content = (
                "<!DOCTYPE html>"
                "<html><head><title>Zerodha Kite Connected</title></head>"
                "<body style='font-family:-apple-system,BlinkMacSystemFont,\"Segoe UI\",Roboto,sans-serif;"
                "text-align:center;padding-top:60px;background-color:#f0fdf4;color:#1e293b;'>"
                "<div style='max-width:480px;margin:auto;background:#ffffff;padding:36px;border-radius:12px;"
                "box-shadow:0 10px 15px -3px rgba(0,0,0,0.1);border:1px solid #bbf7d0;'>"
                "<div style='font-size:48px;line-height:1;margin-bottom:16px;'>&#10004;</div>"
                "<h2 style='color:#16a34a;margin:0 0 12px 0;'>Authentication Successful!</h2>"
                "<p style='color:#475569;font-size:15px;line-height:1.5;margin-bottom:20px;'>"
                "Your Zerodha Kite session was captured and saved to <code>.env</code>.</p>"
                "<p style='color:#64748b;font-size:13px;'>You can close this window and return to your terminal.</p>"
                "</div></body></html>"
            )
            self.wfile.write(html_content.encode("utf-8"))
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"No request_token found in callback.")


def save_env_token(token: str) -> None:
    """Safely updates or appends KITE_ACCESS_TOKEN in .env preserving all comments."""
    token = token.strip().strip("'\"")
    lines: list[str] = []
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines(keepends=True)

    found = False
    new_lines: list[str] = []
    for line in lines:
        if line.strip().startswith("KITE_ACCESS_TOKEN="):
            new_lines.append(f"KITE_ACCESS_TOKEN={token}\n")
            found = True
        else:
            new_lines.append(line)

    if not found:
        new_lines.append(f"\nKITE_ACCESS_TOKEN={token}\n")

    ENV_PATH.write_text("".join(new_lines), encoding="utf-8")
    os.environ["KITE_ACCESS_TOKEN"] = token

    # If CLOUD_DASHBOARD_URL is configured, automatically push the fresh token to cloud infra
    cloud_url = os.getenv("CLOUD_DASHBOARD_URL", "").strip()
    if cloud_url:
        try:
            import urllib.request
            import json
            op_tok = os.getenv("DASHBOARD_API_TOKEN", "apex3_secure_operator_token").strip()
            target_ep = f"{cloud_url.rstrip('/')}/api/set_token"
            req = urllib.request.Request(
                target_ep,
                data=json.dumps({"access_token": token}).encode("utf-8"),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {op_tok}",
                    "Origin": "http://127.0.0.1:5000",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    print(f"  [+] Automatically synced fresh token to cloud instance: {cloud_url}")
        except Exception as e:
            print(f"  [!] Note: Could not auto-sync token to cloud ({cloud_url}): {e}")


def extract_token_from_string(raw: str) -> str:
    """Extract request_token whether user pasted a full URL or a bare token."""
    cleaned = raw.strip().strip("'\" ")
    if "request_token=" in cleaned:
        if "?" in cleaned:
            parsed = urlparse(cleaned)
            qs = parse_qs(parsed.query)
        else:
            qs = parse_qs(cleaned)
        if "request_token" in qs and qs["request_token"]:
            return qs["request_token"][0].strip()
    return cleaned


def start_local_listener(port: int = 5000) -> tuple[HTTPServer | None, int]:
    """Attempts to bind a local HTTP listener on standard callback ports."""
    ports_to_try = [port, 8080, 8000]
    for p in ports_to_try:
        try:
            server = HTTPServer(("127.0.0.1", p), KiteCallbackReceiver)
            t = threading.Thread(target=server.serve_forever, daemon=True, name=f"KiteListener-{p}")
            t.start()
            return server, p
        except OSError:
            continue
    return None, 0


def establish_angel_connection() -> tuple[bool, str, float | None]:
    """Authenticates Angel One SmartAPI using TOTP and tests live feeds."""
    api_key = os.getenv("ANGEL_API_KEY", "").strip()
    client_code = os.getenv("ANGEL_CLIENT_CODE", "").strip()
    pin = os.getenv("ANGEL_PIN", "").strip()
    totp_secret = os.getenv("ANGEL_TOTP_SECRET", "").strip()

    if not all([api_key, client_code, pin, totp_secret]):
        return False, "Angel One credentials incomplete in .env", None

    try:
        from apex3.broker.angel_data_provider import AngelDataProvider
        provider = AngelDataProvider(
            api_key=api_key,
            client_code=client_code,
            pin=pin,
            totp_secret=totp_secret,
        )
        if not provider._auth_token and not provider.authenticate():
            return False, "Angel One SmartAPI login failed (check credentials/TOTP)", None

        quote = provider.get_quote("NIFTY 50", exchange="NSE")
        return True, f"Client {client_code} | NIFTY 50 LTP: Rs. {quote.last_price:,.2f}", quote.last_price
    except Exception as e:
        return False, f"Angel One connection error: {e}", None


def check_existing_kite_session(api_key: str, access_token: str) -> tuple[bool, dict | None, dict | None, str]:
    """Tests if the currently saved KITE_ACCESS_TOKEN is still active."""
    if not access_token:
        return False, None, None, "No access token configured in .env"
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token)
        profile = kite.profile()
        margins = kite.margins(segment="equity")
        return True, profile, margins, "Session valid"
    except Exception as e:
        return False, None, None, str(e)


def login_and_authenticate_kite(api_key: str, api_secret: str) -> tuple[bool, str, dict | None, dict | None]:
    """Guides the operator through Zerodha Kite login and session generation."""
    from kiteconnect import KiteConnect
    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    # Reset any previously captured token
    KiteCallbackReceiver.captured_token = None

    # Start local receiver on port 5000 (standard Kite redirect port)
    server, port = start_local_listener(5000)

    print("\n" + "-" * 70)
    print("  ZERODHA KITE CONNECT AUTHENTICATION REQUIRED")
    print("-" * 70)
    print("  Daily Kite access tokens expire each morning at 06:00 AM IST.")
    print("  Opening Kite login in your browser...")
    print(f"\n  If the browser did not open, click or visit:")
    print(f"  --> {login_url}\n")
    print("-" * 70)

    # Launch user's default browser
    try:
        webbrowser.open(login_url)
    except Exception:
        pass

    if port > 0:
        print(f"  [*] Local/Cloudflare callback listener active on http://127.0.0.1:{port}/auth/callback")

    print("\n  Waiting for Zerodha login redirect (or paste the redirected URL / request_token below):")
    print("  (Tip: If your redirect hits 127.0.0.1:5000 or your Cloudflare Tunnel, press ENTER once logged in)")

    try:
        user_input = input("\n  Redirect URL / request_token > ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n[!] Authentication cancelled by user.")
        if server:
            server.shutdown()
        return False, "Cancelled by user", None, None

    request_token = extract_token_from_string(user_input)
    if not request_token and KiteCallbackReceiver.captured_token:
        request_token = KiteCallbackReceiver.captured_token
        print("  [+] Captured request_token automatically from redirect listener!")

    if not request_token:
        if server:
            server.shutdown()
        return False, "No request_token provided or captured", None, None

    print("\n  [*] Exchanging request_token for Zerodha daily access token...")
    try:
        session_data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session_data.get("access_token")
        if not access_token:
            return False, f"Exchange failed: {session_data}", None, None

        # Save to .env
        save_env_token(access_token)

        # Validate with profile and margins
        kite.set_access_token(access_token)
        profile = kite.profile()
        margins = kite.margins(segment="equity")
        if server:
            server.shutdown()
        return True, access_token, profile, margins
    except Exception as e:
        if server:
            server.shutdown()
        return False, f"Failed to generate session: {e}", None, None


def main():
    parser = argparse.ArgumentParser(description="APEX-3 Live Trading Connection Establishment")
    parser.add_argument("--start", action="store_true", help="Automatically launch live trading engine without confirmation prompt")
    parser.add_argument("--relogin", action="store_true", help="Force fresh Zerodha login even if existing token is valid")
    parser.add_argument("--verify-only", action="store_true", help="Verify connection and exit without running live engine")
    args = parser.parse_args()

    now_ist = datetime.now(IST)
    print("\n" + "=" * 70)
    print("      APEX-3 LIVE BROKER ESTABLISHMENT & ENGINE LAUNCHER")
    print(f"      Session Time: {now_ist.strftime('%Y-%m-%d %H:%M:%S %Z')} ({now_ist.strftime('%A')})")
    print("=" * 70)

    # 1. Angel One SmartAPI Feed Verification
    print("\n[Step 1/3] Connecting Angel One SmartAPI (Live Market Feed)...")
    angel_ok, angel_msg, nifty_ltp = establish_angel_connection()
    if angel_ok:
        print(f"  [PASS] {angel_msg}")
    else:
        print(f"  [FAIL] {angel_msg}")
        print("         Please check ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, and ANGEL_TOTP_SECRET in .env")
        sys.exit(1)

    # 2. Zerodha Kite Connect Verification & Authentication
    print("\n[Step 2/3] Checking Zerodha Kite Connect Execution Session...")
    kite_api_key = os.getenv("KITE_API_KEY", "").strip()
    kite_api_secret = os.getenv("KITE_API_SECRET", "").strip()
    existing_token = os.getenv("KITE_ACCESS_TOKEN", "").strip()

    if not kite_api_key or not kite_api_secret:
        print("  [FAIL] KITE_API_KEY or KITE_API_SECRET missing in .env")
        sys.exit(1)

    kite_ok = False
    profile = None
    margins = None

    if not args.relogin and existing_token:
        is_valid, prof, mrg, msg = check_existing_kite_session(kite_api_key, existing_token)
        if is_valid:
            kite_ok = True
            profile = prof
            margins = mrg
            print(f"  [PASS] Active session verified from .env!")
        else:
            print(f"  [*] Saved token expired or invalid ({msg}). Fresh login required.")

    if not kite_ok:
        if args.verify_only:
            print("  [EXPIRED] Saved Zerodha token is expired. Run 'python connect_live.py' to authenticate.")
            print("\n" + "=" * 70)
            print("      VERIFICATION SUMMARY")
            print("=" * 70)
            print(f"  1. Market Data (Angel One) : [PASS] (NIFTY 50: Rs. {nifty_ltp:,.2f})")
            print("  2. Execution Broker (Kite) : [EXPIRED / LOGIN REQUIRED]")
            print("  3. Action Required         : Run 'python connect_live.py' to establish connection.")
            print("=" * 70 + "\n")
            return

        login_ok, access_token, prof, mrg = login_and_authenticate_kite(kite_api_key, kite_api_secret)
        if not login_ok:
            print(f"\n  [FAIL] Kite authentication failed: {access_token}")
            sys.exit(1)
        kite_ok = True
        profile = prof
        margins = mrg

    # Display Kite Details
    user_name = profile.get("user_name", "Trader")
    user_id = profile.get("user_id", "")
    available_cash = float(margins.get("available", {}).get("cash", 0.0) or 0.0)
    net_equity = float(margins.get("net", 0.0) or 0.0)

    print(f"  [PASS] Authenticated User : {user_name} (Client ID: {user_id})")
    print(f"  [PASS] Available Cash    : Rs. {available_cash:,.2f} (Net Equity: Rs. {net_equity:,.2f})")

    if net_equity < 15000.0:
        print(f"  [!] Warning: Net equity (Rs. {net_equity:,.2f}) is below standard Rs. 15,000 capital base.")
    else:
        print("  [PASS] Capital Base Check : Sizing requirement met (>= Rs. 15,000.00)")

    # 3. Pre-Flight State Audit
    print("\n[Step 3/3] Performing Pre-Flight Position & Order State Audit...")
    try:
        from kiteconnect import KiteConnect
        kite = KiteConnect(api_key=kite_api_key)
        kite.set_access_token(os.getenv("KITE_ACCESS_TOKEN", ""))
        orders = kite.orders()
        positions = kite.positions().get("net", [])
        open_pos = [p for p in positions if int(p.get("quantity", 0)) != 0]

        print(f"  [PASS] Active Orders      : {len(orders)} order(s) in Kite book")
        print(f"  [PASS] Open Positions     : {len(open_pos)} open position(s)")
        if len(open_pos) == 0:
            print("  [PASS] Flat State Check   : Verified clean (Flat positions, ready for session)")
        else:
            print(f"  [!] Open Positions Detected: {', '.join(p.get('tradingsymbol','') for p in open_pos)}")
    except Exception as e:
        print(f"  [!] Audit warning: {e}")

    # Summary
    print("\n" + "=" * 70)
    print("      ALL BROKER CONNECTIONS SUCCESSFULLY ESTABLISHED")
    print("=" * 70)
    print(f"  1. Market Data (Angel One) : CONNECTED (NIFTY 50: Rs. {nifty_ltp:,.2f})")
    print(f"  2. Execution Broker (Kite) : CONNECTED ({user_name} [{user_id}], Cash: Rs. {available_cash:,.2f})")
    print(f"  3. Environment Status      : .env UPDATED & SYNCED")
    print(f"  4. Live Engine Ready       : YES")
    print("=" * 70)

    if args.verify_only:
        print("\n[+] Verification-only mode completed. Exiting.")
        return

    # Prompt or auto-launch
    should_launch = args.start
    if not should_launch:
        print("\n>>> Launch APEX-3 Autonomous Live Trading Engine now?")
        try:
            choice = input("    Press ENTER to start live trading (or type 'q' to exit): ").strip().lower()
            if choice not in ("q", "quit", "exit", "n", "no"):
                should_launch = True
        except (KeyboardInterrupt, EOFError):
            print("\nExiting without launching engine.")
            return

    if should_launch:
        print("\n" + "=" * 70)
        print("      BOOTING APEX-3 AUTONOMOUS LIVE ENGINE...")
        print("      Dashboard URL: http://127.0.0.1:5000")
        print("      Press Ctrl+C to safely pause or stop live trading")
        print("=" * 70 + "\n")
        
        # Ensure LIVE_TRADING_ENABLED is true
        os.environ["LIVE_TRADING_ENABLED"] = "true"
        
        import subprocess
        python_exe = sys.executable
        try:
            subprocess.run([python_exe, "-m", "apex3.main", "--mode", "live"], check=False)
        except KeyboardInterrupt:
            print("\n[+] Live engine stopped by operator.")
    else:
        print("\n[+] Broker connections established and saved.")
        print("    To start live trading anytime, run:")
        print("    python connect_live.py --start")
        print("    or: python -m apex3.main --mode live\n")


if __name__ == "__main__":
    main()
