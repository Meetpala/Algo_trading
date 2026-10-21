"""Hybrid Broker Adapter for APEX-3.

SPLIT-FEED ARCHITECTURE:
- Market Data Layer: Angel One SmartAPI (100% Free Forever live quotes, candles, instruments).
- Execution Layer: Zerodha Kite Connect Personal Free (100% Free Forever orders, margins, positions).

Strict Security & Separation:
All order placements, modifications, cancellations, and margin checks are routed EXCLUSIVELY
to Zerodha. Angel One is used solely as a read-only market data stream.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from apex3.broker.angel_data_provider import AngelDataProvider
from apex3.broker.interface import BrokerInterface
from apex3.models import (
    Candle,
    InstrumentInfo,
    MarginInfo,
    OrderMarginEstimate,
    OrderRequest,
    OrderState,
    PositionState,
    Quote,
)

logger = logging.getLogger(__name__)


class HybridBrokerAdapter(BrokerInterface):
    """Hybrid broker adapter combining Angel One data feed with Zerodha order execution."""

    def __init__(
        self,
        data_provider: AngelDataProvider,
        execution_broker: BrokerInterface,
    ) -> None:
        self.data_provider = data_provider
        self.execution_broker = execution_broker
        logger.info(
            "Initialized APEX-3 HYBRID BROKER ADAPTER: [Data: Angel One SmartAPI (Free)] ➔ [Execution: Zerodha Personal (Free)]"
        )

    # ── Market Data (Delegated to Angel One SmartAPI) ────────────────

    def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        """Fetch live quote from Angel One free data feed."""
        return self.data_provider.get_quote(symbol=symbol, exchange=exchange)

    def get_historical(
        self,
        symbol: str,
        interval: str,
        from_dt: datetime,
        to_dt: datetime,
        exchange: str = "NSE",
    ) -> list[Candle]:
        """Fetch historical candles from Angel One free data feed."""
        return self.data_provider.get_historical(
            symbol=symbol,
            interval=interval,
            from_dt=from_dt,
            to_dt=to_dt,
            exchange=exchange,
        )

    def get_instrument_info(self, symbol: str, exchange: str = "NSE") -> InstrumentInfo:
        """Fetch instrument info from Angel One data feed."""
        return self.data_provider.get_instrument_info(symbol=symbol, exchange=exchange)

    # ── Order Execution & Portfolio (Delegated to Zerodha Personal) ──

    def get_margins(self) -> MarginInfo:
        """Fetch margin from Zerodha."""
        return self.execution_broker.get_margins()

    def estimate_order_margin(self, request: OrderRequest) -> OrderMarginEstimate:
        """Estimate order margin from Zerodha."""
        return self.execution_broker.estimate_order_margin(request)

    def place_order(self, request: OrderRequest) -> str:
        """Submit live order EXCLUSIVELY to Zerodha."""
        logger.info("HYBRID ROUTE: Routing order for %s to ZERODHA", request.symbol)
        return self.execution_broker.place_order(request)

    def modify_order(
        self,
        order_id: str,
        quantity: Optional[int] = None,
        price: Optional[float] = None,
        trigger_price: Optional[float] = None,
        order_type: Optional[str] = None,
    ) -> bool:
        """Modify order EXCLUSIVELY on Zerodha."""
        return self.execution_broker.modify_order(
            order_id=order_id,
            quantity=quantity,
            price=price,
            trigger_price=trigger_price,
            order_type=order_type,
        )

    def cancel_order(self, order_id: str) -> bool:
        """Cancel order EXCLUSIVELY on Zerodha."""
        return self.execution_broker.cancel_order(order_id)

    def get_order_status(self, order_id: str) -> OrderState:
        """Fetch order status from Zerodha."""
        return self.execution_broker.get_order_status(order_id)

    def get_orders(self) -> list[OrderState]:
        """Fetch orders from Zerodha."""
        return self.execution_broker.get_orders()

    def get_trades(self) -> list[dict]:
        """Fetch trades from Zerodha."""
        return self.execution_broker.get_trades()

    def get_positions(self) -> list[PositionState]:
        """Fetch net positions from Zerodha."""
        return self.execution_broker.get_positions()

    def is_session_valid(self) -> bool:
        """Check session validity on the execution broker (Zerodha)."""
        return self.execution_broker.is_session_valid()
