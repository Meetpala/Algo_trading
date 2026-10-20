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
    parser.add_argument("--simulate", action="store_true", help="Run a simulated trading day and print report")
    parser.add_argument("--port", type=int, default=5000, help="Local operator dashboard port")
    args = parser.parse_args()

    config = load_config(args.config)
    logger.info("Initializing APEX-3 version 0.1.0 (Mode: %s)...", config.mode.upper())

    # Initialize Journal
    journal = Journal(db_path=config.journal.db_path)

    # Initialize Broker Adapter
    if config.mode == "live":
        from apex3.broker.kite_adapter import KiteAdapter
        logger.warning("LIVE TRADING MODE CONFIGURED — Checking safety gates...")
        broker = KiteAdapter(
            api_key=config.secrets.kite_api_key,
            access_token=config.secrets.kite_access_token,
            live_enabled=config.secrets.live_trading_enabled,
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
    logger.info(
        "Starting APEX-3 Local Operator Dashboard at http://%s:%d (localhost only)...",
        config.dashboard.host,
        args.port or config.dashboard.port,
    )
    dashboard_app.run(
        host=config.dashboard.host,
        port=args.port or config.dashboard.port,
        debug=False,
    )


if __name__ == "__main__":
    run_app()
