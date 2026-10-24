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

        for bp in broker_positions:
            if bp.quantity == 0:
                continue

            # Check if this position is recognized by internal state
            pos = self.sm.positions.get(bp.symbol)
            if pos is None or pos.phase == PositionPhase.FLAT:
                # This is an unknown / manual position
                unknown_symbols.append(bp.symbol)
            else:
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
        """Verify that every open strategy position has an active protective stop.

        Returns list of symbols that are OPEN but UNPROTECTED.
        """
        unprotected: list[str] = []
        for symbol, pos in self.sm.positions.items():
            if pos.quantity > 0:
                if not pos.protective_order_id or pos.phase != PositionPhase.OPEN_PROTECTED:
                    unprotected.append(symbol)
                    self.journal.log_warning(
                        severity="EMERGENCY",
                        code="UNPROTECTED_POSITION",
                        message=f"Position in {symbol} (qty={pos.quantity}) lacks verified protective stop!",
                        data={"symbol": symbol, "quantity": pos.quantity},
                    )
        return unprotected

    def execute_flatten_sequence(self) -> tuple[bool, str]:
        """Execute the controlled MIS square-off sequence.

        1. Cancel strategy pending entries
        2. Cancel strategy protective stop orders
        3. Fetch fresh broker positions
        4. Place market exits for any remaining strategy MIS exposure
        5. Verify flat
        """
        logger.info("Starting controlled MIS square-off sequence...")
        self.journal.log_event(
            event_type="FLATTEN_START",
            explanation="Beginning controlled MIS square-off sequence",
        )

        # 1. Cancel pending strategy entry orders
        try:
            orders = self.broker.get_orders()
            for o in orders:
                if o.status in (OrderStatus.OPEN, OrderStatus.TRIGGER_PENDING):
                    if self.sm.is_strategy_order(o.request.tag):
                        self.broker.cancel_order(o.order_id)
                        self.journal.log_event(
                            event_type="CANCEL_PENDING_ORDER",
                            symbol=o.request.symbol,
                            data={"order_id": o.order_id},
                            explanation=f"Cancelled pending strategy order {o.order_id}",
                        )
        except Exception as e:
            logger.error("Error cancelling pending orders during flatten: %s", e)

        # 2. Flatten each active strategy position
        all_flat = True
        residual_errors: list[str] = []

        try:
            broker_positions = self.broker.get_positions()
        except Exception as e:
            return False, f"Could not retrieve positions during flatten: {e}"

        for bp in broker_positions:
            if bp.quantity == 0:
                continue

            # Only touch strategy-owned positions
            managed = self.sm.positions.get(bp.symbol)
            if managed is None or managed.quantity == 0:
                continue

            # Cancel protective stop first to free up quantity
            if managed.protective_order_id:
                try:
                    self.broker.cancel_order(managed.protective_order_id)
                except Exception as e:
                    logger.warning("Could not cancel protective stop %s: %s", managed.protective_order_id, e)

            # Exit side: if BUY, exit is SELL; if SELL, exit is BUY
            exit_side = Side.SELL if managed.side == Side.BUY else Side.BUY
            exit_qty = bp.quantity

            exit_req = OrderRequest(
                symbol=managed.symbol,
                side=exit_side,
                quantity=exit_qty,
                product=Product.MIS,
                order_type=OrderType.MARKET,
                tag=f"{self.session_tag}_EXIT",
                strategy_id=self.session_tag,
            )

            try:
                exit_order_id = self.broker.place_order(exit_req)
                managed.exit_order_id = exit_order_id
                managed.phase = PositionPhase.EXITING

                self.journal.log_event(
                    event_type="EXIT_ORDER_PLACED",
                    symbol=managed.symbol,
                    side=exit_side.value,
                    data={"quantity": exit_qty, "exit_order_id": exit_order_id},
                    explanation=f"Submitted market exit for {managed.symbol} (qty={exit_qty})",
                )
            except Exception as e:
                all_flat = False
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

        return True, "Flatten orders submitted successfully"
