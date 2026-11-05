"""Unit tests for dynamic order variety and live circuit limit validation."""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from apex3.broker.kite_adapter import KiteAdapter, is_nse_market_hours
from apex3.models import OrderRequest, OrderType, Product, Side

IST = ZoneInfo("Asia/Kolkata")


def test_is_nse_market_hours():
    # Weekday during market hours: 10:30 IST on a Wednesday (2026-11-04)
    wed_market = datetime(2026, 11, 4, 10, 30, tzinfo=IST)
    assert is_nse_market_hours(wed_market) is True

    # Weekday before market hours: 09:00 IST
    wed_pre = datetime(2026, 11, 4, 9, 0, tzinfo=IST)
    assert is_nse_market_hours(wed_pre) is False

    # Weekday after market hours: 16:00 IST
    wed_post = datetime(2026, 11, 4, 16, 0, tzinfo=IST)
    assert is_nse_market_hours(wed_post) is False

    # Weekend: Saturday 11:00 IST
    sat_noon = datetime(2026, 11, 7, 11, 0, tzinfo=IST)
    assert is_nse_market_hours(sat_noon) is False


def test_kite_adapter_dynamic_variety_selection():
    adapter = KiteAdapter(api_key="test_key", access_token="test_tok", live_enabled=True)
    
    # 1. During market hours -> regular
    with patch("apex3.broker.kite_adapter.is_nse_market_hours", return_value=True):
        assert adapter.get_dynamic_variety() == "regular"
        assert adapter.get_dynamic_variety(None) == "regular"
        assert adapter.get_dynamic_variety("amo") == "amo"  # explicit override

    # 2. Outside market hours -> amo
    with patch("apex3.broker.kite_adapter.is_nse_market_hours", return_value=False):
        assert adapter.get_dynamic_variety() == "amo"
        assert adapter.get_dynamic_variety(None) == "amo"
        assert adapter.get_dynamic_variety("regular") == "regular"  # explicit override


def test_kite_adapter_order_placement_retry_on_variety_mismatch():
    adapter = KiteAdapter(api_key="test_key", access_token="test_tok", live_enabled=True)
    mock_kite = MagicMock()
    adapter._kite = mock_kite

    # Simulate Kite throwing error when regular variety sent outside hours
    first_call = True
    def mock_place(**kwargs):
        nonlocal first_call
        if kwargs.get("variety") == "regular" and first_call:
            first_call = False
            raise Exception("Your order could not be converted to a After Market Order (AMO).")
        return "261106000001"

    mock_kite.place_order.side_effect = mock_place

    with patch("apex3.broker.kite_adapter.is_nse_market_hours", return_value=True):
        req = OrderRequest(
            symbol="IDEA",
            side=Side.BUY,
            quantity=1,
            product=Product.CNC,
            order_type=OrderType.LIMIT,
            price=12.55,
        )
        order_id = adapter.place_order(req)
        assert order_id == "261106000001"
        assert mock_kite.place_order.call_count == 2
        # Verify second call used fallback 'amo'
        second_kwargs = mock_kite.place_order.call_args_list[1][1]
        assert second_kwargs["variety"] == "amo"
        assert adapter._order_variety_cache["261106000001"] == "amo"
