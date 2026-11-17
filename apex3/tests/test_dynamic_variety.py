"""Unit tests for dynamic order variety, live circuit limit validation, 20-stock radar tokens, and late-start cloud activation."""
import pytest
from unittest.mock import MagicMock, patch
from datetime import datetime, time as dtime
from zoneinfo import ZoneInfo

from apex3.broker.angel_data_provider import DEFAULT_ANGEL_TOKENS
from apex3.broker.kite_adapter import KiteAdapter, is_nse_market_hours
from apex3.config import load_config
from apex3.journal.sqlite_journal import Journal
from apex3.models import OrderRequest, OrderType, Product, Side
from apex3.strategy.engine import StrategyEngine, build_active_radar

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


def test_all_20_radar_stocks_have_angel_tokens():
    cfg = load_config()
    radar = build_active_radar(cfg.raw_dict)
    assert len(radar) == 20
    for sym in radar:
        tok = DEFAULT_ANGEL_TOKENS.get(sym, "")
        assert tok != "", f"Missing Angel One NSE token for radar stock: {sym}"


def test_late_start_cloud_login_at_0950_does_not_lock_startup_backfill_early(tmp_path):
    """When Cloud VM runs at 09:20 while token is expired or paused, _startup_backfill_date must not lock until active at 09:50."""
    cfg = load_config()
    mock_broker = MagicMock()
    mock_broker.is_session_valid.return_value = False
    journal = Journal(db_path=str(tmp_path / "test_late_0950.db"))
    engine = StrategyEngine(config=cfg, broker=mock_broker, journal=journal)

    # 1. At 09:20 IST, broker session is invalid -> _startup_backfill_date must NOT be locked
    t_0920 = datetime(2026, 11, 18, 9, 20, tzinfo=IST)
    with patch.object(engine, "reconcile_and_backfill_late_start") as mock_backfill:
        decs = engine.evaluate_step(t_0920)
        assert any("BROKER_SESSION_INVALID" in d.reason_codes for d in decs)
        assert engine._startup_backfill_date is None

    # 2. At 09:50 IST, operator logs in & unpauses -> _startup_backfill_date locks and backfill runs!
    mock_broker.is_session_valid.return_value = True
    engine.is_paused = False
    t_0950 = datetime(2026, 11, 18, 9, 50, tzinfo=IST)
    with patch.object(engine, "reconcile_and_backfill_late_start") as mock_backfill:
        engine.evaluate_step(t_0950)
        assert mock_backfill.called
        assert engine._startup_backfill_date == t_0950.date()
