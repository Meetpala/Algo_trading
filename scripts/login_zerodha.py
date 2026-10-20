"""Interactive Zerodha Kite Connect Morning Authentication Tool.

Zerodha Kite Connect access tokens expire every single day at 06:00 AM IST.
Run this script each morning between 08:30 and 09:10 AM IST to generate
your daily access token and automatically save it into your `.env` file.

Usage:
    python scripts/login_zerodha.py
"""
import os
import re
import sys
from urllib.parse import urlparse, parse_qs
from dotenv import load_dotenv, set_key

# Load environment
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
load_dotenv(env_path)

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("Error: kiteconnect is not installed. Run: pip install kiteconnect")
    sys.exit(1)


def main():
    print("=" * 65)
    print("  APEX-3 ZERODHA KITE CONNECT AUTHENTICATION HELPER")
    print("=" * 65)

    api_key = os.getenv("KITE_API_KEY", "").strip()
    api_secret = os.getenv("KITE_API_SECRET", "").strip()

    if not api_key or api_key == "your_api_key_here":
        api_key = input("Enter your Kite API Key: ").strip()
        if not api_key:
            print("Error: API Key cannot be empty.")
            sys.exit(1)
        set_key(env_path, "KITE_API_KEY", api_key)

    if not api_secret or api_secret == "your_api_secret_here":
        api_secret = input("Enter your Kite API Secret: ").strip()
        if not api_secret:
            print("Error: API Secret cannot be empty.")
            sys.exit(1)
        set_key(env_path, "KITE_API_SECRET", api_secret)

    kite = KiteConnect(api_key=api_key)
    login_url = kite.login_url()

    print("\nStep 1: Open the following URL in your web browser:")
    print("-" * 65)
    print(login_url)
    print("-" * 65)
    print("Step 2: Log in with your Zerodha User ID, Password, and TOTP.")
    print("Step 3: After login, your browser will redirect to your Redirect URL.")
    print("        (Example: https://127.0.0.1/?status=success&request_token=XXXXXX)")
    print("-" * 65)

    raw_input = input("\nPaste the full Redirect URL (or just the request_token): ").strip()
    if not raw_input:
        print("Error: Input cannot be empty.")
        sys.exit(1)

    # Extract request_token
    request_token = raw_input
    if "request_token=" in raw_input:
        parsed = urlparse(raw_input)
        qs = parse_qs(parsed.query)
        if "request_token" in qs:
            request_token = qs["request_token"][0]

    print(f"\nExchanging request_token for daily session...")
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = data.get("access_token")
        user_name = data.get("user_name", "Trader")
        user_id = data.get("user_id", "")

        if not access_token:
            print("Failed to obtain access token from response:", data)
            sys.exit(1)

        # Save to .env
        set_key(env_path, "KITE_ACCESS_TOKEN", access_token)

        print("\n" + "=" * 65)
        print(f" SUCCESS! Authenticated as {user_name} ({user_id})")
        print(f" Daily Access Token generated and saved to .env")
        print("=" * 65)

        # Validate margins
        kite.set_access_token(access_token)
        margins = kite.margins(segment="equity")
        live_balance = margins.get("available", {}).get("live_balance", margins.get("net", 0.0))
        print(f" Live Available Equity Margin: Rs. {float(live_balance):,.2f}")
        print("=" * 65)

    except Exception as e:
        print(f"\nAuthentication Failed: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
