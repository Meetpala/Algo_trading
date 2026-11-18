"""Unit & integration tests verifying all P0 and P1 pre-deployment audit fixes.

Covers:
1. Explicit & fail-closed operating mode resolution (`resolve_operating_mode`) and docker-compose safe default.
2. Exact parity between `size_backtest_quantity` (backtest) and `size_position` (live sizer), including the
   BAJFINANCE 2026-09-09 short trade fee-inclusive Rs. 120 risk cap.
3. Stop-width minimum alignment (0.55% floor and 0.90% ceiling) across config, signal gate, and boundary tests.
4. NSE Retail Algo order-type compliance lock in `KiteAdapter` (prohibiting MARKET and SL-M orders).
5. Stop-limit (SL) gap-through emergency handling, unconfirmed cancellation safety, and non-tick circuit clamping.
6. Late-start (09:50) startup recovery fail-closed behavior when a recovered position's protective stop cannot be confirmed.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, time
from pathlib import Path
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

import pytest

from apex3.broker.kite_adapter import KiteAdapter
from apex3.config import CostsConfig, load_config
from apex3.journal.sqlite_journal import Journal
from apex3.main import resolve_operating_mode
from apex3.models import (
    Decision,
    InstrumentInfo,
    MarginInfo,
    OrderRequest,
    OrderState,
    OrderStatus,
    OrderType,
    PositionState,
    Product,
    Quote,
    Side,
)
from apex3.reconciliation.state_machine import PositionPhase
from apex3.strategy.engine import StrategyEngine
from apex3.strategy.regime import MarketRegime
from apex3.strategy.signal import StockCandidateBar, evaluate_candidate_signal
from apex3.strategy.sizer import calculate_zerodha_intraday_charges, size_position
from backtest_3months import size_backtest_quantity

IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------------------
# 1. Explicit & Fail-Closed Operating Mode Resolution (P0)
# ---------------------------------------------------------------------------

def test_resolve_operating_mode_defaults_to_paper_and_compose_is_safe():
    """Verify default mode is paper and docker-compose.yml fails closed to paper when APEX3_MODE is unset."""
    cfg = load_config()
    cfg.mode = "paper"
    assert resolve_operating_mode(cfg, cli_mode=None, env_mode="") == "paper"

    compose_path = Path(__file__).resolve().parents[2] / "docker-compose.yml"
    compose_text = compose_path.read_text(encoding="utf-8")
    assert "${APEX3_MODE:-paper}" in compose_text
    assert "${APEX3_MODE:-live}" not in compose_text


def test_resolve_operating_mode_fails_closed_on_cli_env_conflict():
    """Conflicting CLI --mode and APEX3_MODE environment variable must raise ValueError."""
    cfg = load_config()
    with pytest.raises(ValueError, match="FAIL_CLOSED: Conflicting run modes"):
        resolve_operating_mode(cfg, cli_mode="live", env_mode="paper")


def test_resolve_operating_mode_fails_closed_when_live_gates_missing():
    """Requesting live mode without all 3 compliance/safety gates enabled must raise PermissionError."""
    cfg = load_config()
    cfg.secrets = replace(
        cfg.secrets,
        live_trading_enabled=True,
        static_ip_configured=False,
        sebi_compliance_acknowledged=True,
    )
    with pytest.raises(PermissionError, match="STATIC_IP_CONFIGURED=true"):
        resolve_operating_mode(cfg, cli_mode="live", env_mode="live")

    cfg_all = load_config()
    cfg_all.secrets = replace(
        cfg_all.secrets,
        live_trading_enabled=True,
        static_ip_configured=True,
        sebi_compliance_acknowledged=True,
    )
    assert resolve_operating_mode(cfg_all, cli_mode="live", env_mode="live") == "live"


# ---------------------------------------------------------------------------
# 2. Backtest Sizer vs. Production Sizer Parity (P0 - BAJFINANCE 2026-09-09)
# ---------------------------------------------------------------------------

def test_bajfinance_short_2026_09_09_sizer_parity_under_120_risk_cap():
    """Verify BAJFINANCE short (entry 1042.75, stop 1051.30) sizes to 12 shares (net loss Rs. 115.97 <= Rs. 120)."""
    cfg = load_config()
    entry_price = 1042.75
    stop_price = 1051.30
    r_dist = abs(entry_price - stop_price)  # 8.55

    bt_qty = size_backtest_quantity(
        entry_price=entry_price,
        stop_price=stop_price,
        side=Side.SELL,
        slot_budget=28_500.0,
        risk_cap=120.0,
        target_r=2.0,
        costs=CostsConfig(),
        spread_bps=0.0,
        tick_size=0.05,
    )

    live_res = size_position(
        symbol="BAJFINANCE",
        side=Side.SELL,
        entry_price=entry_price,
        stop_price=stop_price,
        margin_info=MarginInfo(equity_available=15_000.0, net_value=15_000.0),
        instrument=InstrumentInfo(symbol="BAJFINANCE", exchange="NSE", tick_size=0.05, lot_size=1),
        num_qualified_symbols=2,
        limits_config=cfg.limits,
        risk_config=cfg.risk,
    )

    assert bt_qty == 12
    assert live_res.quantity == 12
    stop_charges = calculate_zerodha_intraday_charges(stop_price, entry_price, bt_qty)
    total_stop_loss_inr = round((r_dist * bt_qty) + stop_charges, 2)
    assert total_stop_loss_inr == 115.97
    assert total_stop_loss_inr <= 120.00


# ---------------------------------------------------------------------------
# 3. Stop-Width Minimum Rulebook Alignment & Boundary Tests (P1)
# ---------------------------------------------------------------------------

def test_stop_width_minimum_matches_rulebook_and_boundaries():
    """Verify 0.55% <= R% <= 0.90% stop-width bounds are enforced at exact boundary values."""
    cfg = load_config()
    assert cfg.signal.min_stop_distance_pct == pytest.approx(0.0055)
    assert cfg.signal.max_stop_distance_pct == pytest.approx(0.0090)

    def _make_candidate_with_swing_low(swing_low: float, atr14: float = 3.0) -> StockCandidateBar:
        # LONG initial_stop = floor_0.05(swing_low - 1.5 * atr14) = floor_0.05(swing_low - 4.50)
        return StockCandidateBar(
            symbol="TRENT",
            timestamp_hhmm="09:40",
            close=1000.0,
            or_high=995.0,
            or_low=988.0,
            session_vwap=993.0,
            stock_return_20=0.015,
            nifty_return_20=0.005,
            bar_volume=250_000,
            avg_volume_20=100_000,
            bid_ask_spread_bps=2.0,
            upper_circuit=1100.0,
            lower_circuit=900.0,
            swing_low_5=swing_low,
            swing_high_5=1000.0,
            atr14=atr14,
            session_high_so_far=1000.0,
            session_low_so_far=988.0,
            adr14_pct=0.028,
            bar_high=1000.5,
            bar_low=994.0,
            tick_size=0.05,
        )

    # 1. Below 0.55% floor: swing_low=999.10, 1.5*ATR=4.5 -> initial_stop=994.60 -> R%=0.54% (< 0.55%) -> REJECTED
    sig_narrow = evaluate_candidate_signal(
        _make_candidate_with_swing_low(999.10),
        MarketRegime.BULLISH,
        active_positions_count=0,
        cfg=cfg.raw_dict,
    )
    assert sig_narrow is None

    # 2. At 0.55% lower boundary: swing_low=999.00, 1.5*ATR=4.5 -> initial_stop=994.50 -> R%=0.55% -> ACCEPTED
    sig_lower_ok = evaluate_candidate_signal(
        _make_candidate_with_swing_low(999.00),
        MarketRegime.BULLISH,
        active_positions_count=0,
        cfg=cfg.raw_dict,
    )
    assert sig_lower_ok is not None
    assert sig_lower_ok.levels.risk_pct == pytest.approx(0.0055)

    # 3. At 0.90% upper boundary: swing_low=995.50, 1.5*ATR=4.5 -> initial_stop=991.00 -> R%=0.90% -> ACCEPTED
    sig_upper_ok = evaluate_candidate_signal(
        _make_candidate_with_swing_low(995.50),
        MarketRegime.BULLISH,
        active_positions_count=0,
        cfg=cfg.raw_dict,
    )
    assert sig_upper_ok is not None
    assert sig_upper_ok.levels.risk_pct == pytest.approx(0.0090)

    # 4. Above 0.90% ceiling: swing_low=995.40, 1.5*ATR=4.5 -> initial_stop=990.90 -> R%=0.91% -> REJECTED
    sig_wide = evaluate_candidate_signal(
        _make_candidate_with_swing_low(995.40),
        MarketRegime.BULLISH,
        active_positions_count=0,
        cfg=cfg.raw_dict,
    )
    assert sig_wide is None


# ---------------------------------------------------------------------------
# 4. NSE Retail Algo Order-Type Compliance Lock (P1)
# ---------------------------------------------------------------------------

def test_kite_adapter_blocks_market_and_slm_orders():
    """Verify KiteAdapter strictly rejects MARKET and SL-M orders on place_order and modify_order."""
    adapter = KiteAdapter(api_key="test_k", access_token="test_t", live_enabled=True)
    adapter._kite = MagicMock()

    mkt_req = OrderRequest(
        symbol="TRENT",
        side=Side.BUY,
        quantity=4,
        price=0.0,
        product=Product.MIS,
        order_type=OrderType.MARKET,
    )
    with pytest.raises(ValueError, match="NSE_ALGO_COMPLIANCE_BLOCK"):
        adapter.place_order(mkt_req)

    slm_req = OrderRequest(
        symbol="TRENT",
        side=Side.SELL,
        quantity=4,
        price=0.0,
        trigger_price=2800.0,
        product=Product.MIS,
        order_type=OrderType.SL_M,
    )
    with pytest.raises(ValueError, match="NSE_ALGO_COMPLIANCE_BLOCK"):
        adapter.place_order(slm_req)

    with pytest.raises(ValueError, match="NSE_ALGO_COMPLIANCE_BLOCK"):
        adapter.modify_order("ORD123", order_type="MARKET")

    with pytest.raises(ValueError, match="NSE_ALGO_COMPLIANCE_BLOCK"):
        adapter.modify_order("ORD123", order_type="SL-M")


# ---------------------------------------------------------------------------
# 5. Stop-Limit Gap Handling & Non-Tick Circuit Clamping (P1)
# ---------------------------------------------------------------------------

def test_stop_limit_gap_through_cancels_sl_and_dispatches_bounded_limit_exit(tmp_path):
    """When SL triggers to OPEN and price gaps past limit, engine cancels SL and places bounded LIMIT exit."""
    cfg = load_config()
    journal = Journal(db_path=str(tmp_path / "gap_test.db"))
    broker = MagicMock()
    now = datetime(2026, 11, 18, 10, 15, tzinfo=IST)

    broker.is_connected.return_value = True
    broker.get_positions.return_value = [
        PositionState(symbol="TRENT", side=Side.BUY, quantity=4, average_entry=2800.0, product=Product.MIS)
    ]
    sl_req = OrderRequest(
        symbol="TRENT",
        side=Side.SELL,
        quantity=4,
        price=2773.0,
        trigger_price=2780.0,
        product=Product.MIS,
        order_type=OrderType.SL,
        tag="APX_TRENT_SL",
    )
    open_sl_state = OrderState(
        order_id="SL_101",
        request=sl_req,
        status=OrderStatus.OPEN,
        filled_quantity=0,
        pending_quantity=4,
    )
    cancelled_sl_state = OrderState(
        order_id="SL_101",
        request=sl_req,
        status=OrderStatus.CANCELLED,
        filled_quantity=0,
        pending_quantity=0,
    )
    broker.get_orders.return_value = [open_sl_state]
    # First call sees OPEN (triggered but gapped through 2773.0), second call (after cancel) sees CANCELLED
    broker.get_order_status.side_effect = [open_sl_state, cancelled_sl_state]
    broker.get_quote.return_value = Quote(
        symbol="TRENT",
        exchange="NSE",
        last_price=2750.0,
        bid=2749.80,
        ask=2750.20,
        volume=500_000,
        timestamp=now,
        is_fresh=True,
    )
    broker.get_instrument_info.return_value = InstrumentInfo(
        symbol="TRENT",
        exchange="NSE",
        tick_size=0.05,
        lot_size=1,
        lower_circuit=2520.03,
        upper_circuit=3080.00,
    )
    broker.place_order.return_value = "EMG_202"

    engine = StrategyEngine(config=cfg, broker=broker, journal=journal)
    engine._startup_backfill_date = now.date()
    pos = engine.state_machine.get_or_create_position("TRENT")
    pos.symbol = "TRENT"
    pos.side = Side.BUY
    pos.quantity = 4
    pos.average_entry = 2800.0
    pos.initial_stop_price = 2780.0
    pos.current_stop_price = 2780.0
    pos.protective_order_id = "SL_101"
    pos.phase = PositionPhase.OPEN_PROTECTED

    decisions = engine.evaluate_step(now)
    exit_dec = next((d for d in decisions if d.symbol == "TRENT"), None)
    assert exit_dec is not None
    assert "STOP_TRIGGERED_UNFILLED_GAP_EXIT" in exit_dec.reason_codes
    broker.cancel_order.assert_called_once_with("SL_101")
    placed_req = broker.place_order.call_args[0][0]
    assert placed_req.order_type == OrderType.LIMIT
    assert placed_req.price >= 2520.05  # Clamped inside lower_circuit


def test_non_tick_circuit_clamp_never_rounds_outside_circuit(tmp_path):
    """Verify SELL exit near lower circuit 11.61 rounds UP to 11.65 (never 11.60 below circuit)."""
    cfg = load_config()
    journal = Journal(db_path=str(tmp_path / "circuit_clamp.db"))
    broker = MagicMock()
    now = datetime.now(IST)
    broker.get_instrument_info.return_value = InstrumentInfo(
        symbol="IDEA",
        exchange="NSE",
        tick_size=0.05,
        lot_size=1,
        lower_circuit=11.61,
        upper_circuit=14.19,
    )
    broker.get_quote.return_value = Quote(
        symbol="IDEA",
        exchange="NSE",
        last_price=11.62,
        bid=11.62,
        ask=11.65,
        volume=1_000_000,
        timestamp=now,
        is_fresh=True,
    )
    engine = StrategyEngine(config=cfg, broker=broker, journal=journal)
    sell_req = engine._create_exit_order_request(
        symbol="IDEA",
        side=Side.SELL,
        quantity=10,
        tag="TEST_EMG",
        current_price=11.62,
    )
    assert sell_req.order_type == OrderType.LIMIT
    assert sell_req.price >= 11.61
    assert sell_req.price == pytest.approx(11.65)


# ---------------------------------------------------------------------------
# 6. Late-Start Recovery Fail-Closed When Stop Cannot Be Confirmed (P1)
# ---------------------------------------------------------------------------

def test_late_start_recovery_fails_closed_and_exits_when_stop_cannot_be_confirmed(tmp_path):
    """If a recovered position has no resting stop and SL placement fails confirmation, engine flattens and pauses."""
    cfg = load_config()
    journal = Journal(db_path=str(tmp_path / "recovery_fail_closed.db"))
    broker = MagicMock()
    now = datetime(2026, 11, 18, 9, 50, tzinfo=IST)

    # Broker reports an open MIS position on TRENT
    broker.get_positions.return_value = [
        PositionState(symbol="TRENT", side=Side.BUY, quantity=4, average_entry=2800.0, product=Product.MIS)
    ]
    # Broker orderbook shows the filled strategy entry order, but NO resting TRIGGER_PENDING stop order
    entry_req = OrderRequest(
        symbol="TRENT",
        side=Side.BUY,
        quantity=4,
        price=2800.0,
        product=Product.MIS,
        order_type=OrderType.LIMIT,
        tag="APEX3_TRENT_ENT",
    )
    broker.get_orders.return_value = [
        OrderState(order_id="ENT_1", request=entry_req, status=OrderStatus.COMPLETE, filled_quantity=4)
    ]
    broker.get_trades.return_value = []
    broker.get_instrument_info.return_value = InstrumentInfo(
        symbol="TRENT",
        exchange="NSE",
        tick_size=0.05,
        lot_size=1,
        lower_circuit=2520.0,
        upper_circuit=3080.0,
    )
    broker.get_quote.return_value = Quote(
        symbol="TRENT",
        exchange="NSE",
        last_price=2805.0,
        bid=2804.80,
        ask=2805.20,
        volume=100_000,
        timestamp=now,
        is_fresh=True,
    )
    # Simulate SL reattachment failing confirmation (returns REJECTED instead of TRIGGER_PENDING)
    sl_rejected = OrderState(
        order_id="SL_REJ",
        request=entry_req,
        status=OrderStatus.REJECTED,
        filled_quantity=0,
    )
    broker.place_order.side_effect = ["SL_REJ", "EMG_EXIT_1"]
    broker.get_order_status.return_value = sl_rejected
    broker.get_historical.return_value = []

    engine = StrategyEngine(config=cfg, broker=broker, journal=journal)
    engine.state_machine.session_tag = "APX"

    res = engine.reconcile_and_backfill_late_start(now)
    assert res["is_clean"] is False
    assert engine.is_paused is True
    pos = engine.state_machine.positions["TRENT"]
    assert pos.phase == PositionPhase.EXITING
    assert pos.exit_order_id == "EMG_EXIT_1"
    # Second place_order call must be the emergency bounded LIMIT exit
    emg_req = broker.place_order.call_args_list[1][0][0]
    assert emg_req.order_type == OrderType.LIMIT
    assert emg_req.side == Side.SELL
    assert emg_req.quantity == 4
