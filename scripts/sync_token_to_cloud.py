"""One-click script to exchange a Zerodha redirect URL (or request_token) and sync KITE_ACCESS_TOKEN to both local .env and the Google Cloud VM (http://34.14.147.10:5000)."""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from dotenv import load_dotenv, set_key
from kiteconnect import KiteConnect

load_dotenv(os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env")), override=True)
CLOUD_BASE_URL = os.environ.get("CLOUD_BASE_URL") or os.environ.get("APEX3_CLOUD_URL") or "http://34.14.147.10:5000"


def extract_request_token(raw_input: str) -> str:
    raw = raw_input.strip()
    if "request_token=" in raw:
        parsed = urllib.parse.urlparse(raw)
        qs = urllib.parse.parse_qs(parsed.query)
        if "request_token" in qs and qs["request_token"]:
            return qs["request_token"][0].strip()
        m = re.search(r"request_token=([^&\s]+)", raw)
        if m:
            return m.group(1).strip()
    return raw


def _operator_token() -> str:
    """Operator token comes from local .env (DASHBOARD_API_TOKEN, same value as on the VM) - never scraped from HTML."""
    tok = os.environ.get("DASHBOARD_API_TOKEN", "").strip()
    if not tok:
        raise SystemExit(
            "DASHBOARD_API_TOKEN is not set in your local .env. Set it to the SAME value as on the VM .env "
            "(the dashboard no longer publishes its token to the network)."
        )
    return tok


def push_access_token_to_cloud(access_token: str) -> dict:
    op_token = _operator_token()

    payload = json.dumps({"access_token": access_token}).encode("utf-8")
    req = urllib.request.Request(
        f"{CLOUD_BASE_URL}/api/set_token",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "X-Operator-Token": op_token,
            "Origin": CLOUD_BASE_URL,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=25) as r:
        return json.loads(r.read().decode("utf-8"))


def verify_cloud_status() -> dict:
    op_token = _operator_token()

    req = urllib.request.Request(
        f"{CLOUD_BASE_URL}/api/status",
        headers={"X-Operator-Token": op_token},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    global CLOUD_BASE_URL
    import argparse
    parser = argparse.ArgumentParser(description="Sync Zerodha access token to Cloud VM")
    parser.add_argument("url_or_token", nargs="?", default=None, help="Redirect URL or request token")
    parser.add_argument("--cloud-url", type=str, default=None, help="Cloud VM base URL")
    args = parser.parse_args()

    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_dotenv(env_path, override=True)

    if args.cloud_url:
        CLOUD_BASE_URL = args.cloud_url.rstrip("/")

    api_key = os.environ.get("KITE_API_KEY", "")
    api_secret = os.environ.get("KITE_API_SECRET", "")

    if args.url_or_token:
        req_token = extract_request_token(args.url_or_token)
        print(f"[1/3] Exchanging request_token ({req_token[:8]}...) with Zerodha Kite Connect...")
        kite = KiteConnect(api_key=api_key)
        data = kite.generate_session(req_token, api_secret=api_secret)
        access_token = data["access_token"]
        set_key(env_path, "KITE_ACCESS_TOKEN", access_token)
        print(f"      New KITE_ACCESS_TOKEN generated and saved to local .env ({access_token[:8]}...)")
    else:
        access_token = os.environ.get("KITE_ACCESS_TOKEN", "")
        print(f"[1/3] Using existing KITE_ACCESS_TOKEN from local .env ({access_token[:8]}...)")

    print(f"[2/3] Pushing KITE_ACCESS_TOKEN to Cloud VM ({CLOUD_BASE_URL}/api/set_token)...")
    res = push_access_token_to_cloud(access_token)
    print(f"      Cloud Response: {res}")

    print(f"[3/3] Verifying live Cloud VM status ({CLOUD_BASE_URL}/api/status)...")
    st = verify_cloud_status()
    print(f"      Mode: {st.get('mode')} | Paused: {st.get('is_paused')} | Session: {st.get('session', {}).get('state')}")
    print(f"      Live Zerodha Margin on Cloud: Rs. {st.get('margins', {}).get('available')}")
    print(f"      Live Zerodha Orders on Cloud: {st.get('live_orders_status')} ({len(st.get('live_orders', []))} orders)")
    print(f"      Active Watchlist on Cloud ({len(st.get('watchlist', []))} stocks): {', '.join(st.get('watchlist', []))}")


if __name__ == "__main__":
    main()

