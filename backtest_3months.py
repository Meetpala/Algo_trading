"""Conservative APEX-3 historical audit engine.

Completed-bar signals fill on the next aligned 5-minute bar. The replay uses
fixed 2.0R targets, structural stops, a one-time fee lock, and itemized
Zerodha/statutory charges.
"""
from __future__ import annotations

import os
import math
import json
from datetime import datetime, time, timedelta
from typing import Optional
import pandas as pd

from apex3.config import load_config, CostsConfig
from apex3.models import Candle, Quote, InstrumentInfo, OpeningRange, Side, Decision
from apex3.strategy.engine import build_active_radar
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import StockCandidateBar, evaluate_candidate_signal
from apex3.strategy.trail import calculate_target_price, compute_initial_stop_and_target
from apex3.indicators.atr import compute_atr
from apex3.indicators.vwap import compute_session_vwap
from backtest_engine import calculate_charges


def load_preparsed_data(symbols: list[str]) -> dict:
    """Pre-parse all CSV data into indexed datetime structures once."""
    data = {}
    for s in symbols:
        path = os.path.join('scratch_data', f'{s}.csv')
        df = pd.read_csv(path)
        df['Datetime'] = pd.to_datetime(df['Datetime'])
        df = df.sort_values('Datetime').reset_index(drop=True)

        candles = []
        for _, row in df.iterrows():
            candles.append(Candle(
                timestamp=row['Datetime'].to_pydatetime(),
                open=float(row['Open']),
                high=float(row['High']),
                low=float(row['Low']),
                close=float(row['Close']),
                volume=int(row['Volume']),
                is_complete=True,
            ))
        data[s] = {
            'df': df,
            'candles': candles,
            'dt_to_idx': {c.timestamp: i for i, c in enumerate(candles)},
        }
    return data


def friction_ratio_pct(gross_profit: float, total_charges: float) -> float:
    """Return statutory charges as a percentage of positive gross profits."""
    if gross_profit <= 0:
        return float("inf") if total_charges > 0 else 0.0
    return round((total_charges / gross_profit) * 100.0, 2)


def resolve_bar_exit(
    side: Side,
    *,
    open_price: float,
    high: float,
    low: float,
    stop_price: float,
    target_price: float,
) -> tuple[float, str] | None:
    """Resolve OHLC bars conservatively, treating a same-bar stop as first."""
    if side == Side.BUY:
        if low <= stop_price:
            return min(stop_price, open_price), "STOP_LOSS"
        if target_price > 0 and high >= target_price:
            return target_price, "TARGET_HIT"
    else:
        if high >= stop_price:
            return max(stop_price, open_price), "STOP_LOSS"
        if target_price > 0 and low <= target_price:
            return target_price, "TARGET_HIT"
    return None


def fee_lock_stop_for_bar(
    side: Side,
    *,
    entry_price: float,
    risk_per_share: float,
    current_stop: float,
    high: float,
    low: float,
    close: float,
    bar_close_time: time,
    bar_start_time: time | None = None,
    already_applied: bool = False,
    tick_size: float = 0.05,
) -> tuple[float, bool] | None:
    """Return a fee-lock stop and whether its trigger was intrabar."""
    if already_applied or entry_price <= 0 or risk_per_share <= 0 or tick_size <= 0:
        return None

    if side == Side.BUY:
        favorable_r = (high - entry_price) / risk_per_share
        close_r = (close - entry_price) / risk_per_share
        late_trigger = bar_close_time >= time(14, 15) and close_r >= 1.15
        late_intrabar_trigger = (
            bar_start_time is not None
            and bar_start_time >= time(14, 15)
            and favorable_r >= 1.15
        )
        candidate = math.ceil(
            entry_price * 1.0015 / tick_size - 1e-10
        ) * tick_size
        improves_stop = candidate > current_stop
    else:
        favorable_r = (entry_price - low) / risk_per_share
        close_r = (entry_price - close) / risk_per_share
        late_trigger = bar_close_time >= time(14, 15) and close_r >= 1.15
        late_intrabar_trigger = (
            bar_start_time is not None
            and bar_start_time >= time(14, 15)
            and favorable_r >= 1.15
        )
        candidate = math.floor(
            entry_price * 0.9985 / tick_size + 1e-10
        ) * tick_size
        improves_stop = candidate < current_stop

    intrabar_trigger = favorable_r >= 1.65 or late_intrabar_trigger
    if not (intrabar_trigger or late_trigger) or not improves_stop:
        return None
    return round(candidate, 2), intrabar_trigger


def resolve_position_bar_exit(
    side: Side,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    stop_price: float,
    target_price: float,
    entry_price: float,
    risk_per_share: float,
    bar_close_time: time,
    bar_start_time: time | None = None,
    fee_lock_applied: bool = False,
) -> tuple[tuple[float, str] | None, float, bool]:
    """Resolve exits conservatively, arming newly observed fee stops for next bar."""
    existing_stop_exit = resolve_bar_exit(
        side,
        open_price=open_price,
        high=high,
        low=low,
        stop_price=stop_price,
        target_price=0.0,
    )
    if existing_stop_exit is not None:
        exit_price, _reason = existing_stop_exit
        reason = "FEE_LOCK_STOP" if fee_lock_applied else "STOP_LOSS"
        return (exit_price, reason), stop_price, fee_lock_applied

    lock = fee_lock_stop_for_bar(
        side,
        entry_price=entry_price,
        risk_per_share=risk_per_share,
        current_stop=stop_price,
        high=high,
        low=low,
        close=close,
        bar_close_time=bar_close_time,
        bar_start_time=bar_start_time,
        already_applied=fee_lock_applied,
    )
    if lock is not None:
        candidate_stop, intrabar_trigger = lock
        if intrabar_trigger:
            lock_stop_hit = (
                low <= candidate_stop
                if side == Side.BUY
                else high >= candidate_stop
            )
            if lock_stop_hit:
                fill = (
                    min(candidate_stop, open_price)
                    if side == Side.BUY
                    else max(candidate_stop, open_price)
                )
                return (fill, "FEE_LOCK_STOP"), candidate_stop, True

            target_event = resolve_bar_exit(
                side,
                open_price=open_price,
                high=high,
                low=low,
                stop_price=stop_price,
                target_price=target_price,
            )
            if target_event is not None:
                return target_event, candidate_stop, True
            return None, candidate_stop, True

    exit_event = resolve_bar_exit(
        side,
        open_price=open_price,
        high=high,
        low=low,
        stop_price=stop_price,
        target_price=target_price,
    )
    if exit_event is not None:
        exit_price, reason = exit_event
        if reason == "STOP_LOSS" and fee_lock_applied:
            reason = "FEE_LOCK_STOP"
        return (exit_price, reason), stop_price, fee_lock_applied
    if lock is None:
        return None, stop_price, fee_lock_applied

    candidate_stop, _intrabar_trigger = lock
    return None, candidate_stop, True


def resolve_eod_bar_exit(
    side: Side,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
    stop_price: float,
    target_price: float,
    entry_price: float,
    risk_per_share: float,
    bar_start_time: time,
    bar_close_time: time,
    fee_lock_applied: bool = False,
    eod_reason: str = "EOD_FLATTEN_LAST_AVAILABLE",
) -> tuple[tuple[float, str], float, bool]:
    """Resolve protective exits on the liquidation candle before close."""
    exit_event, next_stop, next_fee_lock_applied = resolve_position_bar_exit(
        side,
        open_price=open_price,
        high=high,
        low=low,
        close=close,
        stop_price=stop_price,
        target_price=target_price,
        entry_price=entry_price,
        risk_per_share=risk_per_share,
        bar_start_time=bar_start_time,
        bar_close_time=bar_close_time,
        fee_lock_applied=fee_lock_applied,
    )
    if exit_event is None:
        exit_event = (close, eod_reason)
    return exit_event, next_stop, next_fee_lock_applied


def apply_adverse_spread(
    price: float,
    side: Side,
    spread_bps: float,
    tick_size: float = 0.05,
) -> float:
    """Apply half the assumed spread and round adversely."""
    if price <= 0 or tick_size <= 0 or spread_bps < 0:
        raise ValueError("price, tick size, and spread must be valid non-negative values")
    half_spread = price * (spread_bps / 20_000.0)
    raw_price = price + half_spread if side == Side.BUY else price - half_spread
    ticks = raw_price / tick_size
    rounded_ticks = math.ceil(ticks - 1e-10) if side == Side.BUY else math.floor(ticks + 1e-10)
    return round(rounded_ticks * tick_size, 2)


def size_backtest_quantity(
    *,
    entry_price: float,
    stop_price: float,
    side: Side,
    slot_budget: float,
    risk_cap: float,
    target_r: float = 2.0,
    costs,
    spread_bps: float = 4.0,
    tick_size: float = 0.05,
) -> int:
    """Size position so stop loss plus statutory fees stays inside rupee risk cap."""
    if entry_price <= 0 or slot_budget <= 0 or risk_cap <= 0:
        return 0

    exit_side = Side.SELL if side == Side.BUY else Side.BUY
    stop_fill = apply_adverse_spread(stop_price, exit_side, spread_bps, tick_size)
    risk_per_share = abs(entry_price - stop_fill)
    if risk_per_share <= 0:
        return 0

    quantity = min(
        math.floor(risk_cap / risk_per_share),
        math.floor(slot_budget / entry_price),
    )
    if quantity <= 0:
        return 0

    while quantity > 0:
        stop_buy = entry_price if side == Side.BUY else stop_fill
        stop_sell = stop_fill if side == Side.BUY else entry_price
        stop_charges = calculate_charges(stop_buy, stop_sell, quantity, costs)["total_charges"]
        net_stop_risk = (risk_per_share * quantity) + stop_charges
        if net_stop_risk <= risk_cap:
            return quantity
        quantity -= 1

    return 0


def estimate_open_position_net_pnl(
    side: Side,
    *,
    entry_price: float,
    mark_price: float,
    quantity: int,
    costs,
    spread_bps: float = 4.0,
    tick_size: float = 0.05,
) -> float:
    """Mark open trade after adverse spread and round-trip fees."""
    if quantity <= 0:
        return 0.0
    exit_side = Side.SELL if side == Side.BUY else Side.BUY
    exit_fill = apply_adverse_spread(mark_price, exit_side, spread_bps, tick_size)
    buy_price = entry_price if side == Side.BUY else exit_fill
    sell_price = exit_fill if side == Side.BUY else entry_price
    gross_pnl = (sell_price - buy_price) * quantity
    charge_total = calculate_charges(buy_price, sell_price, quantity, costs)["total_charges"]
    return round(gross_pnl - charge_total, 2)


def is_entry_fill_acceptable(
    signal_price: float,
    next_bar_open: float,
    max_slippage_pct: float,
) -> bool:
    """Reject next-bar entry if open moved beyond max slippage percentage."""
    if signal_price <= 0 or next_bar_open <= 0 or max_slippage_pct < 0:
        return False
    gap_pct = abs(next_bar_open - signal_price) / signal_price * 100.0
    return gap_pct <= max_slippage_pct + 1e-9


def is_before_entry_cutoff(fill_time: time, cutoff_time: time) -> bool:
    """Allow fills strictly before configured entry cutoff."""
    return fill_time < cutoff_time


def slot_two_unlocked_at_open(
    side: Side,
    *,
    entry_price: float,
    open_price: float,
    stop_price: float,
    risk_per_share: float,
    minimum_r: float = 0.50,
) -> bool:
    """Require existing position to remain above slot gate at open."""
    if entry_price <= 0 or open_price <= 0 or risk_per_share <= 0:
        return False
    if side == Side.BUY:
        gap_stop_hit = open_price <= stop_price
        open_r = (open_price - entry_price) / risk_per_share
    else:
        gap_stop_hit = open_price >= stop_price
        open_r = (entry_price - open_price) / risk_per_share
    return not gap_stop_hit and open_r >= minimum_r


def daily_loss_limit_reached(day_realized: float, open_mark_net: float, daily_stop: float) -> bool:
    """Check daily loss halt condition."""
    return day_realized + open_mark_net <= daily_stop


def average_daily_range_pct_before_session(
    candles_df: pd.DataFrame,
    session_date,
    lookback: int = 14,
) -> float:
    """Calculate ADR from prior completed sessions."""
    if lookback <= 0 or candles_df.empty:
        return 0.0
    prior = candles_df[candles_df["Datetime"].dt.date < session_date].copy()
    if prior.empty:
        return 0.0
    prior["session_date"] = prior["Datetime"].dt.date
    daily = prior.groupby("session_date", sort=True).agg(
        high=("High", "max"),
        low=("Low", "min"),
        close=("Close", "last"),
    )
    previous_close = daily["close"].shift(1)
    ranges = ((daily["high"] - daily["low"]) / previous_close).dropna()
    if len(ranges) < lookback:
        return 0.0
    return float(ranges.iloc[-lookback:].mean())


def run_fast_backtest(
    data: dict,
    watchlist_symbols: list[str],
    target_r: float = 2.0,
    use_fixed_target: bool = True,
    trail_enabled: bool = False,
    trail_activation_r: float = 1.0,
    initial_atr_mult: float = 1.5,
    entry_cutoff_time: time = None,
    max_slots: int = 2,
    starting_capital: float = 15000.0,
    session_dates: list = None,
    execution_spread_bps: float = 4.0,
) -> dict:
    """Replay completed-bar signals with conservative next-bar execution."""
    config = load_config()
    cutoff_time = entry_cutoff_time if entry_cutoff_time is not None else time(12, 30)

    nifty_candles = data['NSEI']['candles']
    nifty_df = data['NSEI']['df']
    dates = sorted(list(set(nifty_df['Datetime'].dt.date)))
    test_dates = sorted(set(session_dates)) if session_dates is not None else dates[-57:]

    current_capital = starting_capital
    leverage = 4.0
    safety_reserve_pct = 5.0
    max_positions = min(max_slots, 2)
    usable_margin = starting_capital * (1.0 - safety_reserve_pct / 100.0)
    slot_budget = (usable_margin * leverage) / max_positions
    risk_cap = starting_capital * 0.0080
    daily_stop = -1.0 * starting_capital * 0.0160

    trades = []
    daily_stats = []

    for session_date in test_dates:
        n_sess_mask = (nifty_df['Datetime'].dt.date == session_date)
        sess_indices = nifty_df[n_sess_mask].index.tolist()
        if len(sess_indices) < 6:
            continue

        opening_ranges = {}
        stock_sess_map = {}
        stock_sess_indices = {}
        for sym in watchlist_symbols:
            s_candles = data[sym]['candles']
            s_df = data[sym]['df']
            s_indices = s_df[s_df['Datetime'].dt.date == session_date].index.tolist()
            stock_sess_indices[sym] = s_indices
            stock_sess_map[sym] = {s_candles[idx].timestamp: idx for idx in s_indices}
            or_indices = [idx for idx in s_indices if s_candles[idx].timestamp.time() < time(9, 30)]
            if len(or_indices) >= 3:
                or_high = max(s_candles[i].high for i in or_indices)
                or_low = min(s_candles[i].low for i in or_indices)
                opening_ranges[sym] = OpeningRange(symbol=sym, high=or_high, low=or_low, is_complete=True, candle_count=len(or_indices))
            else:
                opening_ranges[sym] = OpeningRange(symbol=sym, high=0.0, low=0.0, is_complete=False)

        open_positions = {}
        pending_entry = None
        session_trades = []
        day_realized = 0.0
        daily_halted = False

        for step, n_idx in enumerate(sess_indices):
            c_bar = nifty_candles[n_idx]
            current_dt = c_bar.timestamp
            bar_time = current_dt.time()
            bar_close_dt = current_dt + timedelta(minutes=5)

            if day_realized <= daily_stop:
                daily_halted = True

            # 1. Fill pending entry at bar t+1 open
            if pending_entry is not None:
                pending = pending_entry
                pending_entry = None
                sym = pending['symbol']
                s_idx = stock_sess_map[sym].get(current_dt)

                can_fill = (
                    s_idx is not None
                    and not daily_halted
                    and current_dt.time() <= cutoff_time
                    and len(open_positions) < max_positions
                )

                risk_cfg = config.raw_dict.get("risk_management", {})
                req_r_slot2 = float(risk_cfg.get("second_slot_requires_slot1_profit_r", 0.0))
                if can_fill and len(open_positions) == 1 and req_r_slot2 > 0.0:
                    first = next(iter(open_positions.values()))
                    first_bar_idx = stock_sess_map[first['symbol']].get(current_dt)
                    if first_bar_idx is None:
                        can_fill = False
                    else:
                        first_open = data[first['symbol']]['candles'][first_bar_idx].open
                        if first['side'] == Side.BUY:
                            open_r = (first_open - first['entry_price']) / first['r_rupees']
                        else:
                            open_r = (first['entry_price'] - first_open) / first['r_rupees']
                        if open_r < req_r_slot2:
                            can_fill = False

                if can_fill:
                    s_bar = data[sym]['candles'][s_idx]
                    gap_pct = abs(s_bar.open - pending['signal_price']) / pending['signal_price']
                    if gap_pct <= 0.0015:
                        side = pending['side']
                        entry_price = round(s_bar.open + 0.05 if side == Side.BUY else s_bar.open - 0.05, 2)
                        levels = compute_initial_stop_and_target(
                            side=side,
                            entry_price=entry_price,
                            swing_low_5=pending['swing_low_5'],
                            swing_high_5=pending['swing_high_5'],
                            atr14=pending['atr'],
                            cfg=config.raw_dict,
                        )
                        stop_pct = levels.risk_pct
                        if 0.0055 <= stop_pct <= 0.0090:
                            target_price = calculate_target_price(
                                side=side,
                                entry_price=entry_price,
                                initial_stop=levels.initial_stop,
                                target_r=target_r,
                                tick_size=0.05,
                            )
                            r_dist = abs(entry_price - levels.initial_stop)
                            qty = min(
                                math.floor(risk_cap / r_dist),
                                math.floor(slot_budget / entry_price),
                            )
                            if qty > 0:
                                open_positions[sym] = {
                                    'symbol': sym,
                                    'side': side,
                                    'entry_price': entry_price,
                                    'signal_price': pending['signal_price'],
                                    'signal_time': pending['signal_time'],
                                    'initial_stop': levels.initial_stop,
                                    'current_stop': levels.initial_stop,
                                    'target_price': target_price,
                                    'quantity': qty,
                                    'entry_time': current_dt,
                                    'r_rupees': r_dist,
                                    'unrealized_r': 0.0,
                                    'atr': pending['atr'],
                                    'fee_lock_applied': False,
                                }

            # 2. Position Management & Exits
            closed_syms = []
            for sym, pos in open_positions.items():
                s_idx = stock_sess_map[sym].get(current_dt)
                if s_idx is None:
                    continue
                s_bar = data[sym]['candles'][s_idx]
                r_dist = pos['r_rupees']

                exit_price = None
                exit_reason = None

                if pos['side'] == Side.BUY:
                    stop_hit = s_bar.low <= pos['current_stop']
                    target_hit = pos['target_price'] > 0 and s_bar.high >= pos['target_price']
                    if stop_hit and target_hit:
                        exit_price = min(pos['current_stop'], s_bar.open)
                        exit_reason = 'FEE_LOCK_STOP' if pos['fee_lock_applied'] else 'STOP_LOSS'
                    elif stop_hit:
                        exit_price = min(pos['current_stop'], s_bar.open)
                        exit_reason = 'FEE_LOCK_STOP' if pos['fee_lock_applied'] else 'STOP_LOSS'
                    elif target_hit:
                        exit_price = pos['target_price']
                        exit_reason = 'TARGET_HIT'
                else:
                    stop_hit = s_bar.high >= pos['current_stop']
                    target_hit = pos['target_price'] > 0 and s_bar.low <= pos['target_price']
                    if stop_hit and target_hit:
                        exit_price = max(pos['current_stop'], s_bar.open)
                        exit_reason = 'FEE_LOCK_STOP' if pos['fee_lock_applied'] else 'STOP_LOSS'
                    elif stop_hit:
                        exit_price = max(pos['current_stop'], s_bar.open)
                        exit_reason = 'FEE_LOCK_STOP' if pos['fee_lock_applied'] else 'STOP_LOSS'
                    elif target_hit:
                        exit_price = pos['target_price']
                        exit_reason = 'TARGET_HIT'

                if exit_price is None and bar_time >= time(14, 55):
                    exit_price = s_bar.close
                    exit_reason = 'EOD_FLATTEN_1455'

                if exit_price is not None:
                    buy_p = pos['entry_price'] if pos['side'] == Side.BUY else exit_price
                    sell_p = exit_price if pos['side'] == Side.BUY else pos['entry_price']
                    qty = pos['quantity']
                    gross_pnl = (sell_p - buy_p) * qty
                    charges = calculate_charges(buy_p, sell_p, qty, config.costs)
                    net_pnl = round(gross_pnl - charges['total_charges'], 2)
                    trade_record = {
                        'date': session_date.strftime('%Y-%m-%d'),
                        'symbol': sym,
                        'side': pos['side'].value,
                        'qty': qty,
                        'entry_time': pos['entry_time'].strftime('%H:%M'),
                        'signal_time': pos['signal_time'].strftime('%H:%M'),
                        'signal_price': round(pos['signal_price'], 2),
                        'entry_price': round(pos['entry_price'], 2),
                        'exit_time': current_dt.strftime('%H:%M'),
                        'exit_price': round(exit_price, 2),
                        'exit_reason': exit_reason,
                        'gross_pnl': round(gross_pnl, 2),
                        'charges': charges,
                        'total_charges': charges['total_charges'],
                        'net_pnl': net_pnl,
                        'r_multiple': round(gross_pnl / (pos['r_rupees'] * qty), 2) if pos['r_rupees'] > 0 else 0.0,
                    }
                    trades.append(trade_record)
                    session_trades.append(trade_record)
                    closed_syms.append(sym)
                    day_realized += net_pnl
                    current_capital += net_pnl
                else:
                    if pos['side'] == Side.BUY:
                        unrealized_r = (s_bar.close - pos['entry_price']) / r_dist
                        favorable_r = (s_bar.high - pos['entry_price']) / r_dist
                    else:
                        unrealized_r = (pos['entry_price'] - s_bar.close) / r_dist
                        favorable_r = (pos['entry_price'] - s_bar.low) / r_dist
                    pos['unrealized_r'] = unrealized_r

                    if not pos['fee_lock_applied']:
                        should_lock = False
                        if favorable_r >= 1.65:
                            should_lock = True
                        elif bar_time >= time(14, 15) and unrealized_r >= 1.15:
                            should_lock = True
                        if should_lock:
                            pos['fee_lock_applied'] = True
                            if pos['side'] == Side.BUY:
                                cand_stop = round(math.ceil((pos['entry_price'] * 1.0015) / 0.05) * 0.05, 2)
                                if cand_stop > pos['current_stop']:
                                    pos['current_stop'] = cand_stop
                            else:
                                cand_stop = round(math.floor((pos['entry_price'] * 0.9985) / 0.05) * 0.05, 2)
                                if cand_stop < pos['current_stop']:
                                    pos['current_stop'] = cand_stop

            for cs in closed_syms:
                del open_positions[cs]

            # 3. Signal evaluation on completed bar
            next_n_idx = sess_indices[step + 1] if step + 1 < len(sess_indices) else None
            if (
                next_n_idx is not None
                and not daily_halted
                and len(open_positions) < max_positions
                and time(9, 35) <= bar_close_dt.time() <= cutoff_time
            ):
                risk_cfg = config.raw_dict.get("risk_management", {})
                req_r_slot2 = float(risk_cfg.get("second_slot_requires_slot1_profit_r", 0.0))
                if len(open_positions) == 1 and req_r_slot2 > 0.0:
                    first_p = next(iter(open_positions.values()))
                    if first_p.get('unrealized_r', 0.0) < req_r_slot2:
                        continue
                bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
                regime_eval = evaluate_market_regime(bench_slice, config.regime)
                if regime_eval.regime == MarketRegime.NEUTRAL:
                    continue
                cands = []
                next_fill_dt = nifty_candles[next_n_idx].timestamp
                for sym in watchlist_symbols:
                    if sym in open_positions:
                        continue
                    s_idx = stock_sess_map[sym].get(current_dt)
                    next_s_idx = stock_sess_map[sym].get(next_fill_dt)
                    if s_idx is None or next_s_idx is None:
                        continue
                    s_bar = data[sym]['candles'][s_idx]
                    today_indices = [idx for idx in stock_sess_indices[sym] if data[sym]['candles'][idx].timestamp <= current_dt]
                    today_candles = [data[sym]['candles'][idx] for idx in today_indices]
                    if len(today_candles) < 4:
                        continue
                    or_range = opening_ranges[sym]
                    if not or_range.is_complete:
                        continue
                    first_open = data[sym]['candles'][stock_sess_indices[sym][0]].open
                    history_start = max(0, s_idx - 60)
                    stock_history = data[sym]['candles'][history_start:s_idx + 1]
                    atr_series = compute_atr([c.high for c in stock_history], [c.low for c in stock_history], [c.close for c in stock_history], period=14)
                    if not atr_series: continue
                    atr_val = atr_series[-1]
                    if atr_val <= 0: continue

                    prior_volume_bars = data[sym]['candles'][max(0, s_idx - 20):s_idx]
                    if len(prior_volume_bars) < 20: continue
                    average_volume = sum(c.volume for c in prior_volume_bars) / len(prior_volume_bars)
                    if average_volume <= 0: continue

                    stock_closes = [c.close for c in stock_history[-20:]]
                    bench_closes = [c.close for c in bench_slice[-20:]]
                    if len(stock_closes) < 20 or len(bench_closes) < 20: continue
                    stock_return_20 = (stock_closes[-1] - stock_closes[0]) / stock_closes[0] * 100.0
                    nifty_return_20 = (bench_closes[-1] - bench_closes[0]) / bench_closes[0] * 100.0

                    lookback_bars = getattr(config.stops, 'swing_lookback_bars', 5)
                    swing_bars = stock_history[-lookback_bars:]
                    session_high = max(c.high for c in today_candles)
                    session_low = min(c.low for c in today_candles)
                    session_vwap = compute_session_vwap([c.high for c in today_candles], [c.low for c in today_candles], [c.close for c in today_candles], [c.volume for c in today_candles])
                    fill_time_text = next_fill_dt.strftime('%H:%M')

                    candidate_bar = StockCandidateBar(
                        symbol=sym,
                        timestamp_hhmm=fill_time_text,
                        close=s_bar.close,
                        or_high=or_range.high,
                        or_low=or_range.low,
                        session_vwap=session_vwap,
                        stock_return_20=stock_return_20,
                        nifty_return_20=nifty_return_20,
                        bar_volume=s_bar.volume,
                        avg_volume_20=average_volume,
                        bid_ask_spread_bps=4.0,
                        upper_circuit=first_open * 1.10,
                        lower_circuit=first_open * 0.90,
                        swing_low_5=min(c.low for c in swing_bars),
                        swing_high_5=max(c.high for c in swing_bars),
                        atr14=atr_val,
                        session_high_so_far=session_high,
                        session_low_so_far=session_low,
                        adr14_pct=0.02,
                        bar_high=s_bar.high,
                        bar_low=s_bar.low,
                    )
                    signal = evaluate_candidate_signal(
                        bar=candidate_bar,
                        regime=regime_eval.regime,
                        active_positions_count=len(open_positions),
                        cfg=config.raw_dict,
                    )
                    if signal is None:
                        continue
                    side = Side.BUY if signal.side == 'LONG' else Side.SELL
                    cands.append({
                        'symbol': sym,
                        'side': side,
                        'signal_price': s_bar.close,
                        'signal_time': bar_close_dt,
                        'fill_time': next_fill_dt,
                        'swing_low_5': candidate_bar.swing_low_5,
                        'swing_high_5': candidate_bar.swing_high_5,
                        'atr': atr_val,
                        'conviction': signal.conviction_score,
                    })
                if cands:
                    cands.sort(key=lambda x: x['conviction'], reverse=True)
                    pending_entry = cands[0]

        daily_stats.append({
            'date': session_date.strftime('%Y-%m-%d'),
            'trades_count': len(session_trades),
            'day_pnl': sum(t['net_pnl'] for t in session_trades),
            'closing_capital': round(current_capital, 2),
        })

    wins = [t for t in trades if t['net_pnl'] > 0]
    losses = [t for t in trades if t['net_pnl'] < 0]
    gross_win = sum(t['gross_pnl'] for t in trades if t['gross_pnl'] > 0)
    gross_loss = sum(abs(t['gross_pnl']) for t in trades if t['gross_pnl'] < 0)
    gross_pnl = sum(t['gross_pnl'] for t in trades)
    tot_charges = sum(t['charges']['total_charges'] for t in trades)
    net_pnl = round(gross_pnl - tot_charges, 2)
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 999.0

    charge_breakdown = {
        'brokerage': round(sum(t['charges'].get('brokerage', 0.0) for t in trades), 2),
        'stt': round(sum(t['charges'].get('stt', 0.0) for t in trades), 2),
        'exchange_txn': round(sum(t['charges'].get('exchange_txn', 0.0) for t in trades), 2),
        'sebi': round(sum(t['charges'].get('sebi', 0.0) for t in trades), 2),
        'gst': round(sum(t['charges'].get('gst', 0.0) for t in trades), 2),
        'stamp_duty': round(sum(t['charges'].get('stamp_duty', 0.0) for t in trades), 2),
    }

    friction_ratio_win = friction_ratio_pct(gross_win, tot_charges)
    friction_ratio_ng = (
        round((tot_charges / gross_pnl) * 100.0, 2)
        if gross_pnl > 0 else float("inf")
    )

    return {
        'total_trades': len(trades),
        'winning_trades': len(wins),
        'losing_trades': len(losses),
        'win_rate': round(win_rate, 2),
        'gross_pnl': round(gross_pnl, 2),
        'gross_profit': round(gross_win, 2),
        'gross_loss': round(gross_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'total_charges': round(tot_charges, 2),
        'charge_breakdown': charge_breakdown,
        'net_pnl': round(net_pnl, 2),
        'profit_pct': round((net_pnl / starting_capital) * 100.0, 2),
        'friction_ratio_pct': friction_ratio_win,
        'friction_ratio_winning_profit_pct': friction_ratio_win,
        'friction_ratio_net_gross_pct': friction_ratio_ng,
        'ending_capital': round(current_capital, 2),
        'trades': trades,
        'daily_stats': daily_stats,
    }


if __name__ == '__main__':
    config = load_config()
    watchlist = build_active_radar(config.raw_dict)
    all_symbols = ['NSEI'] + watchlist

    print(f"Pre-loading historical data into memory for {len(all_symbols)} symbols...")
    preloaded = load_preparsed_data(all_symbols)
    print("Data loaded successfully!")

    three_month_dates = sorted(set(preloaded['NSEI']['df']['Datetime'].dt.date))[-57:]
    print(f"\n--- Conservative Replay: next-bar fills, fixed 2R, exact charges ({len(three_month_dates)} sessions) ---")
    res_3m = run_fast_backtest(
        data=preloaded,
        watchlist_symbols=watchlist,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=2,
        session_dates=three_month_dates,
    )

    print("\n=================================================================")
    print("   APEX-3 CONSERVATIVE 3-MONTH AUDIT REPORT                      ")
    print("=================================================================")
    print(f"Starting Capital: Rs. 15,000.00")
    print(f"Active Radar Universe ({len(watchlist)} stocks): {', '.join(watchlist)}")
    print(f"Total Completed Trades: {res_3m['total_trades']}")
    print(f"Winning Trades: {res_3m['winning_trades']} | Losing Trades: {res_3m['losing_trades']}")
    print(f"Profit Probability (Win Rate): {res_3m['win_rate']:.2f}%")
    print(f"Profit Factor: {res_3m['profit_factor']:.2f}")
    print(f"Gross P&L: Rs. {res_3m['gross_pnl']:+,.2f}")
    print(f"Zerodha & Statutory Charges: Rs. {res_3m['total_charges']:,.2f}")
    print("Charge breakdown: " + " | ".join(
        f"{key} Rs. {value:.2f}" for key, value in res_3m['charge_breakdown'].items()
    ))
    print(f"Net Realized P&L: Rs. {res_3m['net_pnl']:+,.2f} ({res_3m['profit_pct']:+.2f}%)")
    friction_ratio = res_3m['friction_ratio_pct']
    print(f"Statutory Friction / Gross Winning Profit: {friction_ratio:.2f}% (limit: < 26.0%)")
    print(f"Statutory Friction / Net Gross P&L: {res_3m['friction_ratio_net_gross_pct']:.2f}%")
    print("=================================================================\n")

    df_all_trades = pd.DataFrame(res_3m['trades'])
    if not df_all_trades.empty:
        df_all_trades['date_dt'] = pd.to_datetime(df_all_trades['date'])

    def summarize_subset(sub_df, name):
        if sub_df.empty:
            return {'Period': name, 'Trades': 0, 'Win Rate': '0%', 'Gross (Rs)': 0, 'Charges (Rs)': 0, 'Net (Rs)': 0, 'ROI (%)': '0%'}
        w = len(sub_df[sub_df['net_pnl'] > 0])
        tot = len(sub_df)
        g = round(sub_df['gross_pnl'].sum(), 2)
        c = round(sub_df['total_charges'].sum(), 2)
        n = round(g - c, 2)
        return {
            'Period': name,
            'Trades': tot,
            'Win Rate': f"{round(w / tot * 100, 1)}%",
            'Gross (Rs)': g,
            'Charges (Rs)': c,
            'Net (Rs)': n,
            'ROI (%)': f"{round((n / 15000.0) * 100, 2)}%"
        }

    monthly_table = []
    if not df_all_trades.empty:
        for period, month_trades in df_all_trades.groupby(df_all_trades['date_dt'].dt.to_period('M')):
            monthly_table.append(summarize_subset(month_trades, str(period)))
    monthly_table.append(summarize_subset(df_all_trades, 'Full 57-Session Total'))
    print("MONTHLY BREAKDOWN TABLE:")
    print(pd.DataFrame(monthly_table).to_string(index=False))

    with open('three_month_conservative_audit.json', 'w') as f:
        json.dump({
            'base_3m': {k: v for k, v in res_3m.items() if k != 'trades'},
            'monthly_table': monthly_table,
            'trades_sample': res_3m['trades'][:20],
        }, f, indent=2)

    assert res_3m['net_pnl'] > 0.0, f"Net P&L Rs. {res_3m['net_pnl']} was not positive"
    assert res_3m['win_rate'] >= 50.0, f"Win Rate {res_3m['win_rate']}% < 50.0% threshold"
    assert res_3m['profit_factor'] >= 1.50, f"Profit Factor {res_3m['profit_factor']} < 1.50 threshold"
    print("\n[PASS] 3-Month Net Realized P&L, win-rate, and profit-factor thresholds verified")
