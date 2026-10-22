"""APEX-3 Local Operator Dashboard.

Localhost-only Flask application. Displays:
- Operating mode (PAPER / LIVE)
- Current IST session state & clock
- Connection & quote health
- Active Watchlist (manual selection up to 3 symbols or scanner)
- Market Regime status (NIFTY 50 EMA-20 slope, ADX)
- Decision pipeline gates
- Broker-reported margin (available, used, net)
- Open positions & filled trades
- Protective stop status & trailing progress (R-multiples)
- Day P&L (realized & unrealized)
- Actions: Pause/Resume, Emergency Flatten, Update Watchlist

CRITICAL: Strictly bound to 127.0.0.1. No remote exposure.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request

from apex3.config import AppConfig
from apex3.journal.sqlite_journal import Journal
from apex3.strategy.engine import StrategyEngine

IST = ZoneInfo("Asia/Kolkata")


def create_app(engine: StrategyEngine, config: AppConfig, journal: Journal) -> Flask:
    """Create Flask application instance."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )

    @app.route("/")
    def index():
        return render_template("dashboard.html", mode=config.mode)

    @app.route("/login/kite")
    def login_kite():
        """Redirects operator to Zerodha Kite login page."""
        api_key = config.secrets.kite_api_key or os.environ.get("KITE_API_KEY", "")
        if not api_key:
            return "Error: KITE_API_KEY is not set in .env", 400
        kite_url = f"https://kite.zerodha.com/connect/login?v=3&api_key={api_key}"
        return redirect(kite_url)

    @app.route("/auth/callback")
    def auth_callback():
        """Captures request_token from Kite redirect, exchanges for daily access_token, and saves to .env."""
        request_token = request.args.get("request_token")
        if not request_token:
            return "Error: No request_token received from Zerodha", 400

        api_key = config.secrets.kite_api_key or os.environ.get("KITE_API_KEY", "")
        api_secret = config.secrets.kite_api_secret or os.environ.get("KITE_API_SECRET", "")

        try:
            from kiteconnect import KiteConnect
            from dotenv import set_key
            kite = KiteConnect(api_key=api_key)
            data = kite.generate_session(request_token, api_secret=api_secret)
            token = data.get("access_token")

            env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
            set_key(env_file, "KITE_ACCESS_TOKEN", token)
            os.environ["KITE_ACCESS_TOKEN"] = token

            kite_inst = getattr(engine.broker, "execution_broker", engine.broker)
            if hasattr(kite_inst, "_kite") and kite_inst._kite:
                kite_inst._kite.set_access_token(token)
                kite_inst._access_token = token

            return redirect("/?auth=success")
        except Exception as e:
            return f"Authentication Failed: {e}", 500

    @app.route("/api/status")
    def api_status():
        """Get live system status for dashboard auto-refresh."""
        now = engine.scheduler.now_ist()
        session_info = engine.scheduler.get_state_display(now)

        # Broker margin
        try:
            margin_info = engine.broker.get_margins()
        except Exception:
            from apex3.models import MarginInfo
            margin_info = MarginInfo(
                equity_available=0.0,
                equity_used=0.0,
                collateral=0.0,
                net_value=0.0,
                timestamp=now,
            )

        # Market regime
        bench_candles = engine.broker.get_historical(config.regime.benchmark_symbol, "5minute", None, None)
        bench_quote = engine.broker.get_quote(config.regime.benchmark_symbol)
        from apex3.strategy.regime import evaluate_market_regime
        regime_eval = evaluate_market_regime(bench_candles, config.regime, bench_quote)

        # Positions
        open_positions = []
        for pos in engine.state_machine.positions.values():
            if pos.quantity > 0:
                open_positions.append(
                    {
                        "symbol": pos.symbol,
                        "side": pos.side.value,
                        "quantity": pos.quantity,
                        "average_entry": pos.average_entry,
                        "current_stop": pos.current_stop_price,
                        "initial_stop": pos.initial_stop_price,
                        "is_protected": (pos.protective_order_id != ""),
                        "protective_order_id": pos.protective_order_id,
                        "best_favorable_price": pos.best_favorable_price,
                        "r_multiple": round(
                            (pos.best_favorable_price - pos.average_entry) / max(0.01, pos.r_value)
                            if pos.side.value == "BUY"
                            else (pos.average_entry - pos.best_favorable_price) / max(0.01, pos.r_value),
                            2,
                        ),
                        "trail_activated": pos.trail_activated,
                        "modification_count": pos.stop_modification_count,
                    }
                )

        # Orders & Fills
        orders = engine.journal.get_orders(limit=15) if hasattr(engine.journal, "get_orders") else []
        events = engine.journal.get_events(limit=15)

        # Evaluate current pipeline step for reasons
        decisions = engine.evaluate_step(now)
        decisions_summary = [
            {
                "symbol": d.symbol or "ALL",
                "decision": d.decision.value,
                "reason": d.explanation,
                "action": d.broker_action,
                "codes": d.reason_codes,
            }
            for d in decisions
        ]

        return jsonify(
            {
                "timestamp_ist": now.strftime("%Y-%m-%d %H:%M:%S IST"),
                "mode": config.mode.upper(),
                "session": session_info,
                "is_paused": engine.is_paused,
                "pause_reason": engine.pause_reason,
                "watchlist": engine.get_active_watchlist(),
                "scanner_enabled": config.watchlist.scanner_enabled,
                "regime": {
                    "state": regime_eval.regime.name,
                    "adx": regime_eval.adx_val,
                    "ema_slope": regime_eval.ema_slope_val,
                    "reason": regime_eval.gate.reason,
                },
                "margins": {
                    "available": margin_info.equity_available,
                    "used": margin_info.equity_used,
                    "net": margin_info.net_value,
                },
                "open_positions": open_positions,
                "decisions": decisions_summary,
                "recent_orders": orders[:10],
                "recent_events": events[:10],
            }
        )

    @app.route("/api/watchlist", methods=["POST"])
    def update_watchlist():
        """Update the manual watchlist (up to 3 symbols)."""
        data = request.json or {}
        raw_symbols = data.get("symbols", [])
        if not isinstance(raw_symbols, list):
            return jsonify({"status": "error", "message": "Symbols must be a list"}), 400

        updated = engine.set_manual_symbols(raw_symbols)
        return jsonify({"status": "success", "watchlist": updated})

    @app.route("/api/pause", methods=["POST"])
    def toggle_pause():
        """Pause or resume strategy evaluation."""
        data = request.json or {}
        pause_req = data.get("pause", not engine.is_paused)
        engine.is_paused = pause_req
        engine.pause_reason = "Manual operator pause" if engine.is_paused else ""
        return jsonify({"status": "success", "is_paused": engine.is_paused})

    @app.route("/api/emergency_flatten", methods=["POST"])
    def emergency_flatten():
        """Trigger emergency square-off sequence."""
        success, msg = engine.reconciler.execute_flatten_sequence()
        return jsonify({"status": "success" if success else "error", "message": msg})

    return app
