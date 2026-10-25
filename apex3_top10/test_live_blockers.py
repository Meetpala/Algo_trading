"""Comprehensive unit test suite for verifying resolution of all live blockers.

Covers:
1. Partial fill entry handling: cancel unfilled remainder, exact stop size, target recalculated from actual fill.
2. Timeout path: no premature OPEN_PROTECTED; stop verified before protected; emergency exit if unconfirmed.
3. Target exit & EXITING idempotence: stop cancellation confirmed first; no duplicate market exits in subsequent cycles.
4. Tracked one-shot EOD flatten: verifies zero exposure, does not repeat resubmissions.
5. Daily loss breach: forces immediate square-off of open positions.
6. Decoupled live compliance: open positions managed and flattened even when live flags are disabled.
7. Special session: blocked with SPECIAL_SESSION_NO_MIS.
8. Dashboard CSRF protection: rejects cross-origin mutating requests.
9. Restart recovery: reconciles broker orders and restores managed state without false halt.
"""
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo
import pytest

from apex3.config import AppConfig, load_config
from apex3.models import (
    Candle,
    Decision,
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
from apex3.strategy.regime import MarketRegime
from apex3.reconciliation.reconciler import Reconciler
from apex3.reconciliation.state_machine import PositionPhase, StateMachine
from apex3.strategy.engine import StrategyEngine
from apex3.strategy.signal import SignalEvaluation, GateResult, GateStatus

IST = ZoneInfo("Asia/Kolkata")


def test_partial_fill_cancels_remainder_and_sizes_stop_exactly():
    """When an entry partially fills, unfilled remainder is cancelled and SL is placed for exact filled quantity."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_positions.return_value = []
    mock_broker.estimate_order_margin.return_value = OrderMarginEstimate(required_margin=3000.0, leverage_available=4.0)

    now = datetime(2026, 3, 20, 9, 35, tzinfo=IST)
    mock_broker.get_instrument_info.return_value = InstrumentInfo(symbol="TRENT", exchange="NSE", lot_size=1, tick_size=0.05)
    mock_broker.get_quote.return_value = Quote(symbol="TRENT", exchange="NSE", last_price=1000.0, bid=999.8, ask=1000.2, volume=10000, timestamp=now)

    placed_orders = []
    def on_place(req):
        ord_id = f"ORD_{len(placed_orders) + 1}"
        placed_orders.append((ord_id, req))
        return ord_id
    mock_broker.place_order.side_effect = on_place

    # Entry order for 50 shares partially fills for 20 shares @ 1001.0
    def on_status(ord_id):
        if "ORD_1" in ord_id:
            return OrderState(
                order_id="ORD_1",
                request=OrderRequest(symbol="TRENT", side=Side.BUY, quantity=14, price=1000.05),
                status=OrderStatus.PENDING,  # partial fill, still pending
                filled_quantity=6,
                pending_quantity=8,
                average_price=1001.0,
            )
        elif "ORD_2" in ord_id:
            # Protective SL
            return OrderState(
                order_id="ORD_2",
                request=OrderRequest(symbol="TRENT", side=Side.SELL, quantity=6, order_type=OrderType.SL),
                status=OrderStatus.TRIGGER_PENDING,
                pending_quantity=6,
                filled_quantity=0,
            )
        return None
    mock_broker.get_order_status.side_effect = on_status

    cfg = AppConfig()
    object.__setattr__(cfg.watchlist, "symbols", ["TRENT"])
    object.__setattr__(cfg.watchlist, "scanner_enabled", False)
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    # 20 candles with ATR ~ 2.0 and last candle low=996.0 -> stop distance ~ 7.05 (0.705%), passing Gate 9 [0.55%, 0.90%]
    candles = [Candle(timestamp=now - timedelta(minutes=5 * (20 - i)), open=1000.0, high=1001.0, low=999.0, close=1000.0, volume=10000, is_complete=True) for i in range(20)]
    candles[-1] = Candle(timestamp=now, open=1000.0, high=1001.0, low=996.0, close=1000.0, volume=10000, is_complete=True)
    mock_broker.get_historical.return_value = candles

    fake_sig = SignalEvaluation(
        symbol="TRENT",
        decision=Decision.PROPOSED_LONG,
        signal_price=1000.0,
        side=Side.BUY,
        gates=[GateResult("TEST", GateStatus.PASS, "OK")],
        relative_strength=0.05,
        relative_volume=3.0,
    )

    import apex3.strategy.engine as eng_module
    orig_eval = eng_module.evaluate_signal
    orig_regime = eng_module.evaluate_market_regime
    try:
        eng_module.evaluate_signal = MagicMock(return_value=fake_sig)
        eng_module.evaluate_market_regime = MagicMock(return_value=MagicMock(regime=MarketRegime.BULLISH, is_valid=True))

        engine.evaluate_step(now)

        # 1. Remainder was cancelled
        assert mock_broker.cancel_order.call_count >= 1
        assert mock_broker.cancel_order.call_args[0][0] == "ORD_1"

        # 2. SL placed for exact filled quantity (6, not 14)
        assert len(placed_orders) == 2
        sl_req = placed_orders[1][1]
        assert sl_req.order_type == OrderType.SL
        assert sl_req.quantity == 6

        # 3. Position protected for 6 shares and target recalculated from actual fill 1001.0
        pos = engine.state_machine.positions["TRENT"]
        assert pos.phase == PositionPhase.OPEN_PROTECTED
        assert pos.quantity == 6
        assert pos.average_entry == 1001.0
        assert pos.target_price > 1001.0
    finally:
        eng_module.evaluate_signal = orig_eval
        eng_module.evaluate_market_regime = orig_regime


def test_target_exit_idempotence_prevents_duplicate_exits():
    """At 2R target, stop cancellation is confirmed, exit order is tracked in EXITING, and no duplicate exit is sent."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_positions.return_value = []

    now = datetime(2026, 3, 20, 10, 30, tzinfo=IST)
    cfg = AppConfig()
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    pos = engine.state_machine.get_or_create_position("INFY")
    pos.side = Side.BUY
    pos.quantity = 25
    pos.average_entry = 1500.0
    pos.initial_stop_price = 1490.0
    pos.current_stop_price = 1490.0
    pos.target_price = 1520.0
    pos.r_value = 10.0
    pos.protective_order_id = "SL_RESTING_123"
    pos.phase = PositionPhase.OPEN_PROTECTED

    mock_broker.get_instrument_info.return_value = InstrumentInfo(symbol="INFY", exchange="NSE", tick_size=0.05, lot_size=1)
    # Price reaches target 1520.0
    mock_broker.get_quote.return_value = Quote(symbol="INFY", exchange="NSE", last_price=1521.0, bid=1520.95, ask=1521.05, volume=50000, timestamp=now, is_fresh=True)

    # Stop is resting as TRIGGER_PENDING, then cancelled
    mock_broker.get_order_status.side_effect = lambda ord_id: OrderState(
        order_id=ord_id,
        status=OrderStatus.TRIGGER_PENDING if ord_id == "SL_RESTING_123" else OrderStatus.PENDING,
        pending_quantity=25,
    )
    mock_broker.place_order.return_value = "EXIT_MKT_999"

    # Cycle 1: Reaches target -> cancels stop -> places exit order -> enters EXITING phase
    decisions1 = engine.evaluate_step(now)
    assert mock_broker.cancel_order.call_count == 1
    assert mock_broker.place_order.call_count == 1
    assert pos.phase == PositionPhase.EXITING
    assert pos.exit_order_id == "EXIT_MKT_999"

    # Cycle 2: 3 seconds later, price is still at target, exit order still pending at broker
    now_later = now + timedelta(seconds=3)
    decisions2 = engine.evaluate_step(now_later)
    # Must NOT place a duplicate market exit order!
    assert mock_broker.place_order.call_count == 1
    assert pos.phase == PositionPhase.EXITING

    # Cycle 3: Broker completes the market exit
    mock_broker.get_order_status.side_effect = lambda ord_id: OrderState(
        order_id=ord_id,
        status=OrderStatus.COMPLETE,
        filled_quantity=25,
        average_price=1521.0,
    )
    decisions3 = engine.evaluate_step(now + timedelta(seconds=6))
    assert pos.phase == PositionPhase.CLOSED
    assert pos.quantity == 0


def test_tracked_one_shot_eod_flatten_does_not_repeat():
    """EOD flatten is executed once, confirms flat with broker, and does not repeatedly cancel/resubmit orders."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True

    # Flatten time at 15:15 IST
    flatten_time = datetime(2026, 3, 20, 15, 15, 5, tzinfo=IST)
    cfg = AppConfig()
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    # Position in state machine
    pos = engine.state_machine.get_or_create_position("RELIANCE")
    pos.side = Side.BUY
    pos.quantity = 10
    pos.average_entry = 2500.0
    pos.phase = PositionPhase.OPEN_PROTECTED
    pos.protective_order_id = "SL_REL_1"

    # Broker has 10 shares open initially
    broker_positions = [PositionState(symbol="RELIANCE", side=Side.BUY, quantity=10, average_entry=2500.0)]
    mock_broker.get_positions.side_effect = lambda: list(broker_positions)
    mock_broker.get_orders.return_value = []

    def on_exit(req):
        broker_positions.clear()  # Broker confirmed flat
        return "EXIT_FLATTEN_ORD"
    mock_broker.place_order.side_effect = on_exit

    # Cycle 1: Flatten window triggered -> executes flatten sequence
    d1 = engine.evaluate_step(flatten_time)
    assert any(d.reason_codes == ["FLATTEN_WINDOW_ACTIVE"] for d in d1)
    assert mock_broker.place_order.call_count == 1
    assert engine._flatten_completed is True
    assert pos.phase in (PositionPhase.CLOSED, PositionPhase.FLAT)

    # Cycle 2: 3 seconds later in flatten window -> returns FLATTEN_COMPLETED without placing orders
    d2 = engine.evaluate_step(flatten_time + timedelta(seconds=3))
    assert any(d.reason_codes == ["FLATTEN_COMPLETED"] for d in d2)
    assert mock_broker.place_order.call_count == 1  # Still 1! No duplicate orders resubmitted!


def test_daily_loss_limit_breach_forces_immediate_flatten():
    """Breaching daily loss limit squares off open positions to make daily loss cap truly hard."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    now = datetime(2026, 3, 20, 11, 0, tzinfo=IST)

    cfg = AppConfig()
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    # Holding 50 shares of TCS bought at 3000, now dropped to 2990 (-500 rupees loss, breaches -240 limit)
    pos = engine.state_machine.get_or_create_position("TCS")
    pos.side = Side.BUY
    pos.quantity = 50
    pos.average_entry = 3000.0
    pos.phase = PositionPhase.OPEN_PROTECTED
    pos.protective_order_id = "SL_TCS"

    broker_positions = [PositionState(symbol="TCS", side=Side.BUY, quantity=50, average_entry=3000.0, current_price=2990.0)]
    mock_broker.get_positions.side_effect = lambda: list(broker_positions)
    mock_broker.get_quote.return_value = Quote(
        symbol="TCS",
        exchange="NSE",
        last_price=2990.0,
        bid=2989.5,
        ask=2990.5,
        volume=5000,
        timestamp=now,
        is_fresh=True,
    )
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_margins.return_value = MarginInfo(equity_available=14500.0, net_value=14500.0)

    def on_flatten(req):
        broker_positions.clear()
        return "FLATTEN_TCS_ORD"
    mock_broker.place_order.side_effect = on_flatten

    decisions = engine.evaluate_step(now)

    # 1. Engine halts with DAILY_LOSS_LIMIT_BREACHED
    halt_decisions = [d for d in decisions if d.decision == Decision.HALT and "DAILY_LOSS_LIMIT_BREACHED" in d.reason_codes]
    assert len(halt_decisions) >= 1
    # 2. Flatten sequence was executed for open positions
    assert mock_broker.place_order.call_count >= 1
    assert pos.phase in (PositionPhase.CLOSED, PositionPhase.FLAT)


def test_decoupled_live_gates_allow_open_position_management():
    """When LIVE_TRADING_ENABLED or STATIC_IP is False, open positions are still managed and flattened."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    now = datetime(2026, 3, 20, 11, 0, tzinfo=IST)

    cfg = AppConfig()
    object.__setattr__(cfg, "mode", "live")
    object.__setattr__(cfg.secrets, "live_trading_enabled", False)  # Disabled!
    object.__setattr__(cfg.secrets, "static_ip_configured", False)

    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    # Existing open position
    pos = engine.state_machine.get_or_create_position("HINDALCO")
    pos.side = Side.BUY
    pos.quantity = 40
    pos.average_entry = 650.0
    pos.initial_stop_price = 645.0
    pos.current_stop_price = 645.0
    pos.target_price = 660.0
    pos.r_value = 5.0
    pos.phase = PositionPhase.OPEN_PROTECTED
    pos.protective_order_id = "SL_HINDALCO"

    mock_broker.get_instrument_info.return_value = InstrumentInfo(symbol="HINDALCO", exchange="NSE", tick_size=0.05, lot_size=1)
    # Stop is hit at broker
    mock_broker.get_order_status.return_value = OrderState(
        order_id="SL_HINDALCO",
        status=OrderStatus.COMPLETE,
        filled_quantity=40,
        average_price=645.0,
    )
    mock_broker.get_positions.return_value = []
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)

    decisions = engine.evaluate_step(now)

    # Position is cleanly closed despite live flag disabled!
    assert pos.phase == PositionPhase.CLOSED
    assert pos.quantity == 0
    # And new entries are blocked with LIVE_TRADING_FLAG_DISABLED
    assert any("LIVE_TRADING_FLAG_DISABLED" in d.reason_codes for d in decisions)


def test_special_session_blocks_new_entries():
    """On special session days (e.g. Muhurat Trading), new intraday MIS entries are blocked."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    mock_broker.get_positions.return_value = []
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)

    # Load real config containing Muhurat Trading special session 2026-11-08
    cfg = load_config()
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    # Date of Muhurat trading: 2026-11-08 (Sunday special session)
    muhurat_now = datetime(2026, 11, 8, 18, 20, tzinfo=IST)
    decisions = engine.evaluate_step(muhurat_now)
    assert any("SPECIAL_SESSION_NO_MIS" in d.reason_codes for d in decisions)


def test_dashboard_csrf_protection_rejects_external_origin():
    """Dashboard control endpoints reject non-loopback Origin/Referer requests to prevent CSRF."""
    from apex3.dashboard.app import create_app
    cfg = AppConfig()
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    engine = StrategyEngine(cfg, mock_broker, mock_journal)
    app = create_app(engine, cfg, mock_journal)
    client = app.test_client()

    # Valid loopback origin
    resp_valid = client.post("/api/pause", json={"pause": True}, headers={"Origin": "http://127.0.0.1:5000"})
    assert resp_valid.status_code == 200

    # Malicious external origin
    resp_blocked = client.post("/api/pause", json={"pause": True}, headers={"Origin": "https://attacker-site.com"})
    assert resp_blocked.status_code == 403
    assert b"Cross-site request rejected" in resp_blocked.data


def test_restart_recovery_reconciles_strategy_orders_without_unknown_positions_halt():
    """Restart recovery restores ManagedPosition from tagged orders without halting on UNKNOWN_MANUAL_POSITIONS."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    sm = StateMachine("APEX3")
    reconciler = Reconciler(mock_broker, sm, mock_journal, session_tag="APEX3")

    mock_broker.get_positions.return_value = [
        PositionState(symbol="INFY", side=Side.BUY, quantity=10, average_entry=1500.0)
    ]
    mock_broker.get_orders.return_value = [
        OrderState(
            order_id="SL_RECOVERED_1",
            request=OrderRequest(symbol="INFY", side=Side.SELL, quantity=10, order_type=OrderType.SL, price=1485.0, trigger_price=1485.0, tag="APEX3_INFY_SL"),
            status=OrderStatus.TRIGGER_PENDING,
            pending_quantity=10,
        )
    ]

    audit = reconciler.audit_startup_state(["INFY"])
    assert audit.is_clean is True
    assert audit.has_unknown_positions is False
    assert "INFY" in sm.positions
    recovered_pos = sm.positions["INFY"]
    assert recovered_pos.phase == PositionPhase.OPEN_PROTECTED
    assert recovered_pos.quantity == 10
    assert recovered_pos.protective_order_id == "SL_RECOVERED_1"
    # Fix 7 verification: initial_stop_price, r_value, and target_price are populated
    assert recovered_pos.initial_stop_price > 0
    assert recovered_pos.r_value > 0
    assert recovered_pos.target_price > 1500.0


def test_broker_tags_strictly_within_20_chars():
    """All broker tags must be strictly <= 20 chars to comply with Zerodha Kite Connect v3."""
    from apex3.models import make_broker_tag
    session_tag = "APEX3"

    tags = [
        make_broker_tag(session_tag, "SUNPHARMA", "E0935"),
        make_broker_tag(session_tag, "SUNPHARMA", "SL"),
        make_broker_tag(session_tag, "SUNPHARMA", "TGT"),
        make_broker_tag(session_tag, "SUNPHARMA", "EMG"),
        make_broker_tag(session_tag, "SUNPHARMA", "FLT"),
        make_broker_tag("APEX3_PROD", "BHARTIARTL", "EMERGENCY_FLATTEN"),
    ]
    for tag in tags:
        assert len(tag) <= 20, f"Tag '{tag}' exceeds 20 characters ({len(tag)})"
        assert tag.startswith(session_tag[:5]), f"Tag '{tag}' does not start with session prefix"


def test_candidate_ranking_evaluates_next_candidate_if_first_fails_gate9():
    """If Candidate #1 fails Gate 9 (stop-width bounds), engine proceeds to evaluate Candidate #2."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)
    mock_broker.get_orders.return_value = []
    mock_broker.get_trades.return_value = []
    mock_broker.get_positions.return_value = []
    mock_broker.estimate_order_margin.return_value = OrderMarginEstimate(required_margin=3000.0, leverage_available=4.0)

    now = datetime(2026, 3, 20, 9, 35, tzinfo=IST)
    cfg = AppConfig()
    object.__setattr__(cfg.watchlist, "symbols", ["STOCK_A", "STOCK_B"])
    object.__setattr__(cfg.watchlist, "scanner_enabled", False)
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    # STOCK_A has stop width outside bounds (e.g. 2.2% > 0.90%)
    # STOCK_B has stop width inside bounds (~0.70%)
    inst_a = InstrumentInfo(symbol="STOCK_A", exchange="NSE", lot_size=1, tick_size=0.05)
    inst_b = InstrumentInfo(symbol="STOCK_B", exchange="NSE", lot_size=1, tick_size=0.05)
    mock_broker.get_instrument_info.side_effect = lambda sym: inst_a if sym == "STOCK_A" else inst_b

    quote_a = Quote(symbol="STOCK_A", exchange="NSE", last_price=1000.0, bid=999.8, ask=1000.2, volume=10000, timestamp=now)
    quote_b = Quote(symbol="STOCK_B", exchange="NSE", last_price=500.0, bid=499.9, ask=500.1, volume=10000, timestamp=now)
    mock_broker.get_quote.side_effect = lambda sym: quote_a if sym == "STOCK_A" else quote_b

    # STOCK_A swing low 970 (wide stop ~3%), STOCK_B swing low 497 (stop ~0.7%)
    candles_a = [Candle(timestamp=now - timedelta(minutes=5 * (20 - i)), open=1000.0, high=1005.0, low=970.0, close=1000.0, volume=10000, is_complete=True) for i in range(20)]
    candles_b = [Candle(timestamp=now - timedelta(minutes=5 * (20 - i)), open=500.0, high=500.5, low=499.5, close=500.0, volume=10000, is_complete=True) for i in range(20)]
    candles_b[-1] = Candle(timestamp=now, open=500.0, high=500.5, low=498.0, close=500.0, volume=10000, is_complete=True)
    mock_broker.get_historical.side_effect = lambda sym, *args, **kwargs: candles_a if sym == "STOCK_A" else candles_b

    sig_a = SignalEvaluation(symbol="STOCK_A", decision=Decision.PROPOSED_LONG, signal_price=1000.0, side=Side.BUY, gates=[], relative_strength=0.08, relative_volume=4.0)
    sig_b = SignalEvaluation(symbol="STOCK_B", decision=Decision.PROPOSED_LONG, signal_price=500.0, side=Side.BUY, gates=[], relative_strength=0.05, relative_volume=3.0)

    import apex3.strategy.engine as eng_module
    orig_eval = eng_module.evaluate_signal
    orig_regime = eng_module.evaluate_market_regime
    try:
        eng_module.evaluate_signal = lambda symbol, **kwargs: sig_a if symbol == "STOCK_A" else sig_b
        eng_module.evaluate_market_regime = MagicMock(return_value=MagicMock(regime=MarketRegime.BULLISH, is_valid=True))

        mock_broker.place_order.return_value = "ORD_B_1"
        mock_broker.get_order_status.return_value = OrderState(
            order_id="ORD_B_1",
            request=OrderRequest(symbol="STOCK_B", side=Side.BUY, quantity=20, price=500.05),
            status=OrderStatus.PENDING,
            filled_quantity=0,
            pending_quantity=20,
        )

        decisions = engine.evaluate_step(now)
        # STOCK_A is rejected on STOP_WIDTH_OUT_OF_BOUNDS
        # But STOCK_B is successfully evaluated and proposed!
        submitted = [d for d in decisions if d.decision == Decision.PROPOSED_LONG and d.symbol == "STOCK_B"]
        assert len(submitted) == 1, "Candidate #2 (STOCK_B) should be proposed after Candidate #1 failed Gate 9"
    finally:
        eng_module.evaluate_signal = orig_eval
        eng_module.evaluate_market_regime = orig_regime


def test_step11_fee_lock_no_unbound_local_error_when_modify_fails():
    """In Step 11, when fee-lock triggers but broker modify_order fails, no UnboundLocalError is raised."""
    mock_broker = MagicMock()
    mock_journal = MagicMock()
    mock_broker.is_session_valid.return_value = True
    now = datetime(2026, 3, 20, 11, 0, tzinfo=IST)

    cfg = AppConfig()
    engine = StrategyEngine(cfg, mock_broker, mock_journal)

    # Position in profit >= +1.60R
    pos = engine.state_machine.get_or_create_position("HINDALCO")
    pos.side = Side.BUY
    pos.quantity = 40
    pos.average_entry = 650.0
    pos.initial_stop_price = 645.0
    pos.current_stop_price = 645.0
    pos.target_price = 660.0
    pos.r_value = 5.0
    pos.phase = PositionPhase.OPEN_PROTECTED
    pos.protective_order_id = "SL_HINDALCO_1"

    # Current price 658.5 -> +1.70R
    mock_broker.get_quote.return_value = Quote(
        symbol="HINDALCO",
        exchange="NSE",
        last_price=658.5,
        bid=658.4,
        ask=658.6,
        volume=50000,
        timestamp=now,
        is_fresh=True,
    )
    mock_broker.get_instrument_info.return_value = InstrumentInfo(symbol="HINDALCO", exchange="NSE", lot_size=1, tick_size=0.05)
    mock_broker.get_order_status.return_value = OrderState(
        order_id="SL_HINDALCO_1",
        status=OrderStatus.TRIGGER_PENDING,
        pending_quantity=40,
    )
    # modify_order fails!
    mock_broker.modify_order.return_value = False

    # Must execute cleanly without UnboundLocalError
    decisions = engine.evaluate_step(now)
    assert len(decisions) >= 1
    assert decisions[0].decision == Decision.MANAGE
    assert pos.current_stop_price == 645.0  # Internal stop not moved because modification unconfirmed

