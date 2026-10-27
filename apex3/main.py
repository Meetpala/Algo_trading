"""APEX-3 Main Entrypoint and Orchestrator.

Starts the strategy engine in paper mode by default, boots the local
operator dashboard on 127.0.0.1:5000, and manages the simulation loop.

Usage:
    python -m apex3.main
    python -m apex3.main --mode paper --port 5000
    python -m apex3.main --simulate
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from apex3.broker.paper import PaperBroker
from apex3.config import load_config
from apex3.dashboard.app import create_app
from apex3.journal.sqlite_journal import Journal
from apex3.journal.trade_logger import TradeLogger
from apex3.strategy.engine import StrategyEngine

IST = ZoneInfo("Asia/Kolkata")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger("apex3.main")


def run_app():
    parser = argparse.ArgumentParser(description="APEX-3 Zerodha Intraday System")
    parser.add_argument("--config", type=str, default=None, help="Path to custom config.yaml")
    parser.add_argument("--mode", type=str, default=None, choices=["paper", "live"], help="Operating mode (paper or live)")
    parser.add_argument("--simulate", action="store_true", help="Run a simulated trading day and print report")
    parser.add_argument("--sync-trades", action="store_true", help="Sync broker trades to info.log and exit")
    parser.add_argument("--host", type=str, default=None, help="Dashboard host interface (e.g. 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Local operator dashboard port")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.mode:
        config.mode = args.mode
    logger.info("Initializing APEX-3 version 0.1.0 (Mode: %s)...", config.mode.upper())

    # Initialize Journal & Trade Audit Logger
    journal = Journal(db_path=config.journal.db_path)
    trade_logger = TradeLogger(log_path="info.log", start_date="2026-10-29")

    # Initialize Broker Adapter
    if config.mode == "live":
        from apex3.broker.kite_adapter import KiteAdapter
        from apex3.broker.rate_limiter import RateLimiter
        logger.warning("LIVE TRADING MODE CONFIGURED — Checking safety gates...")
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
            logger.info("Configuring HYBRID MODE: Angel One SmartAPI (Data) + Zerodha (Orders)")
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

        # Pre-flight session check
        if broker.is_session_valid():
            logger.info("SUCCESS: Broker live session(s) ACTIVE and authenticated.")
        else:
            msg = (
                "CRITICAL PRE-FLIGHT ERROR: Broker session authentication check failed. "
                "Verify API credentials, TOTP, and access tokens before starting autonomous live trading."
            )
            logger.critical(msg)
            if not getattr(args, "simulate", False):
                import sys
                logger.critical("Aborting live autonomous startup. Engine will NOT start.")
                sys.exit(1)
    else:
        logger.info("Operating in PAPER TRADING mode. Real orders are impossible.")
        broker = PaperBroker(starting_margin=config.paper_starting_capital)

    # Sync offline/startup trades from broker into info.log
    try:
        new_synced = trade_logger.sync_from_broker(broker)
        if new_synced > 0:
            logger.info("Trade audit log synced: %d new trade execution(s) recorded in info.log", new_synced)
        else:
            logger.info("Trade audit log (info.log) is up to date.")
    except Exception as e:
        logger.warning("Could not sync trade audit log on startup: %s", e)

    if getattr(args, "sync_trades", False):
        print(f"Trade sync complete: info.log is up to date. ({len(trade_logger._recorded_trade_ids)} IDs tracked)")
        return

    # Initialize Strategy Engine
    engine = StrategyEngine(config=config, broker=broker, journal=journal, trade_logger=trade_logger)

    if config.mode == "live":
        from apex3.reconciliation.reconciler import Reconciler
        logger.info("Executing mandatory pre-flight account state and position reconciliation audit...")
        reconciler = Reconciler(
            broker=broker,
            state_machine=engine.state_machine,
            journal=journal,
            session_tag=engine.session_tag,
            config=config,
        )
        radar_cfg = getattr(config, "raw_dict", {}).get("radar", {}) if hasattr(config, "raw_dict") else {}
        watched = list(radar_cfg.get("core_watchlist", [])) + list(radar_cfg.get("extended_pool", []))
        if not watched:
            watched = list(config.watchlist.symbols) + list(config.watchlist.scanner_universe)
        rec_status = reconciler.audit_startup_state(watched_symbols=watched)
        if not rec_status.is_clean:
            msg = (
                f"CRITICAL PRE-FLIGHT AUDIT FAILED: {rec_status.message}. "
                "Account state is unverified or has conflicting external positions/orders. "
                "Log into the Kite app, resolve orders and positions manually, and restart."
            )
            logger.critical(msg)
            if not getattr(args, "simulate", False):
                import sys
                logger.critical("Aborting live autonomous startup. Engine will NOT start.")
                sys.exit(1)
        else:
            logger.info("PRE-FLIGHT AUDIT PASSED: Account state verified. Flat=%s", rec_status.is_flat)

    if args.simulate:
        logger.info("Running simulated trading session...")
        # Run a simulated sequence through the 12-step pipeline
        sim_time = datetime.now(IST).replace(hour=9, minute=35, second=0)
        decisions = engine.evaluate_step(sim_time)
        print("\n=== SIMULATION CYCLE RESULT ===")
        for d in decisions:
            print(f"Symbol: {d.symbol} | Decision: {d.decision.value} | Reason: {d.explanation}")
            print(f"Broker Action: {d.broker_action}")
        print("===============================\n")
        return

    # Start Autonomous Engine Loop Thread (runs strategy independently of UI)
    import threading

    def _trading_loop():
        logger.info("Autonomous Strategy Execution Thread started (Interval: dynamic 0.5s in-flight / 3.0s idle).")
        while True:
            try:
                now = datetime.now(IST)
                engine.evaluate_step(now)
            except Exception as e:
                logger.error("Error in autonomous trading loop: %s", e)
            sleep_time = 0.5 if getattr(engine, "has_in_flight_orders", lambda: False)() else 3.0
            time.sleep(sleep_time)

    loop_thread = threading.Thread(target=_trading_loop, daemon=True, name="Apex3TradingLoop")
    loop_thread.start()

    # Create & Run Flask Local Dashboard
    dashboard_app = create_app(engine, config, journal)
    host = args.host or config.dashboard.host
    port = args.port or config.dashboard.port
    logger.info(
        "Starting APEX-3 Operator Dashboard at http://%s:%d...",
        host,
        port,
    )
    dashboard_app.run(
        host=host,
        port=port,
        debug=False,
    )


if __name__ == "__main__":
    run_app()
