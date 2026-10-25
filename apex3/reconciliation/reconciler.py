"""APEX-3 Position and Order Reconciler.

Reconciliation Responsibilities:
1. Startup & Reconnect Check:
   - Compare broker net positions vs internal state.
   - Detect unknown/manual positions: If any non-strategy MIS position exists,
     block new entries and set system to PAUSED_UNKNOWN_POSITION.
2. Protective Stop Quantity Verification:
   - Protective stop quantity MUST EXACTLY EQUAL open filled quantity.
   - After a partial fill or modification, verify stop matches position.
3. End-of-Day MIS Flattening Sequence:
   - At 14:55 IST, cancel pending strategy entry orders.
   - Re-fetch broker positions and trades.
   - Cancel existing protective SL orders before submitting market exit.
   - Exit exact residual open MIS quantity.
   - Verify flat by 14:58 IST; raise critical emergency alert if not flat by 15:00 IST.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from apex3.broker.interface import BrokerInterface
from apex3.journal.sqlite_journal import Journal
from apex3.models import (
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionState,
    Product,
    Side,
    make_broker_tag,
)
from apex3.reconciliation.state_machine import PositionPhase, StateMachine

logger = logging.getLogger("apex3.reconciler")


@dataclass
class ReconciliationStatus:
    """Status of reconciliation cycle."""
    is_clean: bool
    has_unknown_positions: bool
    unknown_symbols: list[str]
    unprotected_positions: list[str]
    is_flat: bool
    message: str


class Reconciler:
    """Manages order reconciliation and safe exit sequences."""

    def __init__(
        self,
        broker: BrokerInterface,
        state_machine: StateMachine,
        journal: Journal,
        session_tag: str = "APEX3",
    ):
        self.broker = broker
        self.sm = state_machine
        self.journal = journal
        self.session_tag = session_tag

    def audit_startup_state(self, watched_symbols: list[str]) -> ReconciliationStatus:
        """Run on startup and reconnect to verify position state against broker.

        If any unknown position or external manual MIS trade is open,
        entries are blocked to prevent collision with user manual activity.
        """
        try:
            broker_positions = self.broker.get_positions()
        except Exception as e:
            self.journal.log_warning(
                severity="CRITICAL",
                code="BROKER_POSITION_FETCH_FAILED",
                message=f"Could not fetch broker positions: {e}",
            )
            return ReconciliationStatus(
                is_clean=False,
                has_unknown_positions=True,
                unknown_symbols=[],
                unprotected_positions=[],
                is_flat=False,
                message=f"Failed to fetch broker positions: {e}",
            )

        unknown_symbols: list[str] = []
        open_strategy_count = 0

        # Query broker orders to support restart recovery of strategy positions
        try:
            broker_orders = self.broker.get_orders()
        except Exception:
            broker_orders = []

        for bp in broker_positions:
            if bp.quantity == 0:
                # Clear corresponding internal managed position if present
                managed = self.sm.positions.get(bp.symbol)
                if managed and managed.quantity > 0:
                    managed.quantity = 0
                    managed.phase = PositionPhase.CLOSED
                continue

            # Check if this position is recognized by internal state
            pos = self.sm.positions.get(bp.symbol)
            if pos is None or pos.phase in (PositionPhase.FLAT, PositionPhase.CLOSED):
                # Attempt restart recovery: check if broker orders match this strategy session
                matching_orders = [
                    o for o in broker_orders
                    if getattr(o, "request", None) and o.request.symbol == bp.symbol
                    and self.sm.is_strategy_order(o.request.tag)
                ]
                if matching_orders:
                    # Restore strategy position
                    pos = self.sm.get_or_create_position(bp.symbol)
                    pos.symbol = bp.symbol
                    pos.quantity = bp.quantity
                    pos.side = getattr(bp, "side", Side.BUY)
                    pos.average_entry = bp.average_entry
                    pos.current_stop_price = bp.protective_stop_price or 0.0

                    # Reconnect resting protective stop order if present
                    matching_stops = [
                        o for o in matching_orders
                        if o.request.order_type == OrderType.SL
                        and o.status == OrderStatus.TRIGGER_PENDING
                    ]
                    if matching_stops:
                        pos.protective_order_id = matching_stops[0].order_id
                        pos.current_stop_price = matching_stops[0].request.trigger_price or matching_stops[0].request.price
                        pos.phase = PositionPhase.OPEN_PROTECTED
                        logger.info("Restored active strategy position %s (qty=%d) with stop %s on restart", bp.symbol, bp.quantity, pos.protective_order_id)
                    else:
                        pos.phase = PositionPhase.OPEN_UNPROTECTED
                        logger.warning("Restored strategy position %s (qty=%d) on restart, but resting SL order was not found", bp.symbol, bp.quantity)

                    # Fix 7: Restore initial_stop_price, r_value, and target_price on restart recovery
                    if pos.initial_stop_price <= 0 and pos.current_stop_price > 0:
                        pos.initial_stop_price = pos.current_stop_price
                    if pos.initial_stop_price > 0 and pos.average_entry > 0:
                        pos.r_value = abs(pos.average_entry - pos.initial_stop_price)
                    if pos.r_value > 0 and pos.target_price <= 0:
                        from apex3.strategy.trail import calculate_target_price
                        pos.target_price = calculate_target_price(
                            side=pos.side,
                            entry_price=pos.average_entry,
                            initial_stop=pos.initial_stop_price,
                            target_r=2.0,
                            tick_size=0.05,
                        )

                    open_strategy_count += 1
                else:
                    # Foreign / manual position
                    unknown_symbols.append(bp.symbol)
            else:
                if pos.quantity != bp.quantity:
                    pos.quantity = bp.quantity
                open_strategy_count += 1

        if unknown_symbols:
            msg = f"Unknown manual positions detected on broker: {unknown_symbols}. Strategy paused."
            self.journal.log_warning(
                severity="CRITICAL",
                code="UNKNOWN_MANUAL_POSITIONS",
                message=msg,
                data={"unknown_symbols": unknown_symbols},
            )
            return ReconciliationStatus(
                is_clean=False,
                has_unknown_positions=True,
                unknown_symbols=unknown_symbols,
                unprotected_positions=[],
                is_flat=False,
                message=msg,
            )

        is_flat = (open_strategy_count == 0)
        return ReconciliationStatus(
            is_clean=True,
            has_unknown_positions=False,
            unknown_symbols=[],
            unprotected_positions=[],
            is_flat=is_flat,
            message="Reconciliation clean. No foreign positions detected.",
        )

    def verify_protective_stops(self) -> list[str]:
        """Verify that every open strategy position has an active broker-confirmed protective stop."""
        unprotected: list[str] = []
        try:
            orders = {o.order_id: o for o in self.broker.get_orders()}
        except Exception as e:
            logger.error("Could not fetch orders to verify protective stops: %s", e)
            orders = {}

        for symbol, pos in self.sm.positions.items():
            if pos.quantity > 0:
                is_covered = False
                if pos.protective_order_id and pos.protective_order_id in orders:
                    broker_sl = orders[pos.protective_order_id]
                    expected_exit_side = Side.SELL if pos.side == Side.BUY else Side.BUY
                    # Protective stop must be TRIGGER_PENDING, correctly sized, and correct exit side
                    if (
                        broker_sl.status == OrderStatus.TRIGGER_PENDING
                        and broker_sl.request.quantity == pos.quantity
                        and broker_sl.request.side == expected_exit_side
                    ):
                        is_covered = True
                        pos.phase = PositionPhase.OPEN_PROTECTED

                if not is_covered:
                    unprotected.append(symbol)
                    self.journal.log_warning(
                        severity="EMERGENCY",
                        code="UNPROTECTED_POSITION",
                        message=f"Position in {symbol} (qty={pos.quantity}) lacks verified broker-confirmed protective stop!",
                        data={"symbol": symbol, "quantity": pos.quantity, "protective_order_id": pos.protective_order_id},
                    )
        return unprotected

    def execute_flatten_sequence(self, timeout_sec: float = 10.0) -> tuple[bool, str]:
        """Execute the controlled MIS square-off sequence and poll until account is confirmed flat.

        1. Cancel strategy pending entries and stop orders
        2. Submit market exit orders for all open strategy exposure
        3. Poll order statuses and broker positions until zero exposure or timeout
        4. Report success ONLY after confirming flat at broker
        """
        import time as time_module

        logger.info("Starting controlled MIS square-off sequence...")
        self.journal.log_event(
            event_type="FLATTEN_START",
            explanation="Beginning controlled MIS square-off sequence",
        )

        # 1. Cancel pending strategy orders and stops
        try:
            orders = self.broker.get_orders()
            for o in orders:
                if o.status in (OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING):
                    if self.sm.is_strategy_order(o.request.tag):
                        try:
                            self.broker.cancel_order(o.order_id)
                            self.journal.log_event(
                                event_type="CANCEL_PENDING_ORDER",
                                symbol=o.request.symbol,
                                data={"order_id": o.order_id},
                                explanation=f"Cancelled pending strategy order {o.order_id}",
                            )
                        except Exception as ce:
                            logger.warning("Could not cancel order %s during flatten: %s", o.order_id, ce)
        except Exception as e:
            logger.error("Error cancelling pending orders during flatten: %s", e)

        # 2. Flatten each active strategy position
        residual_errors: list[str] = []
        try:
            broker_positions = self.broker.get_positions()
        except Exception as e:
            return False, f"Could not retrieve positions during flatten: {e}"

        exit_orders: dict[str, str] = {}
        for bp in broker_positions:
            if bp.quantity == 0:
                continue

            managed = self.sm.positions.get(bp.symbol)
            if managed is None:
                continue

            managed.quantity = bp.quantity

            if managed.protective_order_id:
                try:
                    self.broker.cancel_order(managed.protective_order_id)
                except Exception:
                    pass

            side = getattr(bp, "side", None) or managed.side
            exit_side = Side.SELL if side == Side.BUY else Side.BUY
            exit_qty = bp.quantity

            exit_req = OrderRequest(
                symbol=managed.symbol,
                side=exit_side,
                quantity=exit_qty,
                product=Product.MIS,
                order_type=OrderType.MARKET,
                tag=make_broker_tag(self.session_tag, managed.symbol, "FLT"),
                strategy_id=self.session_tag,
            )

            try:
                exit_order_id = self.broker.place_order(exit_req)
                managed.exit_order_id = exit_order_id
                managed.phase = PositionPhase.EXITING
                exit_orders[managed.symbol] = exit_order_id

                self.journal.log_event(
                    event_type="EXIT_ORDER_PLACED",
                    symbol=managed.symbol,
                    side=exit_side.value,
                    data={"quantity": exit_qty, "exit_order_id": exit_order_id},
                    explanation=f"Submitted market exit for {managed.symbol} (qty={exit_qty})",
                )
            except Exception as e:
                err_msg = f"Failed to submit exit for {managed.symbol}: {e}"
                residual_errors.append(err_msg)
                self.journal.log_warning(
                    severity="EMERGENCY",
                    code="EXIT_ORDER_FAILED",
                    message=err_msg,
                    data={"symbol": managed.symbol, "quantity": exit_qty},
                )

        if residual_errors:
            return False, "; ".join(residual_errors)

        # 3. Verification Poll Loop: poll until confirmed flat or timeout
        start_t = time_module.time()
        poll_interval = 0.5
        all_flat = False
        remaining_symbols: list[str] = []

        while time_module.time() - start_t < timeout_sec:
            try:
                fresh_bp = self.broker.get_positions()
                open_bp = [p for p in fresh_bp if p.quantity > 0 and self.sm.positions.get(p.symbol) is not None]
                if not open_bp:
                    all_flat = True
                    break
                remaining_symbols = [p.symbol for p in open_bp]
            except Exception as pe:
                logger.warning("Error polling positions during flatten verification: %s", pe)
            time_module.sleep(poll_interval)

        if not all_flat:
            try:
                fresh_bp = self.broker.get_positions()
                open_bp = [p for p in fresh_bp if p.quantity > 0 and self.sm.positions.get(p.symbol) is not None]
                all_flat = (len(open_bp) == 0)
                remaining_symbols = [p.symbol for p in open_bp]
            except Exception:
                pass

        if all_flat:
            for p in self.sm.positions.values():
                p.quantity = 0
                p.phase = PositionPhase.CLOSED
            logger.info("Controlled MIS square-off confirmed flat at broker.")
            self.journal.log_event(event_type="FLATTEN_CONFIRMED", explanation="Account confirmed flat at broker")
            return True, "Account confirmed flat. All strategy positions closed and verified at broker."
        else:
            msg = f"Flatten verification timed out after {timeout_sec:.1f}s. Residual open exposure: {remaining_symbols}"
            logger.critical("FLATTEN INCOMPLETE: %s", msg)
            self.journal.log_warning(
                severity="EMERGENCY",
                code="FLATTEN_INCOMPLETE",
                message=msg,
                data={"remaining": remaining_symbols},
            )
            return False, msg
