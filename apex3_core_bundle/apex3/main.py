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
    parser.add_argument("--host", type=str, default=None, help="Dashboard host interface (e.g. 127.0.0.1)")
    parser.add_argument("--port", type=int, default=5000, help="Local operator dashboard port")
    args = parser.parse_args()

    config = load_config(args.config)
    if args.mode:
        config.mode = args.mode
    logger.info("Initializing APEX-3 version 0.1.0 (Mode: %s)...", config.mode.upper())

    # Initialize Journal
    journal = Journal(db_path=config.journal.db_path)

    # Initialize Broker Adapter
    if config.mode == "live":
        from apex3.broker.kite_adapter import KiteAdapter
        logger.warning("LIVE TRADING MODE CONFIGURED — Checking safety gates...")
        kite_broker = KiteAdapter(
            api_key=config.secrets.kite_api_key,
            access_token=config.secrets.kite_access_token,
            live_enabled=config.secrets.live_trading_enabled,
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
            )
            broker = HybridBrokerAdapter(data_provider=angel_data, execution_broker=kite_broker)
        else:
            broker = kite_broker

        # Pre-flight session check
        if broker.is_session_valid():
            logger.info("SUCCESS: Zerodha Kite live session is ACTIVE and authenticated.")
        else:
            logger.warning(
                "ATTENTION: Zerodha Kite access token is expired or unauthenticated for today. "
                "Run 'python scripts/login_zerodha.py' or log in via dashboard at /login/kite before 09:15 IST."
            )
    else:
        logger.info("Operating in PAPER TRADING mode. Real orders are impossible.")
        broker = PaperBroker(starting_margin=config.paper_starting_capital)

    # Initialize Strategy Engine
    engine = StrategyEngine(config=config, broker=broker, journal=journal)

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
        logger.info("Autonomous Strategy Execution Thread started (Interval: 3.0s).")
        while True:
            try:
                now = datetime.now(IST)
                engine.evaluate_step(now)
            except Exception as e:
                logger.error("Error in autonomous trading loop: %s", e)
            time.sleep(3.0)

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
