"""APEX-3 Deterministic Strategy Engine.

Executes the 12-step deterministic decision pipeline:
 1. Session and compliance checks
 2. Connectivity, clock sync, and quote freshness
 3. Account reconciliation (pauses on foreign/manual positions)
 4. Broad-market regime filter (NIFTY 50 EMA-20 slope + ADX >= 22)
 5. Universe / Watchlist validation (max 3 symbols, MIS available)
 6. Opening range construction (09:15 - 09:30 IST)
 7. Stock signal generation (mirror rules for Long/Short, 8 gates)
 8. Position sizing and equal allocation across qualifying candidates
 9. Bounded entry execution (LIMIT order, verified broker fills)
10. Immediate protective stop placement for actual filled quantity
11. Position management: Monotonic trailing stop (+1R, ATR/structure)
12. End-of-day square-off: cutoff at 14:15, flatten at 14:55, verify flat by 14:58

CRITICAL: Fail-closed architecture. All decisions return one of:
WAIT, PROPOSED_LONG, PROPOSED_SHORT, MANAGE, EXIT, HALT.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from apex3.broker.interface import BrokerInterface
from apex3.config import AppConfig
from apex3.indicators.atr import compute_atr
from apex3.journal.sqlite_journal import Journal
from apex3.models import (
    Candle,
    Decision,
    GateResult,
    GateStatus,
    InstrumentInfo,
    MarginInfo,
    OpeningRange,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionState,
    Product,
    Quote,
    Side,
    StrategyDecision,
    make_broker_tag,
)
from apex3.reconciliation.reconciler import Reconciler
from apex3.reconciliation.state_machine import PositionPhase, StateMachine
from apex3.strategy.accounting import build_session_pnl
from apex3.strategy.regime import MarketRegime, evaluate_market_regime
from apex3.strategy.risk import evaluate_portfolio_risk
from apex3.strategy.schedule import SessionScheduler, SessionState
from apex3.strategy.signal import build_opening_range, evaluate_signal
from apex3.strategy.sizer import size_position
from apex3.strategy.trail import (
    calculate_initial_stop,
    calculate_stop_limit_prices,
    calculate_target_price,
    ceil_to_tick,
    evaluate_trailing_stop,
    floor_to_tick,
    round_to_tick,
)

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("apex3.engine")


def build_active_radar(cfg: dict) -> list[str]:
    """
    Merges Core Watchlist + Extended Radar + Custom User Radar into a deduplicated list.
    """
    radar_cfg = cfg.get("radar", {})
    symbols = list(radar_cfg.get("core_watchlist", []))
    if radar_cfg.get("enable_extended_radar", True):
        symbols.extend(radar_cfg.get("extended_radar", []))
    symbols.extend(radar_cfg.get("custom_user_radar", []))

    # Fallback to watchlist if radar is empty
    if not symbols:
        wl = cfg.get("watchlist", {})
        symbols = list(wl.get("symbols", [])) or list(wl.get("scanner_universe", []))

    # Deduplicate while preserving order
    seen = set()
    active_radar = []
    for sym in symbols:
        clean_sym = sym.strip().upper()
        if clean_sym and clean_sym not in seen:
            seen.add(clean_sym)
            active_radar.append(clean_sym)
    return active_radar


class StrategyEngine:
    """The central deterministic trading system for APEX-3."""

    def __init__(
        self,
        config: AppConfig,
        broker: BrokerInterface,
        journal: Journal,
        session_tag: str = "APEX3",
    ):
        self.config = config
        self.broker = broker
        self.journal = journal
        self.session_tag = session_tag

        self.scheduler = SessionScheduler(config.schedule)
        self.state_machine = StateMachine(session_tag_prefix=session_tag)
        self.reconciler = Reconciler(broker, self.state_machine, journal, session_tag)

        self.is_halted = False
        self.halt_reason = ""
        self.is_paused = False
        self.pause_reason = ""
        self._daily_loss_halt_date: Optional[date] = None
        self._current_session_date: Optional[date] = None
        self._flatten_completed: bool = False
        self._last_entry_bar_timestamp: Optional[datetime] = None
        self._execution_lock = threading.Lock()
        self.last_decisions: list[StrategyDecision] = []

        # Opening ranges cache: symbol -> OpeningRange
        self.opening_ranges: dict[str, OpeningRange] = {}

    def get_active_watchlist(self) -> list[str]:
        """Return manual symbols or the full scanner candidate universe."""
        if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict) and "radar" in self.config.raw_dict:
            radar_symbols = build_active_radar(self.config.raw_dict)
            if radar_symbols:
                return radar_symbols
        if not self.config.watchlist.scanner_enabled:
            # Manual mode: user selected symbols
            return self.config.watchlist.symbols[: self.config.limits.max_symbols]
        else:
            # Scanner mode evaluates the full universe, then ranks valid signals.
            return list(self.config.watchlist.scanner_universe)

    def set_manual_symbols(self, symbols: list[str]) -> list[str]:
        """Update the manual watchlist (max 3 symbols)."""
        valid_symbols = [s.strip().upper() for s in symbols if s.strip()][: self.config.limits.max_symbols]
        # In a real app we mutate config or create new one; here we update watchlist symbols
        object.__setattr__(self.config.watchlist, "symbols", valid_symbols)
        return valid_symbols

    def evaluate_step(self, current_time: Optional[datetime] = None) -> list[StrategyDecision]:
        """Run one complete cycle of the 12-step deterministic pipeline (thread-safe)."""
        with self._execution_lock:
            decisions = self._evaluate_step_locked(current_time)
            self.last_decisions = decisions
            return decisions

    def _evaluate_step_locked(self, current_time: Optional[datetime] = None) -> list[StrategyDecision]:
        """Internal execution pipeline under execution lock."""
        now = current_time or self.scheduler.now_ist()
        hist_from_5m = now - timedelta(days=25)
        hist_from_1m = now - timedelta(days=5)
        session_date = (now if now.tzinfo else now.replace(tzinfo=IST)).astimezone(IST).date()
        if self._current_session_date != session_date:
            self._current_session_date = session_date
            self.opening_ranges.clear()
            self._last_entry_bar_timestamp = None
            self._flatten_completed = False
        if self._daily_loss_halt_date is not None and self._daily_loss_halt_date != session_date:
            self._daily_loss_halt_date = None
            if self.pause_reason in ("DAILY_LOSS_LIMIT_BREACHED", "DAILY_PNL_ACCOUNTING_FAILED"):
                self.is_paused = False
                self.pause_reason = ""
        gates: list[GateResult] = []

        # -------------------------------------------------------------
        # STEP 1: Session and Compliance Gate
        # -------------------------------------------------------------
        session_state = self.scheduler.get_session_state(now)

        if session_state in (SessionState.HOLIDAY, SessionState.WEEKEND):
            return [
                StrategyDecision(
                    decision=Decision.WAIT,
                    timestamp_ist=now,
                    reason_codes=["NON_TRADING_DAY"],
                    explanation=f"Market closed: {session_state.name}",
                )
            ]

        # -------------------------------------------------------------
        # STEP 12 (Priority): End of Day / Flatten Window (Tracked One-Shot)
        # -------------------------------------------------------------
        if self.scheduler.should_flatten(now):
            if not self._flatten_completed:
                success, msg = self.reconciler.execute_flatten_sequence()
                decision_type = Decision.EXIT
                if success:
                    self._flatten_completed = True
                else:
                    self.journal.log_warning("EMERGENCY", "FLATTEN_FAILED", msg)

                return [
                    StrategyDecision(
                        decision=decision_type,
                        timestamp_ist=now,
                        reason_codes=["FLATTEN_WINDOW_ACTIVE"],
                        explanation=f"Flattening active: {msg}",
                        broker_action="EXIT_ALL_MIS",
                    )
                ]
            else:
                return [
                    StrategyDecision(
                        decision=Decision.WAIT,
                        timestamp_ist=now,
                        reason_codes=["FLATTEN_COMPLETED"],
                        explanation="Account confirmed flat for the session; no further actions.",
                    )
                ]

        # -------------------------------------------------------------
        # STEP 2: Connectivity and Quote Freshness Gate
        # -------------------------------------------------------------
        if not self.broker.is_session_valid():
            return [
                StrategyDecision(
                    decision=Decision.HALT,
                    timestamp_ist=now,
                    reason_codes=["BROKER_SESSION_INVALID"],
                    explanation="Broker session or token is invalid or expired",
                )
            ]

        # -------------------------------------------------------------
        # STEP 3: Account & Order Reconciliation Gate
        # -------------------------------------------------------------
        symbols = self.get_active_watchlist()
        audit = self.reconciler.audit_startup_state(symbols)
        if audit.has_unknown_positions:
            return [
                StrategyDecision(
                    decision=Decision.HALT,
                    timestamp_ist=now,
                    reason_codes=["UNKNOWN_MANUAL_POSITIONS"],
                    explanation=audit.message,
                )
            ]

        # Operator Pause check: if paused and no open positions, wait immediately
        if self.is_paused:
            open_count = sum(1 for p in self.state_machine.positions.values() if p.quantity > 0) + len(
                [p for p in self.state_machine.positions.values() if p.phase == PositionPhase.ENTERING]
            )
            if open_count == 0:
                return [
                    StrategyDecision(
                        decision=Decision.WAIT,
                        timestamp_ist=now,
                        reason_codes=["ENGINE_PAUSED"],
                        explanation=f"Strategy engine is paused: {self.pause_reason or 'Operator pause'}",
                    )
                ]

        # -------------------------------------------------------------
        # STEP 11 (Priority): Manage Open Positions
        # -------------------------------------------------------------
        decisions: list[StrategyDecision] = []

        # Step 11.A: Reconcile pending entry orders (PositionPhase.ENTERING)
        entering_managed = [
            p for p in self.state_machine.positions.values()
            if p.phase == PositionPhase.ENTERING and p.entry_order_id
        ]
        for ent_pos in entering_managed:
            try:
                entry_state = self.broker.get_order_status(ent_pos.entry_order_id)
                if not entry_state:
                    continue

                if entry_state.status == OrderStatus.COMPLETE or entry_state.filled_quantity > 0:
                    # On partial fill, cancel the unfilled remainder immediately and confirm cancellation
                    if entry_state.status != OrderStatus.COMPLETE:
                        try:
                            self.broker.cancel_order(ent_pos.entry_order_id)
                        except Exception as cancel_err:
                            logger.warning("Failed to cancel remaining entry for %s: %s", ent_pos.symbol, cancel_err)
                        refreshed_entry = self.broker.get_order_status(ent_pos.entry_order_id)
                        if refreshed_entry:
                            entry_state = refreshed_entry

                    ent_pos.quantity = entry_state.filled_quantity
                    if entry_state.average_price > 0:
                        ent_pos.average_entry = entry_state.average_price
                    ent_pos.phase = PositionPhase.OPEN_UNPROTECTED

                    # Recalculate target and R-value from actual confirmed fill price
                    ent_pos.r_value = abs(ent_pos.average_entry - ent_pos.initial_stop_price)
                    inst = self.broker.get_instrument_info(ent_pos.symbol)
                    tick = inst.tick_size if inst and inst.tick_size > 0 else 0.05
                    ent_pos.target_price = calculate_target_price(
                        side=ent_pos.side,
                        entry_price=ent_pos.average_entry,
                        initial_stop=ent_pos.initial_stop_price,
                        target_r=self.config.stops.target_r,
                        tick_size=tick,
                    )

                    exit_side = Side.SELL if ent_pos.side == Side.BUY else Side.BUY
                    trigger, limit = calculate_stop_limit_prices(
                        ent_pos.side, ent_pos.initial_stop_price, self.config.stops, tick
                    )
                    sl_req = OrderRequest(
                        symbol=ent_pos.symbol,
                        side=exit_side,
                        quantity=ent_pos.quantity,
                        product=Product.MIS,
                        order_type=OrderType.SL,
                        price=limit,
                        trigger_price=trigger,
                        tag=make_broker_tag(self.session_tag, ent_pos.symbol, "SL"),
                        strategy_id=self.session_tag,
                    )
                    sl_confirmed = False
                    try:
                        sl_order_id = self.broker.place_order(sl_req)
                        ent_pos.protective_order_id = sl_order_id
                        sl_check = self.broker.get_order_status(sl_order_id)
                        sl_qty = getattr(sl_check, "quantity", None) or getattr(getattr(sl_check, "request", None), "quantity", 0) or getattr(sl_check, "pending_quantity", 0)
                        # Treat ONLY TRIGGER_PENDING with exact matching quantity as protective
                        if sl_check and sl_check.status == OrderStatus.TRIGGER_PENDING and sl_qty == ent_pos.quantity:
                            ent_pos.phase = PositionPhase.OPEN_PROTECTED
                            sl_confirmed = True
                    except Exception as sl_err:
                        logger.error("Failed to place protective stop for pending fill %s: %s", ent_pos.symbol, sl_err)

                    if sl_confirmed:
                        self.journal.log_event(
                            event_type="ENTRY_FILLED_PROTECTED",
                            symbol=ent_pos.symbol,
                            data={
                                "quantity": ent_pos.quantity,
                                "average_entry": ent_pos.average_entry,
                                "sl_order_id": ent_pos.protective_order_id,
                            },
                            explanation=f"Entry filled for {ent_pos.symbol} (qty={ent_pos.quantity}); protective SL placed & confirmed",
                        )
                    else:
                        self._last_entry_bar_timestamp = None
                        logger.critical("EMERGENCY: Protective stop unconfirmed for filled entry in %s. Liquidating immediately.", ent_pos.symbol)
                        self.journal.log_warning(
                            severity="EMERGENCY",
                            code="PROTECTIVE_STOP_UNCONFIRMED",
                            message=f"Protective stop for filled entry in {ent_pos.symbol} failed. Liquidating immediately.",
                        )
                        try:
                            emer_exit = OrderRequest(
                                symbol=ent_pos.symbol,
                                side=exit_side,
                                quantity=ent_pos.quantity,
                                product=Product.MIS,
                                order_type=OrderType.MARKET,
                                tag=make_broker_tag(self.session_tag, ent_pos.symbol, "FLT"),
                                strategy_id=self.session_tag,
                            )
                            emer_id = self.broker.place_order(emer_exit)
                            ent_pos.exit_order_id = emer_id
                            ent_pos.phase = PositionPhase.EXITING
                        except Exception as em_err:
                            logger.critical("FATAL: Failed to execute emergency liquidation for %s: %s", ent_pos.symbol, em_err)
                            ent_pos.phase = PositionPhase.ERROR
                        self.is_paused = True
                        self.pause_reason = f"PROTECTIVE_STOP_FAILED_{ent_pos.symbol}"
                elif entry_state.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.ERROR):
                    ent_pos.phase = PositionPhase.FLAT
                    ent_pos.quantity = 0
                    self._last_entry_bar_timestamp = None
                else:
                    timeout_sec = getattr(getattr(self.config, "entry", None), "unfilled_timeout_sec", 30)
                    if ent_pos.entry_timestamp and (now - ent_pos.entry_timestamp).total_seconds() > timeout_sec:
                        self.broker.cancel_order(ent_pos.entry_order_id)
                        refreshed = self.broker.get_order_status(ent_pos.entry_order_id)
                        if refreshed and refreshed.filled_quantity > 0:
                            ent_pos.quantity = refreshed.filled_quantity
                            if refreshed.average_price > 0:
                                ent_pos.average_entry = refreshed.average_price
                            ent_pos.phase = PositionPhase.OPEN_UNPROTECTED
                            ent_pos.r_value = abs(ent_pos.average_entry - ent_pos.initial_stop_price)

                            inst = self.broker.get_instrument_info(ent_pos.symbol)
                            tick = inst.tick_size if inst and inst.tick_size > 0 else 0.05
                            ent_pos.target_price = calculate_target_price(
                                side=ent_pos.side,
                                entry_price=ent_pos.average_entry,
                                initial_stop=ent_pos.initial_stop_price,
                                target_r=self.config.stops.target_r,
                                tick_size=tick,
                            )

                            exit_side = Side.SELL if ent_pos.side == Side.BUY else Side.BUY
                            trigger, limit = calculate_stop_limit_prices(
                                ent_pos.side, ent_pos.initial_stop_price, self.config.stops, tick
                            )
                            sl_req = OrderRequest(
                                symbol=ent_pos.symbol,
                                side=exit_side,
                                quantity=ent_pos.quantity,
                                product=Product.MIS,
                                order_type=OrderType.SL,
                                price=limit,
                                trigger_price=trigger,
                                tag=make_broker_tag(self.session_tag, ent_pos.symbol, "SL"),
                                strategy_id=self.session_tag,
                            )
                            sl_confirmed = False
                            try:
                                sl_order_id = self.broker.place_order(sl_req)
                                ent_pos.protective_order_id = sl_order_id
                                sl_check = self.broker.get_order_status(sl_order_id)
                                sl_check_qty = getattr(sl_check, "quantity", None) or getattr(getattr(sl_check, "request", None), "quantity", 0) or getattr(sl_check, "pending_quantity", 0)
                                if sl_check and sl_check.status == OrderStatus.TRIGGER_PENDING and sl_check_qty == ent_pos.quantity:
                                    ent_pos.phase = PositionPhase.OPEN_PROTECTED
                                    sl_confirmed = True
                            except Exception as sl_err:
                                logger.error("Failed to place stop on timeout fill %s: %s", ent_pos.symbol, sl_err)

                            if not sl_confirmed:
                                self._last_entry_bar_timestamp = None
                                logger.critical("EMERGENCY: Protective stop unconfirmed for timeout fill in %s. Liquidating immediately.", ent_pos.symbol)
                                try:
                                    emer_exit = OrderRequest(
                                        symbol=ent_pos.symbol,
                                        side=exit_side,
                                        quantity=ent_pos.quantity,
                                        product=Product.MIS,
                                        order_type=OrderType.MARKET,
                                        tag=make_broker_tag(self.session_tag, ent_pos.symbol, "FLT"),
                                        strategy_id=self.session_tag,
                                    )
                                    emer_id = self.broker.place_order(emer_exit)
                                    ent_pos.exit_order_id = emer_id
                                    ent_pos.phase = PositionPhase.EXITING
                                except Exception as em_err:
                                    logger.critical("FATAL: Failed emergency liquidation for %s: %s", ent_pos.symbol, em_err)
                                    ent_pos.phase = PositionPhase.ERROR
                                self.is_paused = True
                                self.pause_reason = f"PROTECTIVE_STOP_FAILED_{ent_pos.symbol}"
                        else:
                            ent_pos.phase = PositionPhase.FLAT
                            ent_pos.quantity = 0
                            self._last_entry_bar_timestamp = None
            except Exception as exc:
                logger.error("Error reconciling entering position %s: %s", ent_pos.symbol, exc)

        open_managed = [p for p in self.state_machine.positions.values() if p.quantity > 0]

        for pos in open_managed:
            # If position is already in EXITING phase, poll existing exit order and reconcile
            if pos.phase == PositionPhase.EXITING:
                if pos.exit_order_id:
                    try:
                        exit_state = self.broker.get_order_status(pos.exit_order_id)
                        if exit_state:
                            if exit_state.status == OrderStatus.COMPLETE:
                                pos.phase = PositionPhase.CLOSED
                                pos.quantity = 0
                                self.journal.log_event(
                                    event_type="EXIT_ORDER_COMPLETED",
                                    symbol=pos.symbol,
                                    data={
                                        "exit_order_id": pos.exit_order_id,
                                        "fill_price": exit_state.average_price,
                                    },
                                    explanation=f"Exit order {pos.exit_order_id} filled for {pos.symbol}",
                                )
                                continue
                            elif exit_state.pending_quantity > 0:
                                pos.quantity = exit_state.pending_quantity
                                pos.phase = PositionPhase.EXITING
                            elif hasattr(exit_state, "request") and exit_state.request and exit_state.request.quantity > 0:
                                rem = max(0, exit_state.request.quantity - exit_state.filled_quantity)
                                pos.quantity = rem
                                if pos.quantity == 0:
                                    pos.phase = PositionPhase.CLOSED
                                    continue
                                else:
                                    pos.phase = PositionPhase.EXITING
                            elif exit_state.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.ERROR):
                                logger.warning(
                                    "Exit order %s for %s was %s; clearing exit_order_id for retry",
                                    pos.exit_order_id,
                                    pos.symbol,
                                    exit_state.status.value,
                                )
                                pos.exit_order_id = ""
                    except Exception as e_poll:
                        logger.error("Error polling exit order %s for %s: %s", pos.exit_order_id, pos.symbol, e_poll)
                # Do not submit a second exit order while an exit order is pending or in-flight
                continue

            # Step 11.0: Reconcile broker stop-loss order status before target / manage checks
            if pos.protective_order_id:
                try:
                    sl_order = self.broker.get_order_status(pos.protective_order_id)
                    if sl_order and sl_order.status == OrderStatus.COMPLETE:
                        self.journal.log_event(
                            event_type="STOP_LOSS_HIT",
                            symbol=pos.symbol,
                            side=pos.side.value,
                            data={
                                "stop_price": pos.current_stop_price,
                                "sl_order_id": pos.protective_order_id,
                                "fill_price": sl_order.average_price or pos.current_stop_price,
                            },
                        )
                        pos.phase = PositionPhase.CLOSED
                        pos.quantity = 0
                        decisions.append(
                            StrategyDecision(
                                decision=Decision.EXIT,
                                symbol=pos.symbol,
                                side=pos.side,
                                timestamp_ist=now,
                                reason_codes=["STOP_LOSS_FILLED_AT_BROKER"],
                                explanation=f"Protective SL order {pos.protective_order_id} filled at broker",
                                broker_action="STOP_LOSS_FILLED",
                            )
                        )
                        continue
                    elif sl_order and sl_order.status == OrderStatus.OPEN:
                        # Order triggered but resting as unfilled limit order on the book
                        q_now = self.broker.get_quote(pos.symbol)
                        px = q_now.last_price if q_now and q_now.last_price > 0 else 0.0
                        limit_px = (
                            sl_order.request.price
                            if (getattr(sl_order, "request", None) and sl_order.request.price > 0)
                            else (getattr(sl_order, "price", 0.0) or pos.current_stop_price)
                        )
                        is_gapped = False
                        if pos.side == Side.BUY and px > 0 and px < limit_px * 0.999:
                            is_gapped = True
                        elif pos.side == Side.SELL and px > 0 and px > limit_px * 1.001:
                            is_gapped = True

                        if is_gapped:
                            logger.critical(
                                "EMERGENCY: SL order %s triggered but unfilled and market gapped through limit (%s). Executing market exit!",
                                pos.protective_order_id,
                                px,
                            )
                            sl_st = None
                            try:
                                self.broker.cancel_order(pos.protective_order_id)
                                sl_st = self.broker.get_order_status(pos.protective_order_id)
                            except Exception as c_err:
                                logger.warning("Error cancelling stop during gap exit %s: %s", pos.symbol, c_err)

                            if sl_st and sl_st.status == OrderStatus.COMPLETE:
                                self.journal.log_event(
                                    event_type="STOP_LOSS_HIT",
                                    symbol=pos.symbol,
                                    side=pos.side.value,
                                    data={
                                        "stop_price": pos.current_stop_price,
                                        "sl_order_id": pos.protective_order_id,
                                        "fill_price": sl_st.average_price or pos.current_stop_price,
                                    },
                                )
                                pos.phase = PositionPhase.CLOSED
                                pos.quantity = 0
                                decisions.append(
                                    StrategyDecision(
                                        decision=Decision.EXIT,
                                        symbol=pos.symbol,
                                        side=pos.side,
                                        timestamp_ist=now,
                                        reason_codes=["STOP_LOSS_FILLED_AT_BROKER"],
                                        explanation=f"Protective SL order {pos.protective_order_id} filled at broker during gap cancel",
                                        broker_action="STOP_LOSS_FILLED",
                                    )
                                )
                                continue

                            if sl_st and sl_st.status in (OrderStatus.TRIGGER_PENDING, OrderStatus.OPEN):
                                logger.warning(
                                    "Protective SL order %s for %s cancellation still pending (%s); retrying gap exit next tick",
                                    pos.protective_order_id,
                                    pos.symbol,
                                    sl_st.status.value,
                                )
                                continue

                            sl_filled = getattr(sl_st, "filled_quantity", 0) if sl_st else 0
                            rem_qty = max(0, pos.quantity - int(sl_filled if isinstance(sl_filled, (int, float)) else 0))
                            if rem_qty <= 0:
                                pos.phase = PositionPhase.CLOSED
                                pos.quantity = 0
                                continue

                            exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY
                            emer_exit = OrderRequest(
                                symbol=pos.symbol,
                                side=exit_side,
                                quantity=rem_qty,
                                product=Product.MIS,
                                order_type=OrderType.MARKET,
                                tag=make_broker_tag(self.session_tag, pos.symbol, "EMG"),
                                strategy_id=self.session_tag,
                            )
                            emer_id = self.broker.place_order(emer_exit)
                            pos.exit_order_id = emer_id
                            pos.phase = PositionPhase.EXITING
                            decisions.append(
                                StrategyDecision(
                                    decision=Decision.EXIT,
                                    symbol=pos.symbol,
                                    side=pos.side,
                                    timestamp_ist=now,
                                    reason_codes=["STOP_TRIGGERED_UNFILLED_GAP_EXIT"],
                                    explanation=f"Protective SL limit order {pos.protective_order_id} triggered but unfilled as price gapped to {px}. Emergency market exit dispatched for {rem_qty} shares.",
                                    broker_action="EMERGENCY_GAP_EXIT",
                                )
                            )
                            continue
                except Exception as exc:
                    self.journal.log_warning(
                        severity="WARNING",
                        code="SL_STATUS_CHECK_FAILED",
                        message=f"Could not check SL order {pos.protective_order_id} for {pos.symbol}: {exc}",
                    )

            # Verify confirmed protective stop coverage
            if pos.phase not in (PositionPhase.OPEN_PROTECTED, PositionPhase.EXITING, PositionPhase.CLOSED) and pos.quantity > 0:
                logger.critical("EMERGENCY: Open position in %s has no verified protective stop (phase=%s)! Liquidating immediately.", pos.symbol, pos.phase)
                try:
                    exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY
                    flat_req = OrderRequest(
                        symbol=pos.symbol,
                        side=exit_side,
                        quantity=pos.quantity,
                        product=Product.MIS,
                        order_type=OrderType.MARKET,
                        tag=make_broker_tag(self.session_tag, pos.symbol, "EMG"),
                        strategy_id=self.session_tag,
                    )
                    flat_id = self.broker.place_order(flat_req)
                    pos.exit_order_id = flat_id
                    pos.phase = PositionPhase.EXITING
                except Exception as unprot_err:
                    logger.critical("FATAL: Failed to liquidate unprotected position in %s: %s", pos.symbol, unprot_err)
                continue

            # Manage existing position stop or take profit
            try:
                quote = self.broker.get_quote(pos.symbol)
                if not quote or not quote.is_fresh or quote.last_price <= 0:
                    logger.warning("Skipping stop/target management for %s: quote is missing or stale", pos.symbol)
                    continue
                current_price = quote.last_price
                inst_info = self.broker.get_instrument_info(pos.symbol)

                # 1. Fixed Target check (Option A: 2.0R Target)
                if self.config.stops.use_fixed_target and pos.target_price > 0:
                    target_reached = False
                    if pos.side == Side.BUY and current_price >= pos.target_price:
                        target_reached = True
                    elif pos.side == Side.SELL and current_price <= pos.target_price:
                        target_reached = True

                    if target_reached:
                        exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY
                        # Confirm stop cancellation first before sending market exit
                        stop_filled_during_cancel = False
                        sl_filled = 0
                        sl_st = None
                        if pos.protective_order_id:
                            try:
                                self.broker.cancel_order(pos.protective_order_id)
                                sl_st = self.broker.get_order_status(pos.protective_order_id)
                                if sl_st and sl_st.status == OrderStatus.COMPLETE:
                                    stop_filled_during_cancel = True
                                elif sl_st:
                                    sl_filled = getattr(sl_st, "filled_quantity", 0)
                            except Exception as c_err:
                                logger.warning("Error cancelling stop before target exit %s: %s", pos.symbol, c_err)

                        if stop_filled_during_cancel:
                            self.journal.log_event(
                                event_type="STOP_LOSS_HIT",
                                symbol=pos.symbol,
                                side=pos.side.value,
                                data={
                                    "stop_price": pos.current_stop_price,
                                    "sl_order_id": pos.protective_order_id,
                                    "fill_price": sl_st.average_price or pos.current_stop_price if sl_st else pos.current_stop_price,
                                },
                            )
                            pos.phase = PositionPhase.CLOSED
                            pos.quantity = 0
                            continue

                        if sl_st and sl_st.status in (OrderStatus.TRIGGER_PENDING, OrderStatus.OPEN):
                            logger.warning(
                                "Protective SL order %s for %s cancellation still pending (%s); deferring target exit to next cycle",
                                pos.protective_order_id,
                                pos.symbol,
                                sl_st.status.value,
                            )
                            continue

                        remaining_qty = max(0, pos.quantity - int(sl_filled if isinstance(sl_filled, (int, float)) else 0))
                        if remaining_qty <= 0:
                            pos.phase = PositionPhase.CLOSED
                            pos.quantity = 0
                            continue

                        exit_req = OrderRequest(
                            symbol=pos.symbol,
                            side=exit_side,
                            quantity=remaining_qty,
                            product=Product.MIS,
                            order_type=OrderType.MARKET,
                            price=0.0,
                            tag=make_broker_tag(self.session_tag, pos.symbol, "TGT"),
                            strategy_id=self.session_tag,
                        )
                        exit_order_id = self.broker.place_order(exit_req)
                        pos.exit_order_id = exit_order_id
                        pos.phase = PositionPhase.EXITING

                        # Confirm exit fill status before zeroing position quantity
                        exit_state = self.broker.get_order_status(exit_order_id)
                        if exit_state and exit_state.status == OrderStatus.COMPLETE:
                            pos.phase = PositionPhase.CLOSED
                            pos.quantity = 0
                        elif exit_state and exit_state.pending_quantity > 0:
                            pos.quantity = exit_state.pending_quantity
                            pos.phase = PositionPhase.EXITING
                        elif exit_state and hasattr(exit_state, "request") and exit_state.request and exit_state.request.quantity > 0:
                            rem = max(0, exit_state.request.quantity - exit_state.filled_quantity)
                            pos.quantity = rem
                            if pos.quantity == 0:
                                pos.phase = PositionPhase.CLOSED
                            else:
                                pos.phase = PositionPhase.EXITING
                        else:
                            pos.phase = PositionPhase.EXITING

                        self.journal.log_event(
                            event_type="TARGET_HIT_EXIT",
                            symbol=pos.symbol,
                            side=exit_side.value,
                            data={
                                "target_price": pos.target_price,
                                "exit_price": current_price,
                                "exit_order_id": exit_order_id,
                                "target_r": self.config.stops.target_r,
                            },
                            explanation=f"Fixed target of {self.config.stops.target_r}R reached at {current_price} for {pos.symbol}",
                        )
                        decisions.append(
                            StrategyDecision(
                                decision=Decision.EXIT,
                                symbol=pos.symbol,
                                side=pos.side,
                                timestamp_ist=now,
                                reason_codes=["TARGET_HIT"],
                                explanation=f"Fixed target of {self.config.stops.target_r}R reached at {current_price}",
                                broker_action=f"EXIT_TARGET_{pos.target_price}",
                            )
                        )
                        continue

                # 2. Trailing Stop Management (active only when trail_enabled is True)
                if self.config.stops.trail_enabled:
                    candles_5m = self.broker.get_historical(pos.symbol, "5minute", hist_from_5m, now)
                    candles_1m = self.broker.get_historical(pos.symbol, "minute", hist_from_1m, now)

                    atr_series = compute_atr(
                        [c.high for c in candles_5m],
                        [c.low for c in candles_5m],
                        [c.close for c in candles_5m],
                        period=self.config.stops.initial_atr_period,
                    )
                    atr_val = atr_series[-1] if atr_series else (current_price * 0.01)

                    trail_eval = evaluate_trailing_stop(
                        position=pos.to_position_state(),
                        current_price=current_price,
                        atr_5m=atr_val,
                        candles_1m=candles_1m,
                        config=self.config.stops,
                        costs_config=self.config.costs,
                        current_ist_time=now.time(),
                        tick_size=inst_info.tick_size,
                    )

                    # Update High/Low water mark
                    pos.best_favorable_price = trail_eval.best_favorable_price
                    pos.trail_activated = trail_eval.trail_activated

                    broker_action = "NONE"
                    if trail_eval.should_modify:
                        headroom = self.config.stops.modification_headroom
                        max_mods = self.config.stops.max_modifications_per_order
                        if pos.stop_modification_count < (max_mods - headroom):
                            if pos.protective_order_id:
                                mod_ok = self.broker.modify_order(
                                    order_id=pos.protective_order_id,
                                    price=trail_eval.limit_price,
                                    trigger_price=trail_eval.trigger_price,
                                )
                                if mod_ok:
                                    pos.current_stop_price = trail_eval.proposed_stop
                                    pos.stop_modification_count += 1
                                    broker_action = f"MODIFIED_STOP_{trail_eval.proposed_stop}"
                                    self.journal.log_stop_change(
                                        symbol=pos.symbol,
                                        side=pos.side.value,
                                        previous_stop=pos.current_stop_price,
                                        new_stop=trail_eval.proposed_stop,
                                        reason=trail_eval.reason,
                                        r_multiple=trail_eval.r_multiple,
                                        best_favorable_price=pos.best_favorable_price,
                                        modification_number=pos.stop_modification_count,
                                    )

                    decisions.append(
                        StrategyDecision(
                            decision=Decision.MANAGE,
                            symbol=pos.symbol,
                            side=pos.side,
                            timestamp_ist=now,
                            reason_codes=["MANAGING_OPEN_POSITION"],
                            explanation=trail_eval.reason,
                            open_r=trail_eval.r_multiple,
                            best_favorable_price=pos.best_favorable_price,
                            current_stop=pos.current_stop_price,
                            proposed_stop=trail_eval.proposed_stop,
                            trail_reason=trail_eval.reason,
                            broker_action=broker_action,
                        )
                    )
                else:
                    # Trailing stop disabled (Option A): maintain structural stop towards fixed target
                    if pos.side == Side.BUY:
                        pos.best_favorable_price = max(pos.best_favorable_price, current_price)
                        fav_excursion = pos.best_favorable_price - pos.average_entry
                        curr_excursion = current_price - pos.average_entry
                    else:
                        pos.best_favorable_price = min(pos.best_favorable_price, current_price) if pos.best_favorable_price > 0 else current_price
                        fav_excursion = pos.average_entry - pos.best_favorable_price
                        curr_excursion = pos.average_entry - current_price

                    hwm_r = fav_excursion / pos.r_value if pos.r_value > 0 else 0.0
                    curr_r = curr_excursion / pos.r_value if pos.r_value > 0 else 0.0
                    open_r = hwm_r

                    # One-Time Profit-Preserving / Fee-Protected Step
                    st_cfg = {}
                    if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict):
                        st_cfg = self.config.raw_dict.get("stops_and_targets", {})
                    fee_lock_enabled = st_cfg.get("fee_lock_enabled", getattr(self.config.stops, "fee_lock_enabled", True))
                    trigger_r = float(st_cfg.get("fee_lock_trigger_r", getattr(self.config.stops, "fee_lock_trigger_r", 1.60)))
                    sched_cfg = self.config.raw_dict.get("session_schedule", self.config.raw_dict.get("schedule", {})) if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict) else {}
                    late_time = sched_cfg.get("late_day_protection_time", getattr(self.config.stops, "late_day_protection_time", "14:15"))
                    late_trigger_r = float(st_cfg.get("late_day_lock_trigger_r", getattr(self.config.stops, "late_day_lock_trigger_r", 1.15)))
                    offset_pct = float(st_cfg.get("fee_lock_offset_pct", getattr(self.config.stops, "fee_lock_offset_pct", 0.0015)))
                    profit_r = float(st_cfg.get("fee_lock_profit_r", getattr(self.config.stops, "fee_lock_profit_r", 0.60)))
                    late_profit_r = float(st_cfg.get("late_day_lock_profit_r", getattr(self.config.stops, "late_day_lock_profit_r", 0.35)))

                    now_time_str = now.strftime("%H:%M")
                    target_lock_r = None
                    if fee_lock_enabled and (not getattr(pos, "fee_lock_applied", False)):
                        if hwm_r >= trigger_r:
                            target_lock_r = profit_r
                        elif now_time_str >= late_time and curr_r >= late_trigger_r:
                            target_lock_r = late_profit_r

                    broker_action = "NONE"
                    reason_code = "HOLDING_FOR_TARGET"
                    reason_codes = [reason_code]
                    explanation = f"Holding with structural SL ({pos.current_stop_price:.2f}) towards 2.0R target ({pos.target_price:.2f})"

                    if target_lock_r is not None:
                        tick = inst_info.tick_size if inst_info and inst_info.tick_size > 0 else 0.05
                        if pos.side == Side.BUY:
                            cand = max(pos.average_entry * (1.0 + offset_pct), pos.average_entry + (target_lock_r * pos.r_value))
                            protected_stop = ceil_to_tick(cand, tick)
                            valid_move = protected_stop > pos.current_stop_price and protected_stop <= current_price
                        else:
                            cand = min(pos.average_entry * (1.0 - offset_pct), pos.average_entry - (target_lock_r * pos.r_value))
                            protected_stop = floor_to_tick(cand, tick)
                            valid_move = protected_stop < pos.current_stop_price and protected_stop >= current_price

                        if valid_move:
                            prev_stop = pos.current_stop_price
                            trigger, limit = calculate_stop_limit_prices(pos.side, protected_stop, self.config.stops, tick)
                            mod_confirmed = False
                            if pos.protective_order_id:
                                try:
                                    mod_ok = self.broker.modify_order(
                                        order_id=pos.protective_order_id,
                                        price=limit,
                                        trigger_price=trigger,
                                    )
                                    if mod_ok:
                                        mod_st = self.broker.get_order_status(pos.protective_order_id)
                                        if mod_st and mod_st.status in (OrderStatus.TRIGGER_PENDING, OrderStatus.OPEN):
                                            mod_confirmed = True
                                        elif mod_st and mod_st.status == OrderStatus.COMPLETE:
                                            # Stop filled during modification
                                            self.journal.log_event(
                                                event_type="STOP_LOSS_HIT",
                                                symbol=pos.symbol,
                                                side=pos.side.value,
                                                data={
                                                    "stop_price": pos.current_stop_price,
                                                    "sl_order_id": pos.protective_order_id,
                                                    "fill_price": mod_st.average_price or pos.current_stop_price,
                                                },
                                            )
                                            pos.phase = PositionPhase.CLOSED
                                            pos.quantity = 0
                                            decisions.append(
                                                StrategyDecision(
                                                    decision=Decision.EXIT,
                                                    symbol=pos.symbol,
                                                    side=pos.side,
                                                    timestamp_ist=now,
                                                    reason_codes=["STOP_LOSS_FILLED_DURING_MODIFICATION"],
                                                    explanation=f"Protective SL order {pos.protective_order_id} filled at broker during modification",
                                                    broker_action="STOP_LOSS_FILLED",
                                                )
                                            )
                                            continue
                                except Exception as m_err:
                                    logger.error("Failed to modify stop order %s for %s: %s", pos.protective_order_id, pos.symbol, m_err)
                            else:
                                mod_confirmed = True

                            if mod_confirmed:
                                pos.current_stop_price = protected_stop
                                pos.fee_lock_applied = True
                                pos.stop_modification_count += 1
                                broker_action = f"MODIFIED_STOP_FEE_LOCK_{protected_stop}"
                                reason_codes = ["PROFIT_PRESERVING_STEP", "FEE_PROTECTED_BREAKEVEN_STEP"]
                                explanation = f"Moved stop to profit-preserving level ({protected_stop:.2f}, +{target_lock_r:.2f}R) at {open_r:.2f}R"
                                self.journal.log_stop_change(
                                    symbol=pos.symbol,
                                    side=pos.side.value,
                                    previous_stop=prev_stop,
                                    new_stop=protected_stop,
                                    reason="PROFIT_PRESERVING_STEP",
                                    r_multiple=round(open_r, 2),
                                    best_favorable_price=pos.best_favorable_price,
                                    modification_number=pos.stop_modification_count,
                                )
                            else:
                                logger.warning("Stop modification unconfirmed by broker for %s; internal stop not moved", pos.symbol)

                    decisions.append(
                        StrategyDecision(
                            decision=Decision.MANAGE,
                            symbol=pos.symbol,
                            side=pos.side,
                            timestamp_ist=now,
                            reason_codes=reason_codes if (target_lock_r is not None and valid_move and mod_confirmed) else [reason_code],
                            explanation=explanation,
                            open_r=round(open_r, 2),
                            best_favorable_price=pos.best_favorable_price,
                            current_stop=pos.current_stop_price,
                            proposed_stop=pos.current_stop_price,
                            broker_action=broker_action,
                        )
                    )
            except Exception as e:
                logger.error("Error managing position %s: %s", pos.symbol, e)

        # Daily P&L must include today's realized fills, estimated charges, and
        # broker-confirmed positions marked to current quotes. Keep this gate
        # ahead of every new-entry path so losses cannot disappear when a
        # position closes or the engine restarts.
        if self._daily_loss_halt_date == session_date:
            return decisions + [
                StrategyDecision(
                    decision=Decision.HALT,
                    timestamp_ist=now,
                    reason_codes=[self.pause_reason or "DAILY_LOSS_LIMIT_BREACHED"],
                    explanation="New entries remain paused for the rest of this trading session.",
                )
            ]

        try:
            broker_orders = self.broker.get_orders()
            broker_trades = self.broker.get_trades()
            broker_positions = self.broker.get_positions()
            risk_positions: list[PositionState] = []
            for position in broker_positions:
                if position.quantity <= 0:
                    continue
                mark = self.broker.get_quote(position.symbol)
                if not mark.is_fresh or mark.last_price <= 0:
                    raise ValueError(f"Missing or stale mark for {position.symbol}")
                unrealized = (
                    (mark.last_price - position.average_entry) * position.quantity
                    if position.side == Side.BUY
                    else (position.average_entry - mark.last_price) * position.quantity
                )
                risk_positions.append(
                    replace(
                        position,
                        current_price=mark.last_price,
                        unrealized_pnl=unrealized,
                        realized_pnl=0.0,
                    )
                )

            session_pnl = build_session_pnl(
                trades=broker_trades,
                orders=broker_orders,
                open_positions=risk_positions,
                session_tag=self.session_tag,
                session_date=session_date,
                costs=self.config.costs,
            )
            margin_info = self.broker.get_margins()
            daily_risk_status = evaluate_portfolio_risk(
                open_positions=risk_positions,
                margin_info=margin_info,
                proposed_trade_risk_rupees=0.0,
                limits_config=self.config.limits,
                risk_config=self.config.risk,
                additional_realized_pnl=session_pnl.realized_pnl,
                estimated_charges=session_pnl.estimated_charges,
            )
        except Exception as exc:
            self._daily_loss_halt_date = session_date
            self.is_paused = True
            self.pause_reason = "DAILY_PNL_ACCOUNTING_FAILED"
            self.journal.log_warning(
                severity="CRITICAL",
                code="DAILY_PNL_ACCOUNTING_FAILED",
                message=f"Could not safely calculate today's strategy P&L: {exc}",
            )
            return decisions + [
                StrategyDecision(
                    decision=Decision.HALT,
                    timestamp_ist=now,
                    reason_codes=["DAILY_PNL_ACCOUNTING_FAILED"],
                    explanation="New entries blocked because daily P&L could not be verified from broker executions and position marks.",
                )
            ]

        if daily_risk_status.is_daily_loss_breached:
            self._daily_loss_halt_date = session_date
            self.is_paused = True
            self.pause_reason = "DAILY_LOSS_LIMIT_BREACHED"
            has_open = any(p.quantity > 0 for p in self.state_machine.positions.values())
            flatten_msg = ""
            if has_open:
                logger.critical("Daily loss limit breached with open positions! Squaring off immediately.")
                _, flatten_msg = self.reconciler.execute_flatten_sequence()

            self.journal.log_warning(
                severity="CRITICAL",
                code="DAILY_LOSS_LIMIT_BREACHED",
                message=f"{daily_risk_status.gate.reason}. {flatten_msg}".strip(),
                data={
                    "net_day_pnl": session_pnl.net_pnl,
                    "realized_pnl": session_pnl.realized_pnl,
                    "unrealized_pnl": session_pnl.unrealized_pnl,
                    "estimated_charges": session_pnl.estimated_charges,
                    "trade_count": session_pnl.trade_count,
                },
            )
            return decisions + [
                StrategyDecision(
                    decision=Decision.HALT,
                    timestamp_ist=now,
                    reason_codes=["DAILY_LOSS_LIMIT_BREACHED"],
                    explanation=f"{daily_risk_status.gate.reason}. {flatten_msg}".strip(),
                    gates=[daily_risk_status.gate],
                    broker_action="DAILY_LOSS_BREACH_FLATTEN" if has_open else "",
                )
            ]

        # Enforce engine pause state: if paused, block all new entries
        if self.is_paused:
            logger.info("Strategy engine is PAUSED (%s). New entries are blocked.", self.pause_reason or "Operator pause")
            if not decisions:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.WAIT,
                        timestamp_ist=now,
                        reason_codes=["ENGINE_PAUSED"],
                        explanation=f"New entries blocked because engine is paused: {self.pause_reason or 'Operator pause'}",
                    )
                )
            return decisions

        # Live mode compliance checks for NEW entries
        if self.config.mode == "live":
            if not self.config.secrets.live_trading_enabled:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.HALT,
                        timestamp_ist=now,
                        reason_codes=["LIVE_TRADING_FLAG_DISABLED"],
                        explanation="Live trading is configured in config.yaml but LIVE_TRADING_ENABLED env var is false",
                    )
                )
                return decisions
            if not self.config.secrets.static_ip_configured:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.HALT,
                        timestamp_ist=now,
                        reason_codes=["STATIC_IP_NOT_CONFIGURED"],
                        explanation="Live trading requires static IP confirmation (effective 1 April 2026)",
                    )
                )
                return decisions
            if not self.config.secrets.sebi_compliance_acknowledged:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.HALT,
                        timestamp_ist=now,
                        reason_codes=["SEBI_COMPLIANCE_NOT_ACKNOWLEDGED"],
                        explanation="SEBI retail algo compliance acknowledgement required for live trading",
                    )
                )
                return decisions

        if session_state == SessionState.SPECIAL_SESSION:
            decisions.append(
                StrategyDecision(
                    decision=Decision.WAIT,
                    timestamp_ist=now,
                    reason_codes=["SPECIAL_SESSION_NO_MIS"],
                    explanation="Special session active (e.g. Muhurat Trading). Intraday MIS trading blocked by policy.",
                )
            )
            return decisions

        if session_state == SessionState.PRE_OPEN:
            decisions.append(
                StrategyDecision(
                    decision=Decision.WAIT,
                    timestamp_ist=now,
                    reason_codes=["PRE_OPEN_SESSION"],
                    explanation="Pre-open session (before 09:15 IST). Observing and validating data.",
                )
            )
            return decisions

        # If already at max simultaneous positions, cannot take new entries
        active_managed_count = sum(
            1 for p in self.state_machine.positions.values()
            if p.quantity > 0 or p.phase in (PositionPhase.ENTERING, PositionPhase.OPEN_UNPROTECTED, PositionPhase.EXITING)
        )
        if active_managed_count >= self.config.limits.max_symbols:
            if not decisions:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.WAIT,
                        timestamp_ist=now,
                        reason_codes=["MAX_POSITIONS_REACHED"],
                        explanation=f"Already holding maximum simultaneous positions ({active_managed_count}/{self.config.limits.max_symbols})",
                    )
                )
            return decisions

        # Slot-2 Lock Gate: Only allow a 2nd position if Slot 1 is already in profit >= req_r (if req_r > 0)
        active_positions = [
            p for p in self.state_machine.positions.values()
            if p.quantity > 0 or p.phase in (PositionPhase.ENTERING, PositionPhase.OPEN_UNPROTECTED)
        ]
        if len(active_positions) == 1:
            risk_raw = self.config.raw_dict.get("risk_management", {}) if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict) else {}
            req_r = float(risk_raw.get("second_slot_requires_slot1_profit_r", 0.0))
            if req_r > 0.0:
                first_pos = active_positions[0]
                mark = self.broker.get_quote(first_pos.symbol)
                curr_px = mark.last_price if (mark and mark.is_fresh and mark.last_price > 0) else first_pos.average_entry
                r_val = first_pos.r_value if first_pos.r_value > 0 else abs(first_pos.average_entry - first_pos.initial_stop_price)
                unrealized_r = 0.0
                if r_val > 0:
                    unrealized_r = (curr_px - first_pos.average_entry) / r_val if first_pos.side == Side.BUY else (first_pos.average_entry - curr_px) / r_val

                if unrealized_r < req_r:
                    decisions.append(
                        StrategyDecision(
                            decision=Decision.WAIT,
                            timestamp_ist=now,
                            reason_codes=["SECOND_SLOT_LOCKED_UNTIL_0.5R", "SECOND_SLOT_LOCKED_UNTIL_PROFIT"],
                            explanation=f"Slot 2 locked: Active position in {first_pos.symbol} has unrealized R of {unrealized_r:.2f}R (requires >= {req_r:.2f}R)",
                        )
                    )
                    return decisions

        # -------------------------------------------------------------
        # STEP 6: Opening Range Period Check (09:15 - 09:30)
        # -------------------------------------------------------------
        if self.scheduler.is_opening_range_period(now):
            # In opening range build phase - update ranges
            session_date = (now if now.tzinfo else now.replace(tzinfo=IST)).astimezone(IST).date()
            for sym in symbols:
                candles_0915_0930 = self.broker.get_historical(sym, "5minute", hist_from_5m, now)
                self.opening_ranges[sym] = build_opening_range(sym, candles_0915_0930, session_date=session_date)

            return [
                StrategyDecision(
                    decision=Decision.WAIT,
                    timestamp_ist=now,
                    reason_codes=["BUILDING_OPENING_RANGE"],
                    explanation="Building first-15-minute opening range (09:15 - 09:30 IST). No entries permitted.",
                )
            ]

        # Check if outside entry window (e.g. after 14:15 IST)
        if not self.scheduler.can_enter(now):
            if not decisions:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.WAIT,
                        timestamp_ist=now,
                        reason_codes=["OUTSIDE_ENTRY_WINDOW"],
                        explanation="Entry window closed (14:15 IST cutoff passed). Managing existing exposure only.",
                    )
                )
            return decisions

        # -------------------------------------------------------------
        # STEP 4: Market Regime Filter Gate
        # -------------------------------------------------------------
        bench_candles = self.broker.get_historical(self.config.regime.benchmark_symbol, "5minute", hist_from_5m, now)
        bench_quote = self.broker.get_quote(self.config.regime.benchmark_symbol)
        regime_eval = evaluate_market_regime(bench_candles, self.config.regime, bench_quote)

        if regime_eval.regime == MarketRegime.NEUTRAL:
            if not decisions:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.WAIT,
                        timestamp_ist=now,
                        reason_codes=["REGIME_NEUTRAL_OR_MIXED"],
                        explanation=f"Broad-market regime is neutral: {regime_eval.gate.reason}",
                        gates=[regime_eval.gate],
                    )
                )
            return decisions

        # -------------------------------------------------------------
        # STEP 5, 7, 8: Candidate Signal Evaluation & Sizing
        # -------------------------------------------------------------
        qualified_candidates = []
        evaluated_signals = {}

        core_list = set()
        core_mult = 1.35
        if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict):
            radar_cfg = self.config.raw_dict.get("radar", {})
            core_list = set(radar_cfg.get("core_watchlist", []))
            core_mult = float(radar_cfg.get("core_conviction_multiplier", 1.35))

        for sym in symbols:
            # Skip if already open or in flight
            if any(
                p.symbol == sym and (p.quantity > 0 or p.phase in (PositionPhase.ENTERING, PositionPhase.OPEN_UNPROTECTED, PositionPhase.EXITING))
                for p in self.state_machine.positions.values()
            ):
                continue

            inst = self.broker.get_instrument_info(sym)
            quote = self.broker.get_quote(sym)
            stock_candles = self.broker.get_historical(sym, "5minute", hist_from_5m, now)

            # Build/retrieve opening range
            or_range = self.opening_ranges.get(sym)
            if or_range is None or not or_range.is_complete:
                session_date = (now if now.tzinfo else now.replace(tzinfo=IST)).astimezone(IST).date()
                or_range = build_opening_range(sym, stock_candles, session_date=session_date)
                self.opening_ranges[sym] = or_range

            is_core = (sym in core_list) if core_list else True
            sig = evaluate_signal(
                symbol=sym,
                stock_candles_5m=stock_candles,
                benchmark_candles_5m=bench_candles,
                opening_range=or_range,
                market_regime=regime_eval.regime,
                instrument=inst,
                quote=quote,
                config=self.config.signal,
                is_entry_window=True,
                is_core_symbol=is_core,
            )
            evaluated_signals[sym] = (sig, inst, stock_candles, quote)
            if sig.decision in (Decision.PROPOSED_LONG, Decision.PROPOSED_SHORT):
                qualified_candidates.append(sym)

        if not qualified_candidates:
            # No symbol passed all signal gates
            reasons = [f"{s}: {evaluated_signals[s][0].explanation}" for s in evaluated_signals]
            if not decisions:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.WAIT,
                        timestamp_ist=now,
                        reason_codes=["NO_QUALIFYING_CANDIDATES"],
                        explanation="; ".join(reasons) if reasons else "Watchlist empty or no signals",
                    )
                )
            return decisions

        # Rank qualified candidates by Conviction Score: (|RS| * 100 * relative_volume) / max(spread_bps, 1.0)

        def compute_conviction(sym_name: str) -> float:
            sig_item, _, _, quote_item = evaluated_signals[sym_name]
            mid = (quote_item.bid + quote_item.ask) / 2.0 if (quote_item and quote_item.bid and quote_item.ask) else 0.0
            spread_bps = ((quote_item.ask - quote_item.bid) / mid) * 10000.0 if mid > 0 else 1.0
            spread_floor = max(spread_bps, 1.0)
            score = (abs(sig_item.relative_strength) * 100.0 * sig_item.relative_volume) / spread_floor
            if sym_name in core_list:
                score *= core_mult
            return score

        qualified_candidates.sort(key=compute_conviction, reverse=True)

        # Completed-bar deduplication: at most 1 entry per 5-minute candle
        if self._last_entry_bar_timestamp is not None:
            qualified_candidates = [
                sym for sym in qualified_candidates
                if not (
                    evaluated_signals.get(sym)
                    and len(evaluated_signals[sym]) > 2
                    and evaluated_signals[sym][2]
                    and evaluated_signals[sym][2][-1].timestamp == self._last_entry_bar_timestamp
                )
            ]

        max_new = 1
        if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict):
            max_new = int(self.config.raw_dict.get("risk_management", {}).get("max_new_entries_per_bar", 1))

        # Sizing and Risk for qualified candidates
        num_qualified = len(qualified_candidates)
        entries_submitted = 0

        for sym in qualified_candidates:
            if entries_submitted >= max_new:
                break
            if len(risk_positions) >= self.config.limits.max_symbols:
                break
            sig, inst, stock_candles, _ = evaluated_signals[sym]
            if self._last_entry_bar_timestamp is not None and stock_candles and stock_candles[-1].timestamp == self._last_entry_bar_timestamp:
                logger.debug("Bar %s already has an entry taken for %s. Skipping.", self._last_entry_bar_timestamp, sym)
                continue
            sig_price = sig.signal_price
            side = sig.side

            # Compute marketable limit entry price up-front before sizing & stops
            tick = inst.tick_size if inst and inst.tick_size > 0 else 0.05
            slip_cfg = getattr(getattr(self.config, "entry", None), "max_slippage_pct", 0.15)
            slip_pct = (slip_cfg / 100.0) if slip_cfg > 0.5 else (slip_cfg if slip_cfg > 0 else 0.0015)
            if side == Side.BUY:
                cand_price = round_to_tick(sig_price + tick, tick_size=tick)
                cap_price = round_to_tick(sig_price * (1.0 + slip_pct), tick_size=tick)
                marketable_entry_price = min(cand_price, cap_price)
            else:
                cand_price = round_to_tick(sig_price - tick, tick_size=tick)
                cap_price = round_to_tick(sig_price * (1.0 - slip_pct), tick_size=tick)
                marketable_entry_price = max(cand_price, cap_price)
            entry_price = marketable_entry_price

            # Calculate ATR for initial stop
            atr_series = compute_atr(
                [c.high for c in stock_candles],
                [c.low for c in stock_candles],
                [c.close for c in stock_candles],
                period=self.config.stops.initial_atr_period,
            )
            atr_val = atr_series[-1] if atr_series else (entry_price * 0.01)

            # Structural initial stop (true 5-bar structural swing anchor)
            lookback_bars = getattr(self.config.stops, "swing_lookback_bars", 5)
            lookback_candles = stock_candles[-lookback_bars:] if len(stock_candles) >= lookback_bars else stock_candles
            swing_low_5 = min(c.low for c in lookback_candles) if lookback_candles else stock_candles[-1].low
            swing_high_5 = max(c.high for c in lookback_candles) if lookback_candles else stock_candles[-1].high
            initial_stop = calculate_initial_stop(
                side=side,
                entry_price=entry_price,
                signal_low=swing_low_5,
                signal_high=swing_high_5,
                atr_value=atr_val,
                config=self.config.stops,
                tick_size=inst.tick_size,
            )

            # Gate 9 Stop-width bounds: 0.55% <= R% <= 0.90%
            min_stop_pct = getattr(self.config.signal, "min_stop_distance_pct", 0.0055)
            max_stop_pct = getattr(self.config.signal, "max_stop_distance_pct", 0.0090)
            stop_dist_pct = abs(entry_price - initial_stop) / entry_price if entry_price > 0 else 0.0
            if stop_dist_pct < min_stop_pct or stop_dist_pct > max_stop_pct:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.WAIT,
                        symbol=sym,
                        timestamp_ist=now,
                        reason_codes=["STOP_WIDTH_OUT_OF_BOUNDS"],
                        explanation=f"Stop width {stop_dist_pct*100:.2f}% is outside allowed bounds [{min_stop_pct*100:.2f}%, {max_stop_pct*100:.2f}%]",
                    )
                )
                continue

            # Estimate order margin from broker
            dummy_req = OrderRequest(
                symbol=sym,
                side=side,
                quantity=100,
                price=entry_price,
                product=Product.MIS,
                order_type=OrderType.LIMIT,
            )
            order_margin_est = self.broker.estimate_order_margin(dummy_req)

            # Sizing calculation
            sizing = size_position(
                symbol=sym,
                side=side,
                entry_price=entry_price,
                stop_price=initial_stop,
                num_qualified_symbols=num_qualified,
                margin_info=margin_info,
                instrument=inst,
                limits_config=self.config.limits,
                risk_config=self.config.risk,
                order_margin_estimate=order_margin_est,
            )

            if not sizing.is_valid or sizing.quantity <= 0:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.WAIT,
                        symbol=sym,
                        side=side,
                        timestamp_ist=now,
                        reason_codes=[sizing.rejection_code or "SIZING_REJECTED"],
                        explanation=f"Sizing rejected for {sym}: {sizing.rejection_reason}",
                        gates=[sizing.gate],
                    )
                )
                continue

            # Portfolio Risk Gate
            risk_status = evaluate_portfolio_risk(
                open_positions=risk_positions,
                margin_info=margin_info,
                proposed_trade_risk_rupees=sizing.trade_risk_rupees,
                limits_config=self.config.limits,
                risk_config=self.config.risk,
                additional_realized_pnl=session_pnl.realized_pnl,
                estimated_charges=session_pnl.estimated_charges,
            )

            if not risk_status.can_open_new_trade:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.WAIT,
                        symbol=sym,
                        side=side,
                        timestamp_ist=now,
                        reason_codes=["PORTFOLIO_RISK_LIMIT_BREACHED"],
                        explanation=f"Portfolio risk check failed: {risk_status.gate.reason}",
                        gates=[risk_status.gate],
                    )
                )
                continue

            # -------------------------------------------------------------
            # STEP 9 & 10: Proposal Ready & Execution
            # -------------------------------------------------------------
            tag = make_broker_tag(self.session_tag, sym, f"E{now.strftime('%H%M')}")
            broker_action = "PLACE_ENTRY_AND_PROTECTION"

            entry_req = OrderRequest(
                symbol=sym,
                side=side,
                quantity=sizing.quantity,
                product=Product.MIS,
                order_type=OrderType.LIMIT,
                price=marketable_entry_price,
                tag=tag,
                strategy_id=self.session_tag,
            )

            try:
                entry_order_id = self.broker.place_order(entry_req)
                entry_state = self.broker.get_order_status(entry_order_id)
                if entry_state and entry_state.status in (OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.ERROR):
                    self._last_entry_bar_timestamp = None
                    rej_msg = getattr(entry_state, "rejection_reason", None) or getattr(entry_state, "status_message", "Order rejected")
                    self.journal.log_warning(
                        severity="ERROR",
                        code="ENTRY_ORDER_REJECTED",
                        message=f"Entry order {entry_order_id} for {sym} was {entry_state.status.value}: {rej_msg}",
                    )
                    decisions.append(
                        StrategyDecision(
                            decision=Decision.WAIT,
                            symbol=sym,
                            side=side,
                            timestamp_ist=now,
                            reason_codes=["ENTRY_REJECTED"],
                            explanation=f"Entry order {entry_order_id} {entry_state.status.value}: {rej_msg}",
                        )
                    )
                    continue

                managed = self.state_machine.get_or_create_position(sym)
                managed.side = side
                managed.entry_order_id = entry_order_id
                managed.strategy_tag = tag
                managed.entry_timestamp = now
                managed.requested_quantity = sizing.quantity
                managed.initial_stop_price = initial_stop
                managed.current_stop_price = initial_stop
                managed.target_price = calculate_target_price(
                    side=side,
                    entry_price=marketable_entry_price,
                    initial_stop=initial_stop,
                    target_r=self.config.stops.target_r,
                    tick_size=inst.tick_size,
                )

                # Record bar timestamp deduplication
                self._last_entry_bar_timestamp = stock_candles[-1].timestamp if stock_candles else now

                entry_filled_qty = getattr(entry_state, "filled_quantity", 0)
                is_filled_qty_positive = isinstance(entry_filled_qty, (int, float)) and entry_filled_qty > 0
                is_complete = getattr(entry_state, "status", None) == OrderStatus.COMPLETE

                if entry_state and (is_complete or is_filled_qty_positive):
                    # On partial fill, cancel the unfilled remainder immediately and confirm cancellation
                    if is_filled_qty_positive and not is_complete:
                        try:
                            self.broker.cancel_order(entry_order_id)
                        except Exception as cancel_err:
                            logger.warning("Failed to cancel remaining entry for %s: %s", sym, cancel_err)
                        refreshed_st = self.broker.get_order_status(entry_order_id)
                        if refreshed_st:
                            entry_state = refreshed_st
                            entry_filled_qty = getattr(entry_state, "filled_quantity", entry_filled_qty)

                    actual_qty = int(entry_filled_qty) if is_filled_qty_positive else sizing.quantity
                    entry_avg_px = getattr(entry_state, "average_price", 0.0)
                    is_avg_px_positive = isinstance(entry_avg_px, (int, float)) and entry_avg_px > 0
                    actual_entry = float(entry_avg_px) if is_avg_px_positive else marketable_entry_price
                    managed.quantity = actual_qty
                    managed.average_entry = actual_entry
                    managed.best_favorable_price = actual_entry
                    managed.r_value = abs(actual_entry - initial_stop)
                    managed.target_price = calculate_target_price(
                        side=side,
                        entry_price=actual_entry,
                        initial_stop=initial_stop,
                        target_r=self.config.stops.target_r,
                        tick_size=inst.tick_size,
                    )
                    managed.phase = PositionPhase.OPEN_UNPROTECTED

                    # Step 10: Place protective stop immediately for filled quantity
                    exit_side = Side.SELL if side == Side.BUY else Side.BUY
                    trigger, limit = calculate_stop_limit_prices(side, initial_stop, self.config.stops, inst.tick_size)

                    sl_req = OrderRequest(
                        symbol=sym,
                        side=exit_side,
                        quantity=actual_qty,
                        product=Product.MIS,
                        order_type=OrderType.SL,
                        price=limit,
                        trigger_price=trigger,
                        tag=make_broker_tag(self.session_tag, sym, "SL"),
                        strategy_id=self.session_tag,
                    )
                    sl_confirmed = False
                    sl_order_id = ""
                    try:
                        sl_order_id = self.broker.place_order(sl_req)
                        managed.protective_order_id = sl_order_id
                        sl_state = self.broker.get_order_status(sl_order_id)
                        sl_qty = getattr(sl_state, "quantity", None) or getattr(getattr(sl_state, "request", None), "quantity", 0) or getattr(sl_state, "pending_quantity", 0)
                        if sl_state and sl_state.status == OrderStatus.TRIGGER_PENDING and sl_qty == actual_qty:
                            managed.phase = PositionPhase.OPEN_PROTECTED
                            sl_confirmed = True
                        else:
                            logger.error("Stop order %s in unconfirmed state: %s", sl_order_id, getattr(sl_state, 'status', None))
                    except Exception as sl_err:
                        logger.error("Failed to place protective stop for %s: %s", sym, sl_err)

                    if not sl_confirmed:
                        self._last_entry_bar_timestamp = None
                        # Recovery path: liquidate filled position immediately to prevent naked risk
                        logger.critical("EMERGENCY: Protective stop placement failed/unconfirmed for %s! Liquidating immediately.", sym)
                        self.journal.log_warning(
                            severity="EMERGENCY",
                            code="PROTECTIVE_STOP_UNCONFIRMED",
                            message=f"Protective stop for {sym} failed or unconfirmed. Executing immediate market liquidation.",
                        )
                        try:
                            flat_req = OrderRequest(
                                symbol=sym,
                                side=exit_side,
                                quantity=actual_qty,
                                product=Product.MIS,
                                order_type=OrderType.MARKET,
                                tag=make_broker_tag(self.session_tag, sym, "FLT"),
                                strategy_id=self.session_tag,
                            )
                            emer_flat_id = self.broker.place_order(flat_req)
                            managed.exit_order_id = emer_flat_id
                            managed.phase = PositionPhase.EXITING
                        except Exception as flat_err:
                            logger.critical("FATAL: Emergency liquidation order failed for %s: %s", sym, flat_err)
                            managed.phase = PositionPhase.ERROR
                        self.is_paused = True
                        self.pause_reason = f"PROTECTIVE_STOP_FAILED_{sym}"
                        continue

                    risk_positions.append(
                        PositionState(
                            symbol=sym,
                            side=side,
                            quantity=actual_qty,
                            average_entry=actual_entry,
                            current_price=actual_entry,
                            protective_stop_price=initial_stop,
                            initial_stop_price=initial_stop,
                            is_protected=True,
                            strategy_tag=tag,
                        )
                    )

                    self.journal.log_event(
                        event_type="ENTRY_AND_PROTECTION_PLACED",
                        symbol=sym,
                        side=side.value,
                        data={
                            "entry_order_id": entry_order_id,
                            "sl_order_id": sl_order_id,
                            "quantity": actual_qty,
                            "entry_price": actual_entry,
                            "initial_stop": initial_stop,
                            "target_price": managed.target_price,
                        },
                        explanation=f"Placed entry, target ({managed.target_price:.2f}), and protective stop for {sym} ({actual_qty} shares)",
                    )
                    broker_action = f"BUY_ENTRY_AND_SL_{initial_stop}" if side == Side.BUY else f"SELL_ENTRY_AND_SL_{initial_stop}"
                else:
                    # Accepted on orderbook but unfilled yet — do NOT place naked SL
                    managed.quantity = 0
                    managed.average_entry = marketable_entry_price
                    managed.best_favorable_price = marketable_entry_price
                    managed.r_value = abs(marketable_entry_price - initial_stop)
                    managed.phase = PositionPhase.ENTERING
                    actual_qty = sizing.quantity
                    actual_entry = marketable_entry_price

                    risk_positions.append(
                        PositionState(
                            symbol=sym,
                            side=side,
                            quantity=sizing.quantity,
                            average_entry=marketable_entry_price,
                            current_price=marketable_entry_price,
                            protective_stop_price=initial_stop,
                            initial_stop_price=initial_stop,
                            is_protected=False,
                            strategy_tag=tag,
                        )
                    )

                    self.journal.log_event(
                        event_type="ENTRY_ORDER_PENDING",
                        symbol=sym,
                        side=side.value,
                        data={
                            "entry_order_id": entry_order_id,
                            "quantity": sizing.quantity,
                            "order_price": marketable_entry_price,
                        },
                        explanation=f"Submitted {side.value} entry {sizing.quantity} {sym} @ {marketable_entry_price:.2f}; awaiting fill before placing SL",
                    )
                    broker_action = f"SUBMITTED_ENTRY_{marketable_entry_price}"

                decisions.append(
                    StrategyDecision(
                        decision=sig.decision,
                        symbol=sym,
                        side=side,
                        timestamp_ist=now,
                        reason_codes=["ENTRY_SUBMITTED"],
                        explanation=f"Submitted {sig.decision.value} for {actual_qty} shares of {sym}",
                        gates=sig.gates + [sizing.gate, risk_status.gate],
                        quantity=actual_qty,
                        entry_limit=actual_entry,
                        initial_stop=initial_stop,
                        risk_rupees=sizing.trade_risk_rupees,
                        estimated_margin=sizing.notional_value / sizing.effective_leverage,
                        max_notional=sizing.per_symbol_budget,
                        broker_action=broker_action,
                    )
                )
                entries_submitted += 1
                if entries_submitted >= max_new:
                    break
            except Exception as e:
                logger.error("Failed to place order for %s: %s", sym, e)
                decisions.append(
                    StrategyDecision(
                        decision=Decision.HALT,
                        symbol=sym,
                        side=side,
                        timestamp_ist=now,
                        reason_codes=["ORDER_PLACEMENT_EXCEPTION"],
                        explanation=f"Order placement failed: {e}",
                    )
                )

        return decisions


# ---------------------------------------------------------------------------
# Cross-Sectional Radar Merger & Conviction Ranker Loop
# ---------------------------------------------------------------------------




def select_best_entries_for_bar(
    benchmark_bar: BenchmarkSnapshot,
    candidate_bars: dict[str, StockCandidateBar],
    open_positions: dict[str, dict],
    realized_daily_pnl_net: float,
    cfg: dict,
    kite_client=None,
) -> list[tuple[TradeSignal, PositionSizeResult]]:
    """
    Evaluates all stocks on the active radar at 5-minute bar close, ranks passing
    candidates by Conviction Score, and returns the #1 trade to execute.
    """
    from apex3.strategy.regime import BenchmarkSnapshot, MarketRegime, evaluate_market_regime
    from apex3.strategy.signal import StockCandidateBar, TradeSignal, evaluate_candidate_signal
    from apex3.strategy.sizer import PositionSizeResult, calculate_position_size

    acc_cfg = cfg.get("account", {})
    risk_cfg = cfg.get("risk_management", cfg.get("risk", {}))

    # Check Daily Loss Limit (-₹240 on ₹15,000 capital)
    starting_cap = float(acc_cfg.get("starting_capital", 15000.0))
    daily_loss_pct = float(risk_cfg.get("daily_loss_limit_pct", 0.0160))
    if daily_loss_pct > 0.1:
        daily_loss_pct /= 100.0
    max_daily_loss = -1.0 * starting_cap * daily_loss_pct
    if realized_daily_pnl_net <= max_daily_loss:
        return []

    max_slots = int(risk_cfg.get("max_concurrent_positions", 2))
    if len(open_positions) >= max_slots:
        return []

    # Slot 2 Gate: If req_r > 0, only allow 2nd position when Slot 1 is >= req_r in profit
    if len(open_positions) == 1:
        req_r = float(risk_cfg.get("second_slot_requires_slot1_profit_r", 0.0))
        if req_r > 0.0:
            first_pos = next(iter(open_positions.values()))
            if first_pos.get("unrealized_r", 0.0) < req_r:
                return []

    # Step 1: Evaluate NIFTY 50 Regime
    regime = evaluate_market_regime(benchmark_bar, cfg)
    regime_val = regime.value if hasattr(regime, "value") else str(regime)
    if regime_val == "NEUTRAL":
        return []

    # Step 2: Scan all symbols in merged radar
    active_radar = build_active_radar(cfg)
    passing_signals: list[TradeSignal] = []

    for symbol in active_radar:
        if symbol in open_positions or symbol not in candidate_bars:
            continue
        sig = evaluate_candidate_signal(
            bar=candidate_bars[symbol],
            regime=regime,
            active_positions_count=len(open_positions),
            cfg=cfg,
        )
        if sig is not None:
            passing_signals.append(sig)

    if not passing_signals:
        return []

    # Step 3: Sort by highest Conviction Score and take at most max_new_entries_per_bar (1)
    passing_signals.sort(key=lambda s: s.conviction_score, reverse=True)
    max_new = int(risk_cfg.get("max_new_entries_per_bar", 1))

    selected_orders = []
    for sig in passing_signals[:max_new]:
        size_res = calculate_position_size(
            entry_price=sig.entry_price,
            risk_per_share=sig.levels.risk_per_share,
            target_price=sig.levels.target_price,
            cfg=cfg,
            kite_client=kite_client,
        )
        if size_res is not None:
            selected_orders.append((sig, size_res))

    return selected_orders
