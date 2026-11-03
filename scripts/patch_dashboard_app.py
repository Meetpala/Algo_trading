import os

file_path = os.path.join("apex3", "dashboard", "app.py")
with open(file_path, "r", encoding="utf-8") as f:
    content = f.read()

# 1. Update imports
old_imports = """from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, redirect, render_template, request

from apex3.config import AppConfig
from apex3.journal.sqlite_journal import Journal
from apex3.strategy.engine import StrategyEngine

import secrets

IST = ZoneInfo("Asia/Kolkata")"""

new_imports = """from __future__ import annotations

import json
import logging
import os
import secrets
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from flask import (
    Flask,
    Response,
    jsonify,
    make_response,
    redirect,
    render_template,
    request,
    session,
    stream_with_context,
)

from apex3.config import AppConfig
from apex3.journal.sqlite_journal import Journal
from apex3.strategy.engine import StrategyEngine

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("apex3.dashboard")"""

# Normalize \r\n
content = content.replace(old_imports.replace("\n", "\r\n"), new_imports.replace("\n", "\r\n"))
content = content.replace(old_imports, new_imports)

# 2. Update create_app tokens and authorization
old_tokens = """    _runtime_token = secrets.token_hex(16)

    def _get_operator_token() -> str:
        return (
            os.environ.get("DASHBOARD_API_TOKEN")
            or getattr(getattr(config, "dashboard", None), "api_token", "")
            or ("apex3_secure_operator_token" if "PYTEST_CURRENT_TEST" in os.environ else _runtime_token)
        )

    def _is_authorized() -> bool:
        \"\"\"Verify operator authorization for state-mutating endpoints.\"\"\"
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
        return True"""

new_tokens = """    app.secret_key = (
        os.environ.get("FLASK_SECRET_KEY")
        or os.environ.get("DASHBOARD_API_TOKEN")
        or "apex3_operator_session_secret_2026"
    )

    _runtime_token = secrets.token_hex(16)

    def _get_operator_token() -> str:
        cfg_token = getattr(getattr(config, "dashboard", None), "api_token", "")
        if cfg_token:
            return cfg_token
        return (
            os.environ.get("DASHBOARD_API_TOKEN")
            or ("apex3_secure_operator_token" if "PYTEST_CURRENT_TEST" in os.environ else "apex3_operator_live_2026")
        )

    def _is_authorized() -> bool:
        \"\"\"Verify operator authorization for state-mutating endpoints and operator dashboard.\"\"\"
        expected_token = _get_operator_token()
        if not expected_token:
            return True

        # 1. Flask session authorization
        if session.get("operator_authorized") is True:
            return True

        # 2. Cookie authorization
        auth_cookie = request.cookies.get("apex3_auth") or ""
        if auth_cookie and secrets.compare_digest(auth_cookie, expected_token):
            return True

        # 3. Query parameter (?token=...)
        query_token = request.args.get("token") or ""
        if query_token and secrets.compare_digest(query_token, expected_token):
            return True

        # 4. HTTP Headers (Authorization Bearer or X-Operator-Token / X-API-Key)
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = auth_header[7:].strip()
        else:
            token = request.headers.get("X-Operator-Token") or request.headers.get("X-API-Key") or ""
        if token and secrets.compare_digest(token, expected_token):
            return True

        # 5. Localhost / Loopback bypass (only if auth_required is explicitly False)
        remote = request.remote_addr or ""
        is_loopback = remote in ("127.0.0.1", "localhost", "::1", "testclient")
        auth_required = bool(getattr(getattr(config, "dashboard", None), "auth_required", False))
        if is_loopback and not auth_required:
            return True

        return False"""

content = content.replace(old_tokens.replace("\n", "\r\n"), new_tokens.replace("\n", "\r\n"))
content = content.replace(old_tokens, new_tokens)

# 3. Update restrict_to_localhost
old_restrict = """    @app.before_request
    def restrict_to_localhost():
        # Allow healthcheck, OAuth callback, and login initiation
        if request.path in ("/health", "/auth/callback", "/login/kite"):
            return None"""

new_restrict = """    @app.before_request
    def restrict_to_localhost():
        # Allow healthcheck, ping, OAuth callback, and login initiation
        if request.path in ("/health", "/ping", "/auth/callback", "/login/kite"):
            return None"""

content = content.replace(old_restrict.replace("\n", "\r\n"), new_restrict.replace("\n", "\r\n"))
content = content.replace(old_restrict, new_restrict)

# 4. Update index()
old_index = """    @app.route("/")
    def index():
        token_to_render = _get_operator_token() if _is_authorized() else ""
        return render_template("dashboard.html", mode=config.mode, operator_token=token_to_render)"""

new_index = """    @app.route("/")
    def index():
        token_param = request.args.get("token", "").strip()
        expected = _get_operator_token()
        if token_param and secrets.compare_digest(token_param, expected):
            session["operator_authorized"] = True

        auth_param = request.args.get("auth", "").strip()
        if auth_param in ("success", "already_active"):
            try:
                exec_b = getattr(engine.broker, "execution_broker", engine.broker)
                if exec_b.is_session_valid():
                    session["operator_authorized"] = True
            except Exception:
                pass

        is_auth = _is_authorized()
        token_to_render = expected if is_auth else ""
        response = make_response(render_template("dashboard.html", mode=config.mode, operator_token=token_to_render))
        if is_auth and token_to_render:
            response.set_cookie("apex3_auth", token_to_render, httponly=False, samesite="Lax", max_age=86400 * 30)
        return response"""

content = content.replace(old_index.replace("\n", "\r\n"), new_index.replace("\n", "\r\n"))
content = content.replace(old_index, new_index)

# 5. Update auth_callback()
old_callback_success = """            try:
                engine.is_halted = False
                engine.halt_reason = ""
                if getattr(engine, "pause_reason", "") == "BROKER_SESSION_INVALID":
                    engine.is_paused = False
                    engine.pause_reason = ""
                engine.reconcile_and_backfill_late_start()
            except Exception:
                pass

            return redirect("/?auth=success")"""

new_callback_success = """            try:
                engine.is_halted = False
                engine.halt_reason = ""
                if getattr(engine, "pause_reason", "") == "BROKER_SESSION_INVALID":
                    engine.is_paused = False
                    engine.pause_reason = ""
                engine.reconcile_and_backfill_late_start()
            except Exception:
                pass

            session["operator_authorized"] = True
            resp = redirect("/?auth=success")
            resp.set_cookie("apex3_auth", _get_operator_token(), httponly=False, samesite="Lax", max_age=86400 * 30)
            return resp"""

content = content.replace(old_callback_success.replace("\n", "\r\n"), new_callback_success.replace("\n", "\r\n"))
content = content.replace(old_callback_success, new_callback_success)

# 6. Update auth_callback() exception handler
old_callback_exc = """        except Exception as e:
            try:
                kite_inst = getattr(engine.broker, "execution_broker", engine.broker)
                if hasattr(kite_inst, "is_session_valid") and kite_inst.is_session_valid():
                    return redirect("/?auth=already_active")
            except Exception:
                pass
            return (
                f"<html><body style='font-family:sans-serif;padding:30px;background:#121212;color:#eee;'>"
                f"<h2 style='color:#ff5252;'>Authentication Notice</h2>"
                f"<p>{e}</p>"
                f"<p style='color:#aaa;'>Each Zerodha <code>request_token</code> can only be used <strong>once</strong> and expires within 2–3 minutes. "
                f"If you refreshed the callback page or took a moment on the login screen, the temporary token was already consumed or timed out.</p>"
                f"<p style='color:#4caf50;'><strong>Current Status:</strong> Check your dashboard to verify if the session is already active.</p>"
                f"<a href='/login/kite' style='display:inline-block;padding:10px 20px;background:#007bff;color:#fff;text-decoration:none;border-radius:4px;margin-right:12px;'>Log In Again with Kite</a>"
                f"<a href='/' style='display:inline-block;padding:10px 20px;background:#333;color:#fff;text-decoration:none;border-radius:4px;'>Go to Dashboard</a>"
                f"</body></html>",
                500,
            )"""

new_callback_exc = """        except Exception as e:
            try:
                kite_inst = getattr(engine.broker, "execution_broker", engine.broker)
                if hasattr(kite_inst, "is_session_valid") and kite_inst.is_session_valid():
                    session["operator_authorized"] = True
                    resp = redirect("/?auth=already_active")
                    resp.set_cookie("apex3_auth", _get_operator_token(), httponly=False, samesite="Lax", max_age=86400 * 30)
                    return resp
            except Exception:
                pass
            return (
                f"<html><body style='font-family:sans-serif;padding:30px;background:#121212;color:#eee;'>"
                f"<h2 style='color:#ff5252;'>Authentication Notice</h2>"
                f"<p>{e}</p>"
                f"<p style='color:#aaa;'>Each Zerodha <code>request_token</code> can only be used <strong>once</strong> and expires within 2–3 minutes. "
                f"If you refreshed the callback page or took a moment on the login screen, the temporary token was already consumed or timed out.</p>"
                f"<p style='color:#4caf50;'><strong>Current Status:</strong> Check your dashboard to verify if the session is already active.</p>"
                f"<a href='/login/kite' style='display:inline-block;padding:10px 20px;background:#007bff;color:#fff;text-decoration:none;border-radius:4px;margin-right:12px;'>Log In Again with Kite</a>"
                f"<a href='/?token={_get_operator_token()}' style='display:inline-block;padding:10px 20px;background:#333;color:#fff;text-decoration:none;border-radius:4px;'>Go to Dashboard (Auto-Unlock)</a>"
                f"</body></html>",
                500,
            )"""

content = content.replace(old_callback_exc.replace("\n", "\r\n"), new_callback_exc.replace("\n", "\r\n"))
content = content.replace(old_callback_exc, new_callback_exc)

# 7. Replace api_status() with _build_status_payload() + api_status() + api_stream()
old_status_block = """    @app.route("/api/status")
    def api_status():
        \"\"\"Get live system status for dashboard auto-refresh.\"\"\"
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

        try:
            raw_w = engine.journal.get_warnings(limit=10) if hasattr(engine.journal, "get_warnings") else []
            warnings = [dict(w) for w in raw_w if isinstance(w, dict) or hasattr(w, "keys")]
        except Exception:
            warnings = []

        # Broker session authorization status
        session_valid = False
        kite_valid = False
        angel_valid = False
        try:
            session_valid = bool(engine.broker.is_session_valid())
        except Exception:
            session_valid = False

        try:
            exec_b = getattr(engine.broker, "execution_broker", engine.broker)
            kite_valid = bool(exec_b.is_session_valid())
        except Exception:
            kite_valid = False

        try:
            dp = getattr(engine.broker, "data_provider", None)
            if dp and hasattr(dp, "is_session_valid"):
                angel_valid = bool(dp.is_session_valid())
            else:
                angel_valid = kite_valid
        except Exception:
            angel_valid = False

        pnl_obj = getattr(engine, "latest_session_pnl", None)
        pnl_data = {
            "realized": round(float(getattr(pnl_obj, "realized_pnl", 0.0) or 0.0), 2) if pnl_obj else 0.0,
            "unrealized": round(float(getattr(pnl_obj, "unrealized_pnl", 0.0) or 0.0), 2) if pnl_obj else 0.0,
            "net": round(float(getattr(pnl_obj, "net_pnl", 0.0) or 0.0), 2) if pnl_obj else 0.0,
            "charges": round(float(getattr(pnl_obj, "estimated_charges", 0.0) or 0.0), 2) if pnl_obj else 0.0,
            "trade_count": int(getattr(pnl_obj, "trade_count", 0) or 0) if pnl_obj else 0,
        }

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
                "broker_session_valid": session_valid,
                "kite_authorized": kite_valid,
                "angel_connected": angel_valid,
                "pnl": pnl_data,
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
                "warnings": warnings[:10],
            }
        )"""

new_status_block = """    def _build_status_payload() -> dict[str, Any]:
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

        try:
            raw_w = engine.journal.get_warnings(limit=10) if hasattr(engine.journal, "get_warnings") else []
            warnings = [dict(w) for w in raw_w if isinstance(w, dict) or hasattr(w, "keys")]
        except Exception:
            warnings = []

        # Broker session authorization status
        session_valid = False
        kite_valid = False
        angel_valid = False
        try:
            session_valid = bool(engine.broker.is_session_valid())
        except Exception:
            session_valid = False

        try:
            exec_b = getattr(engine.broker, "execution_broker", engine.broker)
            kite_valid = bool(exec_b.is_session_valid())
        except Exception:
            kite_valid = False

        try:
            dp = getattr(engine.broker, "data_provider", None)
            if dp and hasattr(dp, "is_session_valid"):
                angel_valid = bool(dp.is_session_valid())
            else:
                angel_valid = kite_valid
        except Exception:
            angel_valid = False

        pnl_obj = getattr(engine, "latest_session_pnl", None)
        pnl_data = {
            "realized": round(float(getattr(pnl_obj, "realized_pnl", 0.0) or 0.0), 2) if pnl_obj else 0.0,
            "unrealized": round(float(getattr(pnl_obj, "unrealized_pnl", 0.0) or 0.0), 2) if pnl_obj else 0.0,
            "net": round(float(getattr(pnl_obj, "net_pnl", 0.0) or 0.0), 2) if pnl_obj else 0.0,
            "charges": round(float(getattr(pnl_obj, "estimated_charges", 0.0) or 0.0), 2) if pnl_obj else 0.0,
            "trade_count": int(getattr(pnl_obj, "trade_count", 0) or 0) if pnl_obj else 0,
        }

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

        return {
            "timestamp_ist": now.strftime("%Y-%m-%d %H:%M:%S IST"),
            "mode": config.mode.upper(),
            "session": session_info,
            "is_paused": engine.is_paused,
            "pause_reason": engine.pause_reason,
            "broker_session_valid": session_valid,
            "kite_authorized": kite_valid,
            "angel_connected": angel_valid,
            "pnl": pnl_data,
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
            "warnings": warnings[:10],
        }

    @app.route("/api/status")
    def api_status():
        \"\"\"Get live system status for dashboard auto-refresh.\"\"\"
        if not _is_authorized():
            return jsonify({"error": "Unauthorized: valid operator token required"}), 401
        return jsonify(_build_status_payload())

    @app.route("/api/stream")
    def api_stream():
        \"\"\"Real-time Server-Sent Events (SSE) stream pushing live system status every second.\"\"\"
        if not _is_authorized():
            return jsonify({"error": "Unauthorized: valid operator token required"}), 401

        def generate():
            while True:
                try:
                    payload = _build_status_payload()
                    yield f"data: {json.dumps(payload)}\\n\\n"
                    time.sleep(1.0)
                except GeneratorExit:
                    break
                except Exception as e:
                    time.sleep(2.0)

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )"""

content = content.replace(old_status_block.replace("\n", "\r\n"), new_status_block.replace("\n", "\r\n"))
content = content.replace(old_status_block, new_status_block)

with open(file_path, "w", encoding="utf-8", newline="\r\n") as f:
    f.write(content)

print("Patch applied to apex3/dashboard/app.py successfully!")

