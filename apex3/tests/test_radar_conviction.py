"""Tests for Two-Tier Radar, Gate 9 (Stop-Width & ADR Exhaustion), and Conviction Ranker."""
import pytest
from apex3.config import load_config
from apex3.strategy.regime import BenchmarkSnapshot, MarketRegime, evaluate_market_regime
from apex3.strategy.trail import (
    StopTargetLevels,
    compute_initial_stop_and_target,
    evaluate_fee_protected_stop,
    floor_to_tick,
    ceil_to_tick,
    round_to_tick,
)
from apex3.strategy.signal import StockCandidateBar, TradeSignal, evaluate_candidate_signal
from apex3.strategy.sizer import (
    calculate_position_size,
    calculate_zerodha_intraday_charges,
    fetch_zerodha_live_capital,
)
from apex3.strategy.engine import build_active_radar, select_best_entries_for_bar


@pytest.fixture
def cfg():
    return load_config().raw_dict


def test_build_active_radar_merges_all_three_tiers():
    mock_cfg = {
        "radar": {
            "core_watchlist": ["RELIANCE", "ICICIBANK", "INFY"],
            "enable_extended_radar": True,
            "extended_radar": ["BEL", "HAL", "TRENT"],
            "custom_user_radar": ["JIOFIN", "ZOMATO"],
        }
    }
    radar = build_active_radar(mock_cfg)
    # Check Core Tier 1
    assert "RELIANCE" in radar
    assert "ICICIBANK" in radar
    assert "INFY" in radar
    # Check Extended Tier 2
    assert "BEL" in radar
    assert "HAL" in radar
    assert "TRENT" in radar
    # Check Custom Tier 3
    assert "JIOFIN" in radar
    assert "ZOMATO" in radar
    # Ensure no duplicates
    assert len(radar) == len(set(radar))


def test_benchmark_snapshot_regime_bullish(cfg):
    snapshot = BenchmarkSnapshot(
        close=25000.0,
        ema20=24900.0,
        prev_ema20=24890.0,
        session_vwap=24950.0,
        adx14=25.0,
        prev_adx14=24.0,
    )
    regime = evaluate_market_regime(snapshot, cfg)
    assert regime == MarketRegime.BULLISH


def test_benchmark_snapshot_regime_sideways_chop_neutral(cfg):
    # Falling ADX should yield NEUTRAL
    snapshot = BenchmarkSnapshot(
        close=25000.0,
        ema20=24900.0,
        prev_ema20=24890.0,
        session_vwap=24950.0,
        adx14=23.0,
        prev_adx14=24.5,  # Falling ADX
    )
    regime = evaluate_market_regime(snapshot, cfg)
    assert regime == MarketRegime.NEUTRAL


def test_compute_initial_stop_and_target_long(cfg):
    levels = compute_initial_stop_and_target(
        side="LONG",
        entry_price=1000.0,
        swing_low_5=996.0,
        swing_high_5=1005.0,
        atr14=2.0,
        cfg=cfg,
    )
    # 996.0 - 1.5 * 2.0 = 993.0
    assert levels.initial_stop == 993.0
    assert levels.risk_per_share == 7.0
    # 1000.0 + 2.0 * 7.0 = 1014.0
    assert levels.target_price == 1014.0
    assert levels.risk_pct == 0.0070  # 0.70% (within 0.45% - 0.85% Gate 9B sweet spot)


def test_evaluate_fee_protected_stop(cfg):
    levels = compute_initial_stop_and_target(
        side="LONG",
        entry_price=1000.0,
        swing_low_5=996.0,
        swing_high_5=1005.0,
        atr14=2.0,
        cfg=cfg,
    )
    # Target is +2.0R (1014.0). Test +1.65R (1000 + 1.65 * 7 = 1011.55)
    new_stop = evaluate_fee_protected_stop(
        levels=levels,
        current_price=1012.0,
        current_time_str="11:30",
        cfg=cfg,
    )
    # Should lock breakeven + 0.15% (1000 * 1.0015 = 1001.50)
    assert new_stop is not None
    assert new_stop == 1001.50
    assert levels.fee_lock_applied is True


def test_gate_9_stop_width_filter(cfg):
    # Candidate with stop too tight (R < 0.45%)
    bar_tight = StockCandidateBar(
        symbol="TIGHT",
        timestamp_hhmm="10:30",
        close=1000.0,
        or_high=998.0,
        or_low=990.0,
        session_vwap=995.0,
        stock_return_20=0.015,
        nifty_return_20=0.005,
        bar_volume=25000,
        avg_volume_20=10000,
        bid_ask_spread_bps=2.0,
        upper_circuit=1100.0,
        lower_circuit=900.0,
        swing_low_5=998.5,  # Stop = 998.5 - 1.5*1.0 = 997.0 -> R = 3.0 / 1000 = 0.30% (< 0.45%)
        swing_high_5=1002.0,
        atr14=1.0,
        session_high_so_far=1002.0,
        session_low_so_far=990.0,
        adr14_pct=0.025,
    )
    sig = evaluate_candidate_signal(bar_tight, MarketRegime.BULLISH, 0, cfg)
    assert sig is None  # Fails Gate 9B


def test_gate_9_adr_exhaustion_filter(cfg):
    # Stock already moved 80% of ADR (1080 - 1000 = 80 / 1000 = 8%, but ADR is 6%)
    bar_exhausted = StockCandidateBar(
        symbol="EXHAUSTED",
        timestamp_hhmm="11:30",
        close=1075.0,
        or_high=1020.0,
        or_low=995.0,
        session_vwap=1030.0,
        stock_return_20=0.04,
        nifty_return_20=0.005,
        bar_volume=30000,
        avg_volume_20=10000,
        bid_ask_spread_bps=2.0,
        upper_circuit=1200.0,
        lower_circuit=900.0,
        swing_low_5=1070.0,
        swing_high_5=1080.0,
        atr14=5.0,
        session_high_so_far=1080.0,
        session_low_so_far=1000.0,  # Range = 80 / 1000 = 8.0%
        adr14_pct=0.06,  # 8.0% / 6.0% = 1.33 > 0.60
    )
    sig = evaluate_candidate_signal(bar_exhausted, MarketRegime.BULLISH, 0, cfg)
    assert sig is None  # Fails Gate 9C


def test_conviction_scoring_and_sizing(cfg):
    # Valid high conviction candidate
    bar = StockCandidateBar(
        symbol="RELIANCE",
        timestamp_hhmm="10:45",
        close=1200.0,
        or_high=1195.0,
        or_low=1180.0,
        session_vwap=1190.0,
        stock_return_20=0.015,
        nifty_return_20=0.005,
        bar_volume=50000,
        avg_volume_20=20000,
        bid_ask_spread_bps=2.0,
        upper_circuit=1320.0,
        lower_circuit=1080.0,
        swing_low_5=1198.8,
        swing_high_5=1202.0,
        atr14=4.0,  # 1198.8 - 6.0 = 1192.8 -> R = 7.2 / 1200 = 0.60%
        session_high_so_far=1202.0,
        session_low_so_far=1192.0,  # Range = 10 / 1192 = 0.84% -> 0.84% / 2.5% = 33.6% ADR (Passes Gate 9C)
        adr14_pct=0.025,
    )
    sig = evaluate_candidate_signal(bar, MarketRegime.BULLISH, 0, cfg)
    assert sig is not None
    assert sig.conviction_score > 0
    assert sig.volume_surge_ratio == 2.5

    # Size calculation for 2 slots (slot budget = ~28,500, risk budget = ~120)
    sizing = calculate_position_size(
        entry_price=sig.entry_price,
        risk_per_share=sig.levels.risk_per_share,
        target_price=sig.levels.target_price,
        cfg=cfg,
    )
    assert sizing is not None
    assert sizing.quantity > 0
    assert sizing.notional_exposure <= 28500.0 + 1200.0
    assert sizing.planned_risk_inr <= 125.0
    # Fees should be ~25-35 Rs
    assert 20.0 <= sizing.estimated_roundtrip_charges_inr <= 45.0


def test_select_best_entries_for_bar_ranks_and_picks_top1(cfg):
    snapshot = BenchmarkSnapshot(
        close=25000.0,
        ema20=24900.0,
        prev_ema20=24890.0,
        session_vwap=24950.0,
        adx14=25.0,
        prev_adx14=24.0,
    )
    # Both RELIANCE and INFY are in core_watchlist
    bar1 = StockCandidateBar(
        symbol="RELIANCE",
        timestamp_hhmm="10:15",
        close=1200.0,
        or_high=1195.0,
        or_low=1180.0,
        session_vwap=1190.0,
        stock_return_20=0.015,
        nifty_return_20=0.005,
        bar_volume=45000,
        avg_volume_20=20000,  # 2.25x
        bid_ask_spread_bps=2.0,
        upper_circuit=1320.0,
        lower_circuit=1080.0,
        swing_low_5=1198.8,
        swing_high_5=1202.0,
        atr14=4.0,
        session_high_so_far=1202.0,
        session_low_so_far=1192.0,  # 33.6% ADR consumed
        adr14_pct=0.025,
    )
    bar2 = StockCandidateBar(
        symbol="INFY",
        timestamp_hhmm="10:15",
        close=1800.0,
        or_high=1790.0,
        or_low=1770.0,
        session_vwap=1780.0,
        stock_return_20=0.025,  # Higher RS!
        nifty_return_20=0.005,
        bar_volume=70000,
        avg_volume_20=20000,  # 3.5x volume!
        bid_ask_spread_bps=1.5,  # Tighter spread!
        upper_circuit=2000.0,
        lower_circuit=1600.0,
        swing_low_5=1795.0,
        swing_high_5=1805.0,
        atr14=5.0,  # Stop = 1795 - 7.5 = 1787.5 -> R = 12.5 -> R% = 0.69%
        session_high_so_far=1805.0,
        session_low_so_far=1785.0,  # Range = 20 / 1785 = 1.12% -> 1.12% / 2.5% = 44.8% ADR consumed
        adr14_pct=0.025,
    )

    test_cfg = dict(cfg)
    test_cfg["radar"] = {"core_watchlist": ["RELIANCE", "INFY"], "enable_extended_radar": False, "custom_user_radar": []}
    candidate_bars = {"RELIANCE": bar1, "INFY": bar2}
    open_positions = {}
    orders = select_best_entries_for_bar(
        benchmark_bar=snapshot,
        candidate_bars=candidate_bars,
        open_positions=open_positions,
        realized_daily_pnl_net=0.0,
        cfg=test_cfg,
    )

    # Should select exactly 1 trade (max_new_entries_per_bar: 1)
    assert len(orders) == 1
    best_sig, best_sizing = orders[0]
    # INFY has higher RS (0.02 vs 0.01), higher vol (3.5 vs 2.25), and tighter spread -> INFY wins!
    assert best_sig.symbol == "INFY"


def test_strategy_engine_watchlist_resolution(tmp_path):
    from apex3.broker.paper import PaperBroker
    from apex3.journal.sqlite_journal import Journal
    from apex3.strategy.engine import StrategyEngine

    cfg = load_config()
    journal = Journal(db_path=str(tmp_path / "test_wl.db"))
    broker = PaperBroker(initial_margin=100000.0)
    engine = StrategyEngine(config=cfg, broker=broker, journal=journal)

    watchlist = engine.get_active_watchlist()
    assert "HINDALCO" in watchlist
    assert "JSWSTEEL" in watchlist
    assert "RELIANCE" in watchlist
    assert "SUNPHARMA" in watchlist
    assert len(watchlist) == 10


def test_strategy_engine_slot2_lock(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from apex3.broker.paper import PaperBroker
    from apex3.journal.sqlite_journal import Journal
    from apex3.models import Decision, Quote, Side
    from apex3.reconciliation.state_machine import PositionPhase
    from apex3.strategy.engine import StrategyEngine

    IST = ZoneInfo("Asia/Kolkata")
    cfg = load_config()
    cfg.raw_dict.setdefault("risk_management", {})["second_slot_requires_slot1_profit_r"] = 0.50
    journal = Journal(db_path=str(tmp_path / "test_engine_lock.db"))
    broker = PaperBroker(initial_margin=100000.0)
    engine = StrategyEngine(config=cfg, broker=broker, journal=journal)

    # 1. Active position with unrealized_r < 0.50 (entry=1000, stop=990, r=10, last_price=1002 -> 0.2R)
    pos = engine.state_machine.get_or_create_position("RELIANCE")
    pos.phase = PositionPhase.OPEN_PROTECTED
    pos.side = Side.BUY
    pos.quantity = 10
    pos.average_entry = 1000.0
    pos.initial_stop_price = 990.0
    pos.current_stop_price = 990.0
    pos.target_price = 1020.0
    pos.r_value = 10.0
    pos.strategy_tag = "APEX3_TEST"

    broker._fixture_quotes["RELIANCE"] = Quote(
        symbol="RELIANCE",
        exchange="NSE",
        last_price=1002.0,
        bid=1001.95,
        ask=1002.05,
        volume=1000,
        timestamp=datetime(2026, 10, 21, 10, 0, tzinfo=IST),
        is_fresh=True,
    )

    t = datetime(2026, 10, 21, 10, 0, tzinfo=IST)
    decisions = engine.evaluate_step(t)
    wait_dec = next((d for d in decisions if "SECOND_SLOT_LOCKED_UNTIL_0.5R" in d.reason_codes), None)
    assert wait_dec is not None
    assert wait_dec.decision == Decision.WAIT

    # 2. When req_r is 0.0 (independent margin allocation), slot 2 lock is bypassed
    cfg.raw_dict["risk_management"]["second_slot_requires_slot1_profit_r"] = 0.0
    decisions_unlocked = engine.evaluate_step(t)
    unlocked_lock_dec = next((d for d in decisions_unlocked if "SECOND_SLOT_LOCKED_UNTIL_0.5R" in d.reason_codes), None)
    assert unlocked_lock_dec is None


def test_strategy_engine_fee_protected_breakeven(tmp_path):
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from apex3.broker.paper import PaperBroker
    from apex3.journal.sqlite_journal import Journal
    from apex3.models import Decision, Quote, Side
    from apex3.reconciliation.state_machine import PositionPhase
    from apex3.strategy.engine import StrategyEngine

    IST = ZoneInfo("Asia/Kolkata")
    cfg = load_config()
    journal = Journal(db_path=str(tmp_path / "test_fee_lock.db"))
    broker = PaperBroker(initial_margin=100000.0)
    engine = StrategyEngine(config=cfg, broker=broker, journal=journal)

    # Open position: entry=1000, stop=990, r=10. Price=1017 (open_r = 1.70R >= 1.65R)
    pos = engine.state_machine.get_or_create_position("RELIANCE")
    pos.phase = PositionPhase.OPEN_PROTECTED
    pos.side = Side.BUY
    pos.quantity = 10
    pos.average_entry = 1000.0
    pos.initial_stop_price = 990.0
    pos.current_stop_price = 990.0
    pos.target_price = 1020.0
    pos.r_value = 10.0
    pos.strategy_tag = "APEX3_TEST"
    pos.fee_lock_applied = False

    broker._fixture_quotes["RELIANCE"] = Quote(
        symbol="RELIANCE",
        exchange="NSE",
        last_price=1017.0,
        bid=1016.95,
        ask=1017.05,
        volume=1000,
        timestamp=datetime(2026, 10, 21, 11, 0, tzinfo=IST),
        is_fresh=True,
    )

    t = datetime(2026, 10, 21, 11, 0, tzinfo=IST)
    decisions = engine.evaluate_step(t)
    manage_dec = next((d for d in decisions if d.symbol == "RELIANCE"), None)
    assert manage_dec is not None
    assert manage_dec.decision == Decision.MANAGE
    assert "FEE_PROTECTED_BREAKEVEN_STEP" in manage_dec.reason_codes
    assert pos.fee_lock_applied is True
    # 1000.0 * 1.0015 = 1001.50
    assert pos.current_stop_price == 1001.50
