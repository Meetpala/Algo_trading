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

import secrets

IST = ZoneInfo("Asia/Kolkata")


def create_app(engine: StrategyEngine, config: AppConfig, journal: Journal) -> Flask:
    """Create Flask application instance."""
    app = Flask(
        __name__,
        template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )

    def _get_operator_token() -> str:
        return (
            os.environ.get("DASHBOARD_API_TOKEN")
            or getattr(getattr(config, "dashboard", None), "api_token", "")
            or "apex3_secure_operator_token"
        )

    def _is_authorized() -> bool:
        """Verify operator authorization for state-mutating endpoints."""
        auth_required = bool(getattr(getattr(config, "dashboard", None), "auth_required", False))
        remote = request.remote_addr or ""
        is_loopback = remote in ("127.0.0.1", "localhost", "::1", "testclient")

        expected_token = _get_operator_token()
        if auth_required or not is_loopback:
            if not expected_token:
                return False
            auth_header = request.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                token = auth_header[7:].strip()
            else:
                token = request.headers.get("X-Operator-Token") or request.headers.get("X-API-Key") or ""
            if not token or not secrets.compare_digest(token, expected_token):
                return False
        return True

    @app.before_request
    def restrict_to_localhost():
        # Allow OAuth callback and login initiation from browser / Cloudflare tunnel
        if request.path in ("/auth/callback", "/login/kite"):
            return None

        # Prevent any external exposure: APEX-3 Dashboard must be strictly local loopback or configured host
        remote = request.remote_addr or ""
        is_loopback = remote in ("127.0.0.1", "localhost", "::1", "testclient")
        allow_external = getattr(getattr(config, "dashboard", None), "host", "127.0.0.1") == "0.0.0.0"
        if not is_loopback and not allow_external:
            return jsonify({"error": "Forbidden: APEX-3 Dashboard is strictly restricted to local loopback interface."}), 403

        # CSRF Protection: For mutating requests, verify Origin or Referer is strictly from loopback
        if request.method in ("POST", "PUT", "DELETE", "PATCH"):
            origin = request.headers.get("Origin") or ""
            referer = request.headers.get("Referer") or ""
            valid_origins = ("http://127.0.0.1", "http://localhost", "https://127.0.0.1", "https://localhost")
            if origin and not any(origin.startswith(vo) for vo in valid_origins):
                return jsonify({"error": "Forbidden: Cross-site request rejected (invalid Origin)"}), 403
            if not origin and referer and not any(referer.startswith(vo) for vo in valid_origins):
                return jsonify({"error": "Forbidden: Cross-site request rejected (invalid Referer)"}), 403

    @app.route("/")
    def index():
        token_to_render = ""
        if _is_authorized():
            token_to_render = _get_operator_token()
        return render_template("dashboard.html", mode=config.mode, operator_token=token_to_render)

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
            lines = []
            if os.path.exists(env_file):
                with open(env_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            found = False
            new_lines = []
            for line in lines:
                if line.strip().startswith("KITE_ACCESS_TOKEN="):
                    new_lines.append(f"KITE_ACCESS_TOKEN={token}\n")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"KITE_ACCESS_TOKEN={token}\n")
            with open(env_file, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

            os.environ["KITE_ACCESS_TOKEN"] = token

            kite_inst = getattr(engine.broker, "execution_broker", engine.broker)
            if hasattr(kite_inst, "_kite") and kite_inst._kite:
                kite_inst._kite.set_access_token(token)
                kite_inst._access_token = token

            return redirect("/?auth=success")
        except Exception as e:
            return f"Authentication Failed: {e}", 500

    @app.route("/api/set_token", methods=["POST"])
    def api_set_token():
        """Allows authorized operator to push a fresh KITE_ACCESS_TOKEN remotely."""
        if not _is_authorized():
            return jsonify({"error": "Unauthorized: valid operator token required"}), 401
        payload = request.get_json(silent=True) or {}
        token = payload.get("access_token", "").strip()
        if not token:
            return jsonify({"error": "Bad request: 'access_token' field is required"}), 400

        try:
            env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), ".env")
            lines = []
            if os.path.exists(env_file):
                with open(env_file, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            found = False
            new_lines = []
            for line in lines:
                if line.strip().startswith("KITE_ACCESS_TOKEN="):
                    new_lines.append(f"KITE_ACCESS_TOKEN={token}\n")
                    found = True
                else:
                    new_lines.append(line)
            if not found:
                new_lines.append(f"KITE_ACCESS_TOKEN={token}\n")
            with open(env_file, "w", encoding="utf-8") as f:
                f.writelines(new_lines)

            os.environ["KITE_ACCESS_TOKEN"] = token

            kite_inst = getattr(engine.broker, "execution_broker", engine.broker)
            if hasattr(kite_inst, "_kite") and kite_inst._kite:
                kite_inst._kite.set_access_token(token)
                kite_inst._access_token = token
                if hasattr(kite_inst, "_instrument_info_cache"):
                    kite_inst._instrument_info_cache.clear()
                if hasattr(kite_inst, "_mis_margin_cache"):
                    kite_inst._mis_margin_cache.clear()

            return jsonify({
                "status": "SUCCESS",
                "message": "KITE_ACCESS_TOKEN updated in .env and live broker session hot-reloaded successfully.",
            })
        except Exception as e:
            return jsonify({"status": "ERROR", "message": str(e)}), 500

    @app.route("/api/status")
    def api_status():
        """Get live system status for dashboard auto-refresh."""
        if not _is_authorized():
            return jsonify({"error": "Unauthorized: valid operator token required"}), 401

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

        # Market regime: read from engine cache to prevent rate-limit exhaustion
        regime_eval = getattr(engine, "latest_regime_eval", None)
        if regime_eval is None:
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

        # Orders & Fills: Separate live broker orders from SQLite journal orders
        live_orders = []
        live_orders_fetch_status = "OK"
        live_orders_error = ""
        try:
            broker_orders = engine.broker.get_orders() if hasattr(engine, "broker") and hasattr(engine.broker, "get_orders") else []
            for bo in broker_orders:
                live_orders.append({
                    "order_id": bo.order_id,
                    "symbol": bo.request.symbol if bo.request else getattr(bo, "symbol", ""),
                    "side": bo.request.side.value if (bo.request and hasattr(bo.request.side, "value")) else str(getattr(bo, "side", "")),
                    "quantity": bo.request.quantity if bo.request else getattr(bo, "quantity", 0),
                    "filled_quantity": getattr(bo, "filled_quantity", 0),
                    "pending_quantity": getattr(bo, "pending_quantity", 0),
                    "order_type": bo.request.order_type.value if (bo.request and hasattr(bo.request.order_type, "value")) else str(getattr(bo, "order_type", "")),
                    "price": getattr(bo.request, "price", 0.0) if bo.request else getattr(bo, "price", 0.0),
                    "trigger_price": getattr(bo.request, "trigger_price", 0.0) if bo.request else getattr(bo, "trigger_price", 0.0),
                    "status": bo.status.value if hasattr(bo.status, "value") else str(bo.status),
                    "status_message": getattr(bo, "status_message", ""),
                    "average_price": getattr(bo, "average_price", 0.0),
                    "source": "live_broker",
                    "updated_at": bo.last_updated.strftime("%Y-%m-%d %H:%M:%S") if getattr(bo, "last_updated", None) else now.strftime("%Y-%m-%d %H:%M:%S"),
                })
        except Exception as bo_err:
            live_orders_fetch_status = "ERROR"
            live_orders_error = str(bo_err)

        journal_orders = []
        try:
            raw_jo = engine.journal.get_orders(limit=15) if hasattr(engine.journal, "get_orders") else []
            for jo in raw_jo:
                if isinstance(jo, dict):
                    jo_copy = dict(jo)
                    jo_copy["source"] = "sqlite_journal"
                    journal_orders.append(jo_copy)
                else:
                    journal_orders.append(jo)
        except Exception:
            journal_orders = []

        try:
            events = engine.journal.get_events(limit=15) if hasattr(engine.journal, "get_events") else []
        except Exception:
            events = []

        # Read latest decisions from engine background thread (strictly read-only, side-effect free)
        decisions = getattr(engine, "last_decisions", [])
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
                "live_orders": live_orders,
                "live_orders_status": live_orders_fetch_status,
                "live_orders_error": live_orders_error,
                "journal_orders": journal_orders[:10],
                "recent_orders": (live_orders if live_orders else journal_orders)[:10],
                "recent_events": events[:10],
            }
        )

    @app.route("/api/watchlist", methods=["POST"])
    def update_watchlist():
        """Update the manual watchlist (up to 3 symbols)."""
        if not _is_authorized():
            return jsonify({"status": "error", "message": "Unauthorized: valid operator token required"}), 401
        data = request.json or {}
        raw_symbols = data.get("symbols", [])
        if not isinstance(raw_symbols, list):
            return jsonify({"status": "error", "message": "Symbols must be a list"}), 400

        updated = engine.set_manual_symbols(raw_symbols)
        return jsonify({"status": "success", "watchlist": updated})

    @app.route("/api/pause", methods=["POST"])
    def toggle_pause():
        """Pause or resume strategy evaluation."""
        if not _is_authorized():
            return jsonify({"status": "error", "message": "Unauthorized: valid operator token required"}), 401
        data = request.json or {}
        pause_req = data.get("pause", not engine.is_paused)
        engine.is_paused = pause_req
        engine.pause_reason = "Manual operator pause" if engine.is_paused else ""
        return jsonify({"status": "success", "is_paused": engine.is_paused})

    @app.route("/api/emergency_flatten", methods=["POST"])
    def emergency_flatten():
        """Trigger emergency square-off sequence serialized with engine execution lock."""
        if not _is_authorized():
            return jsonify({"status": "error", "message": "Unauthorized: valid operator token required"}), 401
        lock = getattr(engine, "_execution_lock", None)
        if lock:
            with lock:
                success, msg = engine.reconciler.execute_flatten_sequence()
        else:
            success, msg = engine.reconciler.execute_flatten_sequence()
        return jsonify({"status": "success" if success else "error", "message": msg})

    return app
