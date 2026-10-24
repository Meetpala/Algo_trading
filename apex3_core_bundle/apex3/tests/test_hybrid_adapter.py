"""Tests for AngelDataProvider and HybridBrokerAdapter."""
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from apex3.broker.angel_data_provider import AngelDataProvider
from apex3.broker.hybrid_adapter import HybridBrokerAdapter
from apex3.broker.interface import BrokerInterface
from apex3.models import (
    Candle,
    InstrumentInfo,
    MarginInfo,
    OrderMarginEstimate,
    OrderRequest,
    OrderState,
    OrderStatus,
    OrderType,
    PositionState,
    Product,
    Quote,
    Side,
)

IST = ZoneInfo("Asia/Kolkata")


class MockExecutionBroker(BrokerInterface):
    """Mock execution broker standing in for Zerodha."""

    def __init__(self):
        self.placed_orders = []
        self.cancelled_orders = []
        self.modified_orders = []

    def get_margins(self) -> MarginInfo:
        return MarginInfo(equity_available=15000.0, equity_used=0.0, timestamp=datetime.now(IST))

    def estimate_order_margin(self, request: OrderRequest) -> OrderMarginEstimate:
        return OrderMarginEstimate(required_margin=3000.0, charges=20.0, total=3020.0, leverage_available=4.0)

    def place_order(self, request: OrderRequest) -> str:
        self.placed_orders.append(request)
        return "ZERODHA_ORDER_12345"

    def modify_order(self, order_id: str, **kwargs) -> bool:
        self.modified_orders.append((order_id, kwargs))
        return True

    def cancel_order(self, order_id: str) -> bool:
        self.cancelled_orders.append(order_id)
        return True

    def get_order_status(self, order_id: str) -> OrderState:
        return OrderState(
            order_id=order_id,
            request=OrderRequest("RELIANCE", "NSE", Side.BUY, 10, Product.MIS, OrderType.LIMIT, 2500.0),
            status=OrderStatus.FILLED,
            filled_quantity=10,
            pending_quantity=0,
            average_price=2500.0,
            last_updated=datetime.now(IST),
        )

    def get_orders(self) -> list[OrderState]:
        return []

    def get_trades(self) -> list[dict]:
        return []

    def get_positions(self) -> list[PositionState]:
        return []

    def is_session_valid(self) -> bool:
        return True

    def get_quote(self, symbol: str, exchange: str = "NSE") -> Quote:
        raise NotImplementedError("Execution broker should not provide market quotes in hybrid mode")

    def get_historical(self, symbol: str, interval: str, from_dt: datetime, to_dt: datetime, exchange: str = "NSE") -> list[Candle]:
        raise NotImplementedError("Execution broker should not provide historical data in hybrid mode")

    def get_instrument_info(self, symbol: str, exchange: str = "NSE") -> InstrumentInfo:
        raise NotImplementedError("Execution broker should not provide instrument info in hybrid mode")


def test_hybrid_adapter_routing():
    """Verify that market data calls go to Angel One and order calls go strictly to Zerodha."""
    angel_provider = AngelDataProvider(api_key="", client_code="", pin="", totp_secret="")
    zerodha_broker = MockExecutionBroker()

    hybrid = HybridBrokerAdapter(data_provider=angel_provider, execution_broker=zerodha_broker)

    # 1. Market Data should route to AngelDataProvider
    quote = hybrid.get_quote("RELIANCE")
    assert quote.symbol == "RELIANCE"
    assert quote.last_price > 0

    inst = hybrid.get_instrument_info("RELIANCE")
    assert inst.symbol == "RELIANCE"
    assert inst.upper_circuit > inst.lower_circuit

    # 2. Margins should route to Zerodha
    margin = hybrid.get_margins()
    assert margin.equity_available == 15000.0

    # 3. Order placement must route EXCLUSIVELY to Zerodha
    req = OrderRequest(
        symbol="RELIANCE",
        exchange="NSE",
        side=Side.BUY,
        quantity=5,
        product=Product.MIS,
        order_type=OrderType.LIMIT,
        price=2500.0,
    )
    order_id = hybrid.place_order(req)
    assert order_id == "ZERODHA_ORDER_12345"
    assert len(zerodha_broker.placed_orders) == 1
    assert zerodha_broker.placed_orders[0].symbol == "RELIANCE"

    # 4. Order cancellation must route EXCLUSIVELY to Zerodha
    res = hybrid.cancel_order("ZERODHA_ORDER_12345")
    assert res is True
    assert "ZERODHA_ORDER_12345" in zerodha_broker.cancelled_orders
