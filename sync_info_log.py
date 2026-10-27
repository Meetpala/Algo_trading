"""APEX-3 Trade Sync Utility.

Syncs and updates info.log with all trades taken by the system from tomorrow
(2026-10-29) onwards.

Usage:
    python sync_info_log.py
    python sync_info_log.py --mode live
    python sync_info_log.py --mode paper
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from apex3.config import load_config
from apex3.journal.trade_logger import TradeLogger

IST = ZoneInfo("Asia/Kolkata")


def main():
    parser = argparse.ArgumentParser(description="Sync broker trades to info.log")
    parser.add_argument("--config", type=str, default=None, help="Path to config.yaml")
    parser.add_argument("--mode", type=str, default=None, choices=["paper", "live"], help="Override mode")
    parser.add_argument("--start-date", type=str, default="2026-10-29", help="Start date (YYYY-MM-DD) for trades")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.mode:
        config.mode = args.mode

    print("\n" + "=" * 60)
    print("APEX-3 TRADE LOG SYNC (info.log)")
    print(f"Timestamp  : {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"Mode       : {config.mode.upper()}")
    print(f"Start Date : {args.start_date} (Past trades excluded)")
    print("=" * 60)

    # Initialize Trade Logger
    trade_logger = TradeLogger(log_path="info.log", start_date=args.start_date)

    # Initialize Broker
    if config.mode == "live":
        from apex3.broker.kite_adapter import KiteAdapter
        from apex3.broker.rate_limiter import RateLimiter

        rl_rates = {
            "orders": float(getattr(config.rate_limits, "orders_per_second", 10.0)),
            "quotes": float(getattr(config.rate_limits, "quotes_per_second", 1.0)),
            "historical": float(getattr(config.rate_limits, "historical_per_second", 3.0)),
        }
        rate_limiter = RateLimiter(rates=rl_rates)
        kite_broker = KiteAdapter(
            api_key=config.secrets.kite_api_key,
            access_token=config.secrets.kite_access_token,
            live_enabled=config.secrets.live_trading_enabled,
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
        else:
            broker = kite_broker

        if not broker.is_session_valid():
            print("[ERROR] Broker session invalid or internet not connected. Please check Kite access token / internet.")
            sys.exit(1)
        print("[OK] Connected to live broker.")
    else:
        from apex3.broker.paper import PaperBroker
        broker = PaperBroker(starting_margin=config.paper_starting_capital)
        print("[OK] Operating in Paper trading mode.")

    # Execute Sync
    new_trades = trade_logger.sync_from_broker(broker)
    total_recorded = len(trade_logger._recorded_trade_ids)

    print("-" * 60)
    if new_trades > 0:
        print(f"[SUCCESS] info.log updated with {new_trades} new trade execution(s)!")
    else:
        print("[UP TO DATE] No new trades since last sync. info.log is current.")
    print(f"Total Unique Trade/Order IDs Tracked: {total_recorded}")
    print("Target File: info.log")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
