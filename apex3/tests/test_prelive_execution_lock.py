"""Unit tests verifying pre-live execution lock bug fixes in signal.py and engine.py."""
from datetime import date, datetime, time, timedelta
from unittest.mock import MagicMock
import zoneinfo

import pytest

from apex3.config import AppConfig, CostsConfig, LimitsConfig, RiskConfig, SignalConfig, StopsConfig
from apex3.indicators.vwap import compute_session_vwap
from apex3.models import (
    Candle,
    Decision,
    GateResult,
    GateStatus,
    InstrumentInfo,
    MarginInfo,
    OpeningRange,
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
from apex3.reconciliation.state_machine import PositionPhase
from apex3.strategy.engine import StrategyEngine
from apex3.strategy.regime import MarketRegime
from apex3.strategy.signal import SignalEvaluation, build_opening_range, evaluate_signal

IST = zoneinfo.ZoneInfo("Asia/Kolkata")


def test_opening_range_excludes_0930_candle():
    """Verify that candles with timestamp 09:30 are strictly excluded from the Opening Range."""
    test_d = date(2026, 3, 20)
    # 09:15, 09:20, 09:25 bars
    candles = [
        Candle(timestamp=datetime(2026, 3, 20, 9, 15, tzinfo=IST), open=100.0, high=105.0, low=99.0, close=104.0, volume=1000, is_complete=True),
        Candle(timestamp=datetime(2026, 3, 20, 9, 20, tzinfo=IST), open=104.0, high=106.0, low=103.0, close=105.0, volume=1200, is_complete=True),
        Candle(timestamp=datetime(2026, 3, 20, 9, 25, tzinfo=IST), open=105.0, high=107.0, low=104.0, close=106.0, volume=1100, is_complete=True),
        # 09:30 breakout bar (should NOT be included in Opening Range high/low)
        Candle(timestamp=datetime(2026, 3, 20, 9, 30, tzinfo=IST), open=106.0, high=120.0, low=106.0, close=119.0, volume=5000, is_complete=True),
    ]

    or_range = build_opening_range("TEST", candles, session_date=test_d)
    assert or_range.is_complete is True
    assert or_range.candle_count == 3
    assert or_range.high == 107.0  # NOT 120.0!
    assert or_range.low == 99.0


def test_session_vwap_today_candles_isolation():
    """Verify evaluate_signal computes session VWAP using today's candles rather than multi-day history."""
    yesterday = date(2026, 3, 19)
    today = date(2026, 3, 20)

    # Yesterday: Massive volume at 50.0 (would pull down VWAP if included)
    hist_candles = [
        Candle(timestamp=datetime(2026, 3, 19, 15, 20, tzinfo=IST), open=50.0, high=50.0, low=50.0, close=50.0, volume=1000000, is_complete=True),
        Candle(timestamp=datetime(2026, 3, 19, 15, 25, tzinfo=IST), open=50.0, high=50.0, low=50.0, close=50.0, volume=1000000, is_complete=True),
    ]

    # Today: 09:15, 09:20, 09:25 at ~100.0, tightly bounded to pass ADR exhaustion (range <= 1.5%)
    today_candles = [
        Candle(timestamp=datetime(2026, 3, 20, 9, 15, tzinfo=IST), open=100.0, high=100.3, low=100.0, close=100.2, volume=1000, is_complete=True),
        Candle(timestamp=datetime(2026, 3, 20, 9, 20, tzinfo=IST), open=100.2, high=100.4, low=100.1, close=100.3, volume=1000, is_complete=True),
        Candle(timestamp=datetime(2026, 3, 20, 9, 25, tzinfo=IST), open=100.3, high=100.5, low=100.2, close=100.4, volume=1000, is_complete=True),
        Candle(timestamp=datetime(2026, 3, 20, 9, 30, tzinfo=IST), open=100.4, high=101.0, low=100.3, close=100.9, volume=3000, is_complete=True),
    ]

    all_candles = hist_candles + today_candles
    or_range = OpeningRange(symbol="TEST", high=100.5, low=100.0, is_complete=True, candle_count=3)
    inst = InstrumentInfo(symbol="TEST", exchange="NSE", lot_size=1, tick_size=0.05)
    quote = Quote(symbol="TEST", exchange="NSE", last_price=100.9, bid=100.85, ask=100.95, volume=6000, timestamp=today_candles[-1].timestamp)

    cfg = SignalConfig(min_relative_volume=1.0, min_clv_ratio=0.5, max_spread_bps=20.0)
    sig = evaluate_signal(
        symbol="TEST",
        stock_candles_5m=all_candles,
        benchmark_candles_5m=all_candles,
        opening_range=or_range,
        market_regime=MarketRegime.BULLISH,
        instrument=inst,
        quote=quote,
        config=cfg,
        is_entry_window=True,
    )

    # Check that VWAP alignment gate was evaluated and passed
    vwap_gate = next((g for g in sig.gates if g.name == "VWAP_ALIGNMENT"), None)
    assert vwap_gate is not None
    assert vwap_gate.status == GateStatus.PASS
    # If yesterday's candles were included, VWAP would be near ~50.
    # Isolated to today, VWAP is ~100.50.
    assert "100.50" in vwap_gate.reason, f"Expected 100.50 in reason, got {vwap_gate.reason}"


def test_engine_clears_opening_range_on_new_session():
    """Verify that StrategyEngine wipes its opening range cache when session date advances."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_positions.return_value = []

    cfg = AppConfig()
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    day1 = datetime(2026, 3, 20, 10, 0, tzinfo=IST)
    day2 = datetime(2026, 3, 23, 10, 0, tzinfo=IST)

    # Populate OR cache on day 1
    engine.opening_ranges["RELIANCE"] = OpeningRange(symbol="RELIANCE", high=2500.0, low=2480.0, is_complete=True)
    assert "RELIANCE" in engine.opening_ranges

    # Set current session date to Day 1
    engine._current_session_date = date(2026, 3, 20)

    # Evaluate step on Day 2
    engine.evaluate_step(day2)

    # Opening ranges should have been wiped
    assert len(engine.opening_ranges) == 0, "Expected opening ranges to be cleared on new trading day"


def test_target_exit_cancels_protective_sl_first():
    """Verify engine cancels protective SL order BEFORE submitting target MARKET exit."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_positions.return_value = []

    now = datetime(2026, 3, 20, 11, 0, tzinfo=IST)
    mock_broker.get_quote.return_value = Quote(symbol="TCS", exchange="NSE", last_price=3100.0, bid=3099.0, ask=3101.0, volume=5000, timestamp=now)
    mock_broker.get_instrument_info.return_value = InstrumentInfo(symbol="TCS", exchange="NSE", lot_size=1, tick_size=0.05)

    cfg = AppConfig(stops=StopsConfig(use_fixed_target=True, target_r=2.0))
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    # Setup an open position with protective order ID and target reached
    pos = engine.state_machine.get_or_create_position("TCS")
    pos.phase = PositionPhase.OPEN_PROTECTED
    pos.side = Side.BUY
    pos.quantity = 10
    pos.average_entry = 3000.0
    pos.initial_stop_price = 2950.0
    pos.current_stop_price = 2950.0
    pos.target_price = 3100.0  # current_price 3100 >= target 3100
    pos.protective_order_id = "SL_ORDER_999"

    # Track order of broker calls
    call_order = []
    mock_broker.cancel_order.side_effect = lambda oid: call_order.append(f"cancel_{oid}")
    mock_broker.place_order.side_effect = lambda req: call_order.append(f"place_{req.order_type.value}") or "EXIT_ORDER_100"

    decisions = engine.evaluate_step(now)

    assert "cancel_SL_ORDER_999" in call_order
    assert "place_MARKET" in call_order
    # Verify cancel occurred BEFORE place
    cancel_idx = call_order.index("cancel_SL_ORDER_999")
    place_idx = call_order.index("place_MARKET")
    assert cancel_idx < place_idx, "Protective SL order must be cancelled BEFORE target exit order is placed!"


def test_entry_rejected_prevents_naked_sl():
    """Verify that when an entry order is rejected by broker, no protective SL is placed and position is not opened."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_positions.return_value = []
    mock_broker.estimate_order_margin.return_value = OrderMarginEstimate(required_margin=6500.0, leverage_available=4.0)

    now = datetime(2026, 3, 20, 9, 35, tzinfo=IST)
    mock_broker.get_instrument_info.return_value = InstrumentInfo(symbol="HINDALCO", exchange="NSE", lot_size=1, tick_size=0.05)
    mock_broker.get_quote.return_value = Quote(symbol="HINDALCO", exchange="NSE", last_price=650.0, bid=649.95, ask=650.05, volume=10000, timestamp=now)

    # Reject the entry order
    mock_broker.place_order.return_value = "ENTRY_ORD_REJECTED"
    dummy_req = OrderRequest(symbol="HINDALCO", side=Side.BUY, quantity=40, price=650.0)
    mock_broker.get_order_status.return_value = OrderState(
        order_id="ENTRY_ORD_REJECTED",
        request=dummy_req,
        status=OrderStatus.REJECTED,
        status_message="RMS: Margin Insufficient",
        filled_quantity=0,
    )

    cfg = AppConfig()
    object.__setattr__(cfg.watchlist, "symbols", ["HINDALCO"])
    object.__setattr__(cfg.watchlist, "scanner_enabled", False)
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    # 15 candles so ATR is realistic and Gate 9 stop width is valid
    candles = []
    for i in range(15):
        t = datetime(2026, 3, 20, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
        candles.append(Candle(timestamp=t, open=649.0, high=650.0, low=648.2, close=649.5, volume=10000, is_complete=True))
    candles[-1] = Candle(timestamp=datetime(2026, 3, 20, 10, 25, tzinfo=IST), open=649.5, high=650.5, low=649.0, close=650.0, volume=35000, is_complete=True)
    mock_broker.get_historical.return_value = candles

    # Mock evaluate_signal to return PROPOSED_LONG
    fake_sig = SignalEvaluation(
        symbol="HINDALCO",
        decision=Decision.PROPOSED_LONG,
        signal_price=650.0,
        side=Side.BUY,
        gates=[GateResult("TEST", GateStatus.PASS, "OK")],
        relative_strength=0.05,
        relative_volume=3.0,
    )

    import apex3.strategy.engine as eng_module
    orig_eval_sig = eng_module.evaluate_signal
    orig_regime = eng_module.evaluate_market_regime
    try:
        eng_module.evaluate_signal = MagicMock(return_value=fake_sig)
        eng_module.evaluate_market_regime = MagicMock(return_value=MagicMock(regime=MarketRegime.BULLISH, is_valid=True))

        decisions = engine.evaluate_step(now)

        # Only place_order for entry should be called, NEVER for protective SL
        assert mock_broker.place_order.call_count == 1
        placed_req = mock_broker.place_order.call_args[0][0]
        assert placed_req.symbol == "HINDALCO"
        assert placed_req.order_type == OrderType.LIMIT

        # Ensure no position is open
        open_pos = [p for p in engine.state_machine.positions.values() if p.quantity > 0]
        assert len(open_pos) == 0, "No open position should exist if entry was rejected"
        assert decisions[0].reason_codes == ["ENTRY_REJECTED"]
    finally:
        eng_module.evaluate_signal = orig_eval_sig
        eng_module.evaluate_market_regime = orig_regime


def test_unfilled_entry_order_does_not_place_naked_sl():
    """Verify that when entry order is OPEN (0 filled), phase is ENTERING, qty is 0, and no SL is placed."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_positions.return_value = []
    mock_broker.estimate_order_margin.return_value = OrderMarginEstimate(required_margin=6500.0, leverage_available=4.0)

    now = datetime(2026, 3, 20, 9, 35, tzinfo=IST)
    mock_broker.get_instrument_info.return_value = InstrumentInfo(symbol="HINDALCO", exchange="NSE", lot_size=1, tick_size=0.05)
    mock_broker.get_quote.return_value = Quote(symbol="HINDALCO", exchange="NSE", last_price=650.0, bid=649.95, ask=650.05, volume=10000, timestamp=now)

    # Order accepted on orderbook, but 0 filled yet
    mock_broker.place_order.return_value = "ENTRY_ORD_OPEN"
    dummy_req = OrderRequest(symbol="HINDALCO", side=Side.BUY, quantity=40, price=650.0)
    mock_broker.get_order_status.return_value = OrderState(
        order_id="ENTRY_ORD_OPEN",
        request=dummy_req,
        status=OrderStatus.OPEN,
        filled_quantity=0,
    )

    cfg = AppConfig()
    object.__setattr__(cfg.watchlist, "symbols", ["HINDALCO"])
    object.__setattr__(cfg.watchlist, "scanner_enabled", False)
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    candles = []
    for i in range(15):
        t = datetime(2026, 3, 20, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
        candles.append(Candle(timestamp=t, open=649.0, high=650.0, low=648.2, close=649.5, volume=10000, is_complete=True))
    mock_broker.get_historical.return_value = candles

    fake_sig = SignalEvaluation(
        symbol="HINDALCO",
        decision=Decision.PROPOSED_LONG,
        signal_price=650.0,
        side=Side.BUY,
        gates=[GateResult("TEST", GateStatus.PASS, "OK")],
        relative_strength=0.05,
        relative_volume=3.0,
    )

    import apex3.strategy.engine as eng_module
    orig_eval_sig = eng_module.evaluate_signal
    orig_regime = eng_module.evaluate_market_regime
    try:
        eng_module.evaluate_signal = MagicMock(return_value=fake_sig)
        eng_module.evaluate_market_regime = MagicMock(return_value=MagicMock(regime=MarketRegime.BULLISH, is_valid=True))

        decisions = engine.evaluate_step(now)

        # place_order was called exactly once (for entry), NEVER for protective SL
        assert mock_broker.place_order.call_count == 1
        pos = engine.state_machine.positions.get("HINDALCO")
        assert pos is not None
        assert pos.phase == PositionPhase.ENTERING
        assert pos.quantity == 0
        assert pos.protective_order_id == ""
    finally:
        eng_module.evaluate_signal = orig_eval_sig
        eng_module.evaluate_market_regime = orig_regime


def test_bar_deduplication_prevents_multiple_entries_on_same_bar():
    """Verify that multiple evaluate_step cycles on the same 5m bar do not trigger duplicate entries."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_positions.return_value = []
    mock_broker.estimate_order_margin.return_value = OrderMarginEstimate(required_margin=6500.0, leverage_available=4.0)

    now1 = datetime(2026, 3, 20, 9, 35, 3, tzinfo=IST)
    now2 = datetime(2026, 3, 20, 9, 35, 6, tzinfo=IST)
    mock_broker.get_instrument_info.return_value = InstrumentInfo(symbol="HINDALCO", exchange="NSE", lot_size=1, tick_size=0.05)
    mock_broker.get_quote.return_value = Quote(symbol="HINDALCO", exchange="NSE", last_price=650.0, bid=649.95, ask=650.05, volume=10000, timestamp=now1)

    mock_broker.place_order.return_value = "ORD_1"
    dummy_req = OrderRequest(symbol="HINDALCO", side=Side.BUY, quantity=40, price=650.0)
    mock_broker.get_order_status.return_value = OrderState(
        order_id="ORD_1",
        request=dummy_req,
        status=OrderStatus.COMPLETE,
        filled_quantity=40,
        average_price=650.0,
    )

    cfg = AppConfig()
    object.__setattr__(cfg.watchlist, "symbols", ["HINDALCO"])
    object.__setattr__(cfg.watchlist, "scanner_enabled", False)
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    candles = []
    for i in range(15):
        t = datetime(2026, 3, 20, 9, 15, tzinfo=IST) + timedelta(minutes=5 * i)
        candles.append(Candle(timestamp=t, open=649.0, high=650.0, low=648.2, close=649.5, volume=10000, is_complete=True))
    mock_broker.get_historical.return_value = candles

    fake_sig = SignalEvaluation(
        symbol="HINDALCO",
        decision=Decision.PROPOSED_LONG,
        signal_price=650.0,
        side=Side.BUY,
        gates=[GateResult("TEST", GateStatus.PASS, "OK")],
        relative_strength=0.05,
        relative_volume=3.0,
    )

    import apex3.strategy.engine as eng_module
    orig_eval_sig = eng_module.evaluate_signal
    orig_regime = eng_module.evaluate_market_regime
    try:
        eng_module.evaluate_signal = MagicMock(return_value=fake_sig)
        eng_module.evaluate_market_regime = MagicMock(return_value=MagicMock(regime=MarketRegime.BULLISH, is_valid=True))

        # First cycle at 09:35:03 -> takes entry
        engine.evaluate_step(now1)
        # Entry + SL placed = 2 order placements
        assert mock_broker.place_order.call_count == 2

        # Second cycle at 09:35:06 (3 seconds later on same bar)
        engine.evaluate_step(now2)
        # Call count should STILL be 2 (no new entry placed!)
        assert mock_broker.place_order.call_count == 2
    finally:
        eng_module.evaluate_signal = orig_eval_sig
        eng_module.evaluate_market_regime = orig_regime


def test_reconciler_flattens_residual_broker_quantity():
    """Verify reconciler flattens residual open position from broker even if internal quantity was 0."""
    from apex3.reconciliation.reconciler import Reconciler
    from apex3.reconciliation.state_machine import StateMachine

    mock_broker = MagicMock()
    mock_journal = MagicMock()
    sm = StateMachine("APEX3")

    # Track TCS in internal state but with quantity 0 (e.g. marked exiting)
    pos = sm.get_or_create_position("TCS")
    pos.side = Side.BUY
    pos.quantity = 0
    pos.phase = PositionPhase.EXITING

    # Broker still has 15 shares open!
    mock_broker.get_positions.return_value = [
        PositionState(symbol="TCS", side=Side.BUY, quantity=15, average_entry=3000.0, current_price=3050.0)
    ]
    mock_broker.get_orders.return_value = []
    mock_broker.place_order.return_value = "EXIT_FLATTEN_ORD"

    reconciler = Reconciler(mock_broker, sm, mock_journal)
    success, msg = reconciler.execute_flatten_sequence()

    assert success is True
    # Verify market exit order was placed for the 15 broker shares!
    assert mock_broker.place_order.call_count == 1
    placed_req = mock_broker.place_order.call_args[0][0]
    assert placed_req.symbol == "TCS"
    assert placed_req.quantity == 15
    assert placed_req.order_type == OrderType.MARKET
    assert placed_req.side == Side.SELL

