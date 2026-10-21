"""Regression tests for conservative APEX-3 backtest accounting and fills."""

from datetime import time

import backtest_3months as backtest
from apex3.config import CostsConfig
from apex3.models import Side
import pandas as pd
import pytest


def _call_backtest_contract(name, *args, **kwargs):
    function = getattr(backtest, name, None)
    assert callable(function), f"backtest_3months.{name} must implement this behavior"
    return function(*args, **kwargs)


def test_friction_ratio_uses_gross_winning_trades_only():
    ratio = _call_backtest_contract("friction_ratio_pct", 1_000.0, 300.0)

    assert ratio == 30.0


def test_same_bar_stop_and_target_resolves_to_stop_for_long():
    exit_info = _call_backtest_contract(
        "resolve_bar_exit",
        Side.BUY,
        open_price=100.0,
        high=111.0,
        low=89.0,
        stop_price=90.0,
        target_price=110.0,
    )

    assert exit_info == (90.0, "STOP_LOSS")


def test_same_bar_stop_and_target_resolves_to_stop_for_short():
    exit_info = _call_backtest_contract(
        "resolve_bar_exit",
        Side.SELL,
        open_price=100.0,
        high=111.0,
        low=89.0,
        stop_price=110.0,
        target_price=90.0,
    )

    assert exit_info == (110.0, "STOP_LOSS")


def test_eod_candle_resolves_stop_before_target_before_flattening():
    exit_event, next_stop, is_applied = _call_backtest_contract(
        "resolve_eod_bar_exit",
        Side.BUY,
        open_price=100.0,
        high=111.0,
        low=89.0,
        close=105.0,
        stop_price=90.0,
        target_price=110.0,
        entry_price=100.0,
        risk_per_share=10.0,
        bar_start_time=time(14, 50),
        bar_close_time=time(14, 55),
        eod_reason="EOD_FLATTEN_LAST_AVAILABLE",
    )

    assert exit_event == (90.0, "STOP_LOSS")
    assert next_stop == 90.0
    assert is_applied is False


def test_execution_spread_is_applied_adversely_and_rounded_to_tick():
    buy_price = _call_backtest_contract(
        "apply_adverse_spread", 1_000.0, Side.BUY, 4.0, 0.05
    )
    sell_price = _call_backtest_contract(
        "apply_adverse_spread", 1_000.0, Side.SELL, 4.0, 0.05
    )

    assert buy_price == 1_000.20
    assert sell_price == 999.80


def test_backtest_size_includes_stop_fees_under_risk_cap():
    quantity = _call_backtest_contract(
        "size_backtest_quantity",
        entry_price=100.0,
        stop_price=99.0,
        side=Side.BUY,
        slot_budget=28_500.0,
        risk_cap=120.0,
        target_r=2.0,
        costs=CostsConfig(),
        spread_bps=0.0,
    )

    assert quantity == 108


def test_open_position_daily_mark_includes_round_trip_fees():
    net_pnl = _call_backtest_contract(
        "estimate_open_position_net_pnl",
        Side.BUY,
        entry_price=100.0,
        mark_price=101.0,
        quantity=1,
        costs=CostsConfig(),
        spread_bps=0.0,
    )

    assert net_pnl == 0.90


def test_entry_gap_beyond_slippage_limit_is_rejected():
    within_limit = _call_backtest_contract(
        "is_entry_fill_acceptable", 100.0, 100.15, 0.15
    )
    outside_limit = _call_backtest_contract(
        "is_entry_fill_acceptable", 100.0, 100.16, 0.15
    )

    assert within_limit is True
    assert outside_limit is False


def test_entry_at_the_cutoff_is_not_accepted():
    allowed_before_cutoff = _call_backtest_contract(
        "is_before_entry_cutoff", time(11, 55), time(12, 0)
    )
    at_cutoff = _call_backtest_contract(
        "is_before_entry_cutoff", time(12, 0), time(12, 0)
    )

    assert allowed_before_cutoff is True
    assert at_cutoff is False


def test_intrabar_fee_lock_arms_for_next_bar_when_lock_level_is_not_touched():
    exit_event, next_stop, is_applied = _call_backtest_contract(
        "resolve_position_bar_exit",
        Side.BUY,
        open_price=100.40,
        high=101.70,
        low=100.20,
        close=100.50,
        stop_price=99.00,
        target_price=102.00,
        entry_price=100.00,
        risk_per_share=1.00,
        bar_close_time=time(11, 0),
    )

    assert exit_event is None
    assert next_stop == 100.15
    assert is_applied is True


def test_intrabar_fee_lock_stop_wins_when_trigger_and_stop_share_one_bar():
    exit_event, next_stop, is_applied = _call_backtest_contract(
        "resolve_position_bar_exit",
        Side.BUY,
        open_price=100.40,
        high=101.70,
        low=100.10,
        close=100.50,
        stop_price=99.00,
        target_price=102.00,
        entry_price=100.00,
        risk_per_share=1.00,
        bar_close_time=time(11, 0),
    )

    assert exit_event == (100.15, "FEE_LOCK_STOP")
    assert next_stop == 100.15
    assert is_applied is True


def test_armed_fee_lock_exits_on_a_later_bar():
    exit_event, next_stop, is_applied = _call_backtest_contract(
        "resolve_position_bar_exit",
        Side.BUY,
        open_price=100.20,
        high=100.30,
        low=100.10,
        close=100.18,
        stop_price=100.15,
        target_price=102.00,
        entry_price=100.00,
        risk_per_share=1.00,
        bar_close_time=time(11, 5),
        fee_lock_applied=True,
    )

    assert exit_event == (100.15, "FEE_LOCK_STOP")
    assert next_stop == 100.15
    assert is_applied is True


def test_late_session_fee_lock_arms_from_completed_close():
    exit_event, next_stop, is_applied = _call_backtest_contract(
        "resolve_position_bar_exit",
        Side.BUY,
        open_price=100.60,
        high=101.30,
        low=100.50,
        close=101.20,
        stop_price=99.00,
        target_price=102.00,
        entry_price=100.00,
        risk_per_share=1.00,
        bar_close_time=time(14, 15),
    )

    assert exit_event is None
    assert next_stop == 100.15
    assert is_applied is True


def test_late_session_fee_lock_observes_intrabar_profit_after_1415():
    exit_event, next_stop, is_applied = _call_backtest_contract(
        "resolve_position_bar_exit",
        Side.BUY,
        open_price=100.20,
        high=101.20,
        low=100.20,
        close=100.30,
        stop_price=99.00,
        target_price=102.00,
        entry_price=100.00,
        risk_per_share=1.00,
        bar_close_time=time(14, 20),
        bar_start_time=time(14, 15),
    )

    assert exit_event is None
    assert next_stop == 100.15
    assert is_applied is True


def test_slot_two_uses_current_open_r_and_stop_not_stale_close_r():
    still_unlocked = _call_backtest_contract(
        "slot_two_unlocked_at_open",
        Side.BUY,
        entry_price=100.0,
        open_price=100.4,
        stop_price=99.0,
        risk_per_share=1.0,
    )
    gap_stopped = _call_backtest_contract(
        "slot_two_unlocked_at_open",
        Side.BUY,
        entry_price=100.0,
        open_price=98.9,
        stop_price=99.0,
        risk_per_share=1.0,
    )

    assert still_unlocked is False
    assert gap_stopped is False


def test_daily_gate_uses_realized_plus_current_open_mark():
    hit_limit = _call_backtest_contract(
        "daily_loss_limit_reached", -180.0, -60.0, -240.0
    )
    above_limit = _call_backtest_contract(
        "daily_loss_limit_reached", -180.0, -59.0, -240.0
    )

    assert hit_limit is True
    assert above_limit is False


def test_adr_uses_only_completed_prior_sessions():
    candles = pd.DataFrame(
        [
            ("2026-01-01 09:15", 100.0, 110.0, 100.0, 105.0),
            ("2026-01-02 09:15", 105.0, 120.0, 110.0, 115.0),
            ("2026-01-03 09:15", 115.0, 130.0, 120.0, 125.0),
            ("2026-01-04 09:15", 125.0, 300.0, 1.0, 200.0),
        ],
        columns=["Datetime", "Open", "High", "Low", "Close"],
    )
    candles["Datetime"] = pd.to_datetime(candles["Datetime"])

    adr_pct = _call_backtest_contract(
        "average_daily_range_pct_before_session",
        candles,
        pd.Timestamp("2026-01-04").date(),
        lookback=2,
    )

    assert adr_pct == pytest.approx(((10.0 / 105.0) + (10.0 / 115.0)) / 2.0)
