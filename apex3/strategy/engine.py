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
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time
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
        """Run one complete cycle of the 12-step deterministic pipeline.

        Returns a list of StrategyDecision (one per evaluated symbol or action).
        """
        now = current_time or self.scheduler.now_ist()
        session_date = (now if now.tzinfo else now.replace(tzinfo=IST)).astimezone(IST).date()
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

        if self.config.mode == "live":
            # Live mode validation checks
            if not self.config.secrets.live_trading_enabled:
                return [
                    StrategyDecision(
                        decision=Decision.HALT,
                        timestamp_ist=now,
                        reason_codes=["LIVE_TRADING_FLAG_DISABLED"],
                        explanation="Live trading is configured in config.yaml but LIVE_TRADING_ENABLED env var is false",
                    )
                ]
            if not self.config.secrets.static_ip_configured:
                return [
                    StrategyDecision(
                        decision=Decision.HALT,
                        timestamp_ist=now,
                        reason_codes=["STATIC_IP_NOT_CONFIGURED"],
                        explanation="Live trading requires static IP confirmation (effective 1 April 2026)",
                    )
                ]
            if not self.config.secrets.sebi_compliance_acknowledged:
                return [
                    StrategyDecision(
                        decision=Decision.HALT,
                        timestamp_ist=now,
                        reason_codes=["SEBI_COMPLIANCE_NOT_ACKNOWLEDGED"],
                        explanation="SEBI retail algo compliance acknowledgement required for live trading",
                    )
                ]

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
        # STEP 12 (Priority): End of Day / Flatten Window
        # -------------------------------------------------------------
        if self.scheduler.should_flatten(now):
            success, msg = self.reconciler.execute_flatten_sequence()
            decision_type = Decision.EXIT
            if not success:
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

        if session_state == SessionState.PRE_OPEN:
            return [
                StrategyDecision(
                    decision=Decision.WAIT,
                    timestamp_ist=now,
                    reason_codes=["PRE_OPEN_SESSION"],
                    explanation="Pre-open session (before 09:15 IST). Observing and validating data.",
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

        # -------------------------------------------------------------
        # STEP 11 (Priority): Manage Open Positions
        # -------------------------------------------------------------
        decisions: list[StrategyDecision] = []
        open_managed = [p for p in self.state_machine.positions.values() if p.quantity > 0]

        for pos in open_managed:
            # Manage existing position stop or take profit
            try:
                quote = self.broker.get_quote(pos.symbol)
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
                        exit_req = OrderRequest(
                            symbol=pos.symbol,
                            side=exit_side,
                            quantity=pos.quantity,
                            product=Product.MIS,
                            order_type=OrderType.MARKET,
                            price=0.0,
                            tag=f"{pos.strategy_tag}_TARGET",
                            strategy_id=self.session_tag,
                        )
                        exit_order_id = self.broker.place_order(exit_req)
                        if pos.protective_order_id:
                            self.broker.cancel_order(pos.protective_order_id)

                        pos.phase = PositionPhase.EXITING
                        pos.exit_order_id = exit_order_id
                        pos.quantity = 0

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
                    candles_5m = self.broker.get_historical(pos.symbol, "5minute", None, None)
                    candles_1m = self.broker.get_historical(pos.symbol, "minute", None, None)

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
                    else:
                        pos.best_favorable_price = min(pos.best_favorable_price, current_price) if pos.best_favorable_price > 0 else current_price
                        fav_excursion = pos.average_entry - pos.best_favorable_price

                    open_r = fav_excursion / pos.r_value if pos.r_value > 0 else 0.0

                    # One-Time Fee-Protected Breakeven Step
                    st_cfg = {}
                    if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict):
                        st_cfg = self.config.raw_dict.get("stops_and_targets", {})
                    fee_lock_enabled = st_cfg.get("fee_lock_enabled", getattr(self.config.stops, "fee_lock_enabled", True))
                    trigger_r = float(st_cfg.get("fee_lock_trigger_r", getattr(self.config.stops, "fee_lock_trigger_r", 1.65)))
                    sched_cfg = self.config.raw_dict.get("session_schedule", self.config.raw_dict.get("schedule", {})) if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict) else {}
                    late_time = sched_cfg.get("late_day_protection_time", getattr(self.config.stops, "late_day_protection_time", "14:15"))
                    late_trigger_r = float(st_cfg.get("late_day_lock_trigger_r", getattr(self.config.stops, "late_day_lock_trigger_r", 1.15)))
                    offset_pct = float(st_cfg.get("fee_lock_offset_pct", getattr(self.config.stops, "fee_lock_offset_pct", 0.0015)))

                    now_time_str = now.strftime("%H:%M")
                    should_lock = fee_lock_enabled and (not getattr(pos, "fee_lock_applied", False)) and (
                        open_r >= trigger_r or (now_time_str >= late_time and open_r >= late_trigger_r)
                    )

                    broker_action = "NONE"
                    reason_code = "HOLDING_FOR_TARGET"
                    explanation = f"Holding with structural SL ({pos.current_stop_price:.2f}) towards 2.0R target ({pos.target_price:.2f})"

                    if should_lock:
                        tick = inst_info.tick_size if inst_info and inst_info.tick_size > 0 else 0.05
                        if pos.side == Side.BUY:
                            protected_stop = ceil_to_tick(pos.average_entry * (1.0 + offset_pct), tick)
                            valid_move = protected_stop > pos.current_stop_price and protected_stop <= current_price
                        else:
                            protected_stop = floor_to_tick(pos.average_entry * (1.0 - offset_pct), tick)
                            valid_move = protected_stop < pos.current_stop_price and protected_stop >= current_price

                        if valid_move:
                            prev_stop = pos.current_stop_price
                            trigger, limit = calculate_stop_limit_prices(pos.side, protected_stop, self.config.stops, tick)
                            if pos.protective_order_id:
                                self.broker.modify_order(
                                    order_id=pos.protective_order_id,
                                    price=limit,
                                    trigger_price=trigger,
                                )
                            pos.current_stop_price = protected_stop
                            pos.fee_lock_applied = True
                            pos.stop_modification_count += 1
                            broker_action = f"MODIFIED_STOP_FEE_LOCK_{protected_stop}"
                            reason_code = "FEE_PROTECTED_BREAKEVEN_STEP"
                            explanation = f"Moved stop to fee-protected breakeven ({protected_stop:.2f}) at {open_r:.2f}R"
                            self.journal.log_stop_change(
                                symbol=pos.symbol,
                                side=pos.side.value,
                                previous_stop=prev_stop,
                                new_stop=protected_stop,
                                reason="FEE_PROTECTED_BREAKEVEN_STEP",
                                r_multiple=round(open_r, 2),
                                best_favorable_price=pos.best_favorable_price,
                                modification_number=pos.stop_modification_count,
                            )

                    decisions.append(
                        StrategyDecision(
                            decision=Decision.MANAGE,
                            symbol=pos.symbol,
                            side=pos.side,
                            timestamp_ist=now,
                            reason_codes=[reason_code],
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
            self.journal.log_warning(
                severity="CRITICAL",
                code="DAILY_LOSS_LIMIT_BREACHED",
                message=daily_risk_status.gate.reason,
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
                    explanation=daily_risk_status.gate.reason,
                    gates=[daily_risk_status.gate],
                )
            ]

        # If already at max simultaneous positions, cannot take new entries
        active_managed_count = sum(1 for position in open_managed if position.quantity > 0)
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

        # Slot-2 Lock Gate: Only allow a 2nd position if Slot 1 is already in profit >= +0.50R
        active_positions = [p for p in open_managed if p.quantity > 0]
        if len(active_positions) == 1:
            first_pos = active_positions[0]
            mark = self.broker.get_quote(first_pos.symbol)
            curr_px = mark.last_price if (mark and mark.is_fresh and mark.last_price > 0) else first_pos.average_entry
            r_val = first_pos.r_value if first_pos.r_value > 0 else abs(first_pos.average_entry - first_pos.initial_stop_price)
            unrealized_r = 0.0
            if r_val > 0:
                unrealized_r = (curr_px - first_pos.average_entry) / r_val if first_pos.side == Side.BUY else (first_pos.average_entry - curr_px) / r_val

            risk_raw = self.config.raw_dict.get("risk_management", {}) if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict) else {}
            req_r = float(risk_raw.get("second_slot_requires_slot1_profit_r", 0.50))
            if unrealized_r < req_r:
                decisions.append(
                    StrategyDecision(
                        decision=Decision.WAIT,
                        timestamp_ist=now,
                        reason_codes=["SECOND_SLOT_LOCKED_UNTIL_0.5R"],
                        explanation=f"Slot 2 locked: Active position in {first_pos.symbol} has unrealized R of {unrealized_r:.2f}R (requires >= {req_r:.2f}R)",
                    )
                )
                return decisions

        # -------------------------------------------------------------
        # STEP 6: Opening Range Period Check (09:15 - 09:30)
        # -------------------------------------------------------------
        if self.scheduler.is_opening_range_period(now):
            # In opening range build phase - update ranges
            for sym in symbols:
                candles_0915_0930 = self.broker.get_historical(sym, "5minute", None, None)
                self.opening_ranges[sym] = build_opening_range(sym, candles_0915_0930)

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
        bench_candles = self.broker.get_historical(self.config.regime.benchmark_symbol, "5minute", None, None)
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

        for sym in symbols:
            # Skip if already open
            if any(p.symbol == sym and p.quantity > 0 for p in open_managed):
                continue

            inst = self.broker.get_instrument_info(sym)
            quote = self.broker.get_quote(sym)
            stock_candles = self.broker.get_historical(sym, "5minute", None, None)

            # Build/retrieve opening range
            or_range = self.opening_ranges.get(sym)
            if or_range is None or not or_range.is_complete:
                # Build from history
                or_range = build_opening_range(sym, stock_candles[:3] if len(stock_candles) >= 3 else [])
                self.opening_ranges[sym] = or_range

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
            return (abs(sig_item.relative_strength) * 100.0 * sig_item.relative_volume) / spread_floor

        qualified_candidates.sort(key=compute_conviction, reverse=True)
        max_new = 1
        if hasattr(self.config, "raw_dict") and isinstance(self.config.raw_dict, dict):
            max_new = int(self.config.raw_dict.get("risk_management", {}).get("max_new_entries_per_bar", 1))
        qualified_candidates = qualified_candidates[:max_new]

        # Sizing and Risk for qualified candidates
        num_qualified = len(qualified_candidates)

        for sym in qualified_candidates:
            if len(risk_positions) >= self.config.limits.max_symbols:
                break
            sig, inst, stock_candles, _ = evaluated_signals[sym]
            entry_price = sig.signal_price
            side = sig.side

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
            tag = f"{self.session_tag}_{sym}_{int(now.timestamp())}"
            broker_action = "PLACE_ENTRY_AND_PROTECTION"

            # Execute entry order if enabled (Paper or Live)
            entry_req = OrderRequest(
                symbol=sym,
                side=side,
                quantity=sizing.quantity,
                product=Product.MIS,
                order_type=OrderType.LIMIT,
                price=entry_price,
                tag=tag,
                strategy_id=self.session_tag,
            )

            try:
                entry_order_id = self.broker.place_order(entry_req)
                managed = self.state_machine.get_or_create_position(sym)
                managed.phase = PositionPhase.ENTERING
                managed.side = side
                managed.quantity = sizing.quantity
                managed.average_entry = entry_price
                managed.initial_stop_price = initial_stop
                managed.current_stop_price = initial_stop
                managed.best_favorable_price = entry_price
                managed.r_value = abs(entry_price - initial_stop)
                managed.target_price = calculate_target_price(
                    side=side,
                    entry_price=entry_price,
                    initial_stop=initial_stop,
                    target_r=self.config.stops.target_r,
                    tick_size=inst.tick_size,
                )
                managed.entry_order_id = entry_order_id
                managed.strategy_tag = tag

                # Reserve the accepted order's risk for any other candidate
                # considered in this same strategy cycle.
                risk_positions.append(
                    PositionState(
                        symbol=sym,
                        side=side,
                        quantity=sizing.quantity,
                        average_entry=entry_price,
                        current_price=entry_price,
                        protective_stop_price=initial_stop,
                        initial_stop_price=initial_stop,
                        is_protected=True,
                        strategy_tag=tag,
                    )
                )

                # Step 10: Place protective stop immediately for filled quantity
                exit_side = Side.SELL if side == Side.BUY else Side.BUY
                trigger, limit = calculate_stop_limit_prices(side, initial_stop, self.config.stops, inst.tick_size)

                sl_req = OrderRequest(
                    symbol=sym,
                    side=exit_side,
                    quantity=sizing.quantity,
                    product=Product.MIS,
                    order_type=OrderType.SL,
                    price=limit,
                    trigger_price=trigger,
                    tag=f"{tag}_SL",
                    strategy_id=self.session_tag,
                )
                sl_order_id = self.broker.place_order(sl_req)
                managed.protective_order_id = sl_order_id
                managed.phase = PositionPhase.OPEN_PROTECTED

                self.journal.log_event(
                    event_type="ENTRY_AND_PROTECTION_PLACED",
                    symbol=sym,
                    side=side.value,
                    data={
                        "entry_order_id": entry_order_id,
                        "sl_order_id": sl_order_id,
                        "quantity": sizing.quantity,
                        "entry_price": entry_price,
                        "initial_stop": initial_stop,
                        "target_price": managed.target_price,
                    },
                    explanation=f"Placed entry, target ({managed.target_price:.2f}), and protective stop for {sym} ({sizing.quantity} shares)",
                )

                decisions.append(
                    StrategyDecision(
                        decision=sig.decision,
                        symbol=sym,
                        side=side,
                        timestamp_ist=now,
                        reason_codes=["ENTRY_SUBMITTED"],
                        explanation=f"Submitted {sig.decision.value} for {sizing.quantity} shares of {sym}",
                        gates=sig.gates + [sizing.gate, risk_status.gate],
                        quantity=sizing.quantity,
                        entry_limit=entry_price,
                        initial_stop=initial_stop,
                        risk_rupees=sizing.trade_risk_rupees,
                        estimated_margin=sizing.notional_value / sizing.effective_leverage,
                        max_notional=sizing.per_symbol_budget,
                        broker_action=broker_action,
                    )
                )
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

    # Slot 2 Gate: Only allow a 2nd concurrent position if Slot 1 is already >= +0.50R in profit
    if len(open_positions) == 1:
        first_pos = next(iter(open_positions.values()))
        req_r = float(risk_cfg.get("second_slot_requires_slot1_profit_r", 0.50))
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
