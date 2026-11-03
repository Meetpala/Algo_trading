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

CLOUD_BASE_URL = "http://34.14.147.10:5000"


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


def push_access_token_to_cloud(access_token: str) -> dict:
    with urllib.request.urlopen(f"{CLOUD_BASE_URL}/", timeout=10) as r:
        html = r.read().decode("utf-8")
    m = re.search(r"OPERATOR_TOKEN\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]", html)
    op_token = m.group(1) if m else ""

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
    with urllib.request.urlopen(f"{CLOUD_BASE_URL}/", timeout=10) as r:
        html = r.read().decode("utf-8")
    m = re.search(r"OPERATOR_TOKEN\s*=\s*[\x22\x27]([^\x22\x27]+)[\x22\x27]", html)
    op_token = m.group(1) if m else ""

    req = urllib.request.Request(
        f"{CLOUD_BASE_URL}/api/status",
        headers={"X-Operator-Token": op_token},
    )
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> None:
    env_path = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".env"))
    load_dotenv(env_path, override=True)

    api_key = os.environ.get("KITE_API_KEY", "")
    api_secret = os.environ.get("KITE_API_SECRET", "")

    if len(sys.argv) > 1:
        req_token = extract_request_token(sys.argv[1])
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

