"""Conservative APEX-3 historical audit engine.

Completed-bar signals fill on the next aligned 5-minute bar. The replay uses
fixed 2.0R targets, structural stops, a one-time fee lock, and itemized
Zerodha/statutory charges.
"""
import os
import math
import json
from datetime import datetime, time, timedelta
import pandas as pd

from apex3.config import load_config
from apex3.models import Candle, OpeningRange, Side
from apex3.strategy.engine import build_active_radar
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import StockCandidateBar, evaluate_candidate_signal
from apex3.strategy.trail import calculate_target_price, compute_initial_stop_and_target
from apex3.indicators.atr import compute_atr
from apex3.indicators.vwap import compute_session_vwap
from backtest_engine import calculate_charges


def load_preparsed_data(symbols: list[str]):
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
    """Return a fee-lock stop and whether its +1.65R trigger was intrabar.

    The late-session rule is evaluated from completed closes and from intrabar
    prices in bars that start at or after 13:45. The caller handles any
    same-candle stop touch.
    """
    if already_applied or entry_price <= 0 or risk_per_share <= 0 or tick_size <= 0:
        return None

    if side == Side.BUY:
        favorable_r = (high - entry_price) / risk_per_share
        close_r = (close - entry_price) / risk_per_share
        late_trigger = bar_close_time >= time(13, 45) and close_r >= 0.75
        late_intrabar_trigger = (
            bar_start_time is not None
            and bar_start_time >= time(13, 45)
            and favorable_r >= 0.75
        )
        candidate = math.ceil(
            entry_price * 1.0015 / tick_size - 1e-10
        ) * tick_size
        improves_stop = candidate > current_stop
    else:
        favorable_r = (entry_price - low) / risk_per_share
        close_r = (entry_price - close) / risk_per_share
        late_trigger = bar_close_time >= time(13, 45) and close_r >= 0.75
        late_intrabar_trigger = (
            bar_start_time is not None
            and bar_start_time >= time(13, 45)
            and favorable_r >= 0.75
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
    """Resolve exits first, then arm a newly observed fee stop for the next bar.

    Existing stops beat same-bar targets. If an intrabar threshold and its new
    stop are both touched in one candle, assume the fee stop is reached first;
    otherwise the new stop becomes active on the next candle.
    """
    # First honor the stop that existed before this candle. A newly triggered
    # fee lock must not erase a stop that was already reachable at the open.
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
            # OHLC does not reveal whether the threshold or retracement came
            # first. If both are touched, resolve stop-first at the new lock.
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
    """Resolve all protective exits on the liquidation candle before its close."""
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
    """Apply half the assumed full spread and round the fill adversely."""
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
    target_r: float,
    costs,
    spread_bps: float = 4.0,
    tick_size: float = 0.05,
    max_fee_ratio_pct: float = 18.0,
) -> int:
    """Size a position so stop loss plus fees stays inside the rupee risk cap."""
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

    target_price = calculate_target_price(
        side=side,
        entry_price=entry_price,
        initial_stop=stop_price,
        target_r=target_r,
        tick_size=tick_size,
    )
    target_fill = apply_adverse_spread(target_price, exit_side, spread_bps, tick_size)

    while quantity > 0:
        stop_buy = entry_price if side == Side.BUY else stop_fill
        stop_sell = stop_fill if side == Side.BUY else entry_price
        stop_charges = calculate_charges(stop_buy, stop_sell, quantity, costs)["total_charges"]
        net_stop_risk = (risk_per_share * quantity) + stop_charges
        if net_stop_risk > risk_cap:
            quantity -= 1
            continue

        target_buy = entry_price if side == Side.BUY else target_fill
        target_sell = target_fill if side == Side.BUY else entry_price
        target_charges = calculate_charges(target_buy, target_sell, quantity, costs)["total_charges"]
        expected_gross_profit = abs(target_fill - entry_price) * quantity
        if friction_ratio_pct(expected_gross_profit, target_charges) > max_fee_ratio_pct:
            return 0
        return quantity

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
    """Mark an open trade after adverse exit spread and estimated round-trip fees."""
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
    """Reject a next-bar entry if its open has moved too far from the signal close."""
    if signal_price <= 0 or next_bar_open <= 0 or max_slippage_pct < 0:
        return False
    gap_pct = abs(next_bar_open - signal_price) / signal_price * 100.0
    return gap_pct <= max_slippage_pct + 1e-9


def is_before_entry_cutoff(fill_time: time, cutoff_time: time) -> bool:
    """Allow fills strictly before the configured entry cutoff."""
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
    """Require the existing position to remain above the slot gate at this open."""
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
    """Use realized and open net P&L to decide whether to halt new entries."""
    return day_realized + open_mark_net <= daily_stop


def average_daily_range_pct_before_session(
    candles_df: pd.DataFrame,
    session_date,
    lookback: int = 14,
) -> float:
    """Calculate ADR from prior completed sessions without using today's range."""
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
):
    """Replay completed-bar signals with conservative next-bar execution.

    This is a paper audit. It deliberately fixes the capital base and uses
    structural stops, a fixed 2R target, adverse spread, and exact charges.
    """
    if starting_capital != 15000.0:
        raise ValueError("APEX-3 audit runs use the fixed Rs. 15,000 capital base")
    if target_r != 2.0 or not use_fixed_target:
        raise ValueError("APEX-3 audit requires a fixed 2.0R target")
    if trail_enabled:
        raise ValueError("Continuous trailing is disabled by the APEX-3 audit rules")
    if initial_atr_mult != 1.5:
        raise ValueError("APEX-3 audit requires the 1.5x ATR structural stop")
    if max_slots < 1:
        raise ValueError("At least one position slot is required")

    config = load_config()
    cutoff_time = entry_cutoff_time if entry_cutoff_time is not None else config.schedule.get_time("entry_cutoff")
    if execution_spread_bps < 0 or execution_spread_bps > 4.0:
        raise ValueError("Execution spread must be between 0 and 4.0 bps")
    nifty_candles = data['NSEI']['candles']
    nifty_df = data['NSEI']['df']

    dates = sorted(list(set(nifty_df['Datetime'].dt.date)))
    test_dates = sorted(set(session_dates)) if session_dates is not None else dates[3:]

    # Gate 9C uses only completed daily ranges from before each session.
    adr_by_symbol = {}
    for sym in watchlist_symbols:
        df = data[sym]['df'].copy()
        df['session_date'] = df['Datetime'].dt.date
        daily = df.groupby('session_date', sort=True).agg(
            high=('High', 'max'),
            low=('Low', 'min'),
            close=('Close', 'last'),
        )
        prior_close = daily['close'].shift(1)
        daily_ranges = (daily['high'] - daily['low']) / prior_close
        adr_values = daily_ranges.rolling(14).mean().shift(1)
        adr_by_symbol[sym] = {
            date: (float(value) if pd.notna(value) else 0.0)
            for date, value in adr_values.items()
        }

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
        # Get session indices
        n_sess_mask = (nifty_df['Datetime'].dt.date == session_date)
        sess_indices = nifty_df[n_sess_mask].index.tolist()
        if len(sess_indices) < 6:
            continue

        # Opening ranges
        opening_ranges = {}
        stock_sess_map = {}
        stock_sess_indices = {}

        for sym in watchlist_symbols:
            s_candles = data[sym]['candles']
            s_df = data[sym]['df']
            s_indices = s_df[s_df['Datetime'].dt.date == session_date].index.tolist()
            stock_sess_indices[sym] = s_indices
            stock_sess_map[sym] = {
                s_candles[idx].timestamp: idx for idx in s_indices
            }

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

            # Apply the daily gate to the live opening mark before any order
            # can fill. Missing marks halt new entries for this session.
            open_mark_complete = True
            open_mark_net = 0.0
            for open_sym, open_pos in open_positions.items():
                open_idx = stock_sess_map[open_sym].get(current_dt)
                if open_idx is None:
                    open_mark_complete = False
                    continue
                open_bar = data[open_sym]['candles'][open_idx]
                open_mark_net += estimate_open_position_net_pnl(
                    open_pos['side'],
                    entry_price=open_pos['entry_price'],
                    mark_price=open_bar.open,
                    quantity=open_pos['quantity'],
                    costs=config.costs,
                    spread_bps=execution_spread_bps,
                )
            if not open_mark_complete or daily_loss_limit_reached(
                day_realized, open_mark_net, daily_stop
            ):
                daily_halted = True

            # A signal from the prior completed candle can fill only at this
            # candle's open. It is canceled if the opening gap is too large.
            if pending_entry is not None:
                pending = pending_entry
                pending_entry = None
                fill_dt = pending['fill_time']
                sym = pending['symbol']
                s_idx = stock_sess_map[sym].get(current_dt)
                can_fill = (
                    current_dt == fill_dt
                    and s_idx is not None
                    and not daily_halted
                    and is_before_entry_cutoff(current_dt.time(), cutoff_time)
                    and len(open_positions) < max_positions
                )

                if can_fill and len(open_positions) == 1:
                    first = next(iter(open_positions.values()))
                    first_bar_idx = stock_sess_map[first['symbol']].get(current_dt)
                    if first_bar_idx is None:
                        can_fill = False
                    else:
                        first_open = data[first['symbol']]['candles'][first_bar_idx].open
                        can_fill = slot_two_unlocked_at_open(
                            first['side'],
                            entry_price=first['entry_price'],
                            open_price=first_open,
                            stop_price=first['current_stop'],
                            risk_per_share=first['r_rupees'],
                        )

                if can_fill:
                    s_bar = data[sym]['candles'][s_idx]
                    if is_entry_fill_acceptable(
                        pending['signal_price'], s_bar.open, config.entry.max_slippage_pct
                    ):
                        side = pending['side']
                        entry_price = apply_adverse_spread(
                            s_bar.open, side, execution_spread_bps
                        )
                        levels = compute_initial_stop_and_target(
                            side=side,
                            entry_price=entry_price,
                            swing_low_5=pending['swing_low_5'],
                            swing_high_5=pending['swing_high_5'],
                            atr14=pending['atr'],
                            cfg=config.raw_dict,
                        )
                        target_price = calculate_target_price(
                            side=side,
                            entry_price=entry_price,
                            initial_stop=levels.initial_stop,
                            target_r=2.0,
                            tick_size=0.05,
                        )
                        stop_pct = levels.risk_pct
                        atr_pct = pending['atr'] / entry_price if entry_price > 0 else 0.0
                        adr14 = adr_by_symbol[sym].get(session_date, 0.0)
                        fill_session_high = max(pending['session_high'], s_bar.open)
                        fill_session_low = min(pending['session_low'], s_bar.open)
                        range_used = (
                            (fill_session_high - fill_session_low)
                            / fill_session_low
                            if fill_session_low > 0 else float('inf')
                        )
                        adr_consumed = range_used / adr14 if adr14 > 0 else float('inf')
                        gate9_pass = (
                            adr14 > 0
                            and atr_pct >= 0.0022
                            and 0.0045 <= stop_pct <= 0.0085
                            and adr_consumed <= 0.60
                        )
                        if gate9_pass:
                            quantity = size_backtest_quantity(
                                entry_price=entry_price,
                                stop_price=levels.initial_stop,
                                side=side,
                                slot_budget=slot_budget,
                                risk_cap=risk_cap,
                                target_r=2.0,
                                costs=config.costs,
                                spread_bps=execution_spread_bps,
                            )
                            if quantity > 0:
                                open_positions[sym] = {
                                    'symbol': sym,
                                    'side': side,
                                    'entry_price': entry_price,
                                    'signal_price': pending['signal_price'],
                                    'signal_time': pending['signal_time'],
                                    'initial_stop': levels.initial_stop,
                                    'current_stop': levels.initial_stop,
                                    'target_price': target_price,
                                    'quantity': quantity,
                                    'entry_time': current_dt,
                                    'r_rupees': levels.risk_per_share,
                                    'unrealized_r': 0.0,
                                    'atr': pending['atr'],
                                    'fee_lock_applied': False,
                                }

            closed_syms = []
            for sym, pos in open_positions.items():
                s_idx = stock_sess_map[sym].get(current_dt)
                force_session_flatten = (
                    bar_time >= time(14, 55) or step == len(sess_indices) - 1
                )
                fallback_eod_bar = bar_time > time(14, 55)
                if (s_idx is None or fallback_eod_bar) and force_session_flatten:
                    available_eod_bars = [
                        idx for idx in stock_sess_indices[sym]
                        if data[sym]['candles'][idx].timestamp <= current_dt
                        and data[sym]['candles'][idx].timestamp.time() < time(14, 55)
                    ]
                    if not available_eod_bars:
                        raise RuntimeError(
                            f"Cannot flatten {sym} on {session_date}: no same-session price before 14:55"
                        )
                    s_idx = available_eod_bars[-1]
                    fallback_eod_bar = True
                elif s_idx is None:
                    continue
                s_bar = data[sym]['candles'][s_idx]
                exit_event = None
                exit_time_text = current_dt.strftime('%H:%M')
                if fallback_eod_bar or (
                    step == len(sess_indices) - 1 and bar_time < time(14, 55)
                ):
                    eod_reason = (
                        "EOD_FLATTEN_LAST_AVAILABLE"
                        if fallback_eod_bar else "EOD_FLATTEN_LAST_SESSION_BAR"
                    )
                    liquidation_bar_close = (
                        s_bar.timestamp + timedelta(minutes=5)
                    ).time()
                    exit_event, new_stop, lock_applied = resolve_eod_bar_exit(
                        pos['side'],
                        open_price=s_bar.open,
                        high=s_bar.high,
                        low=s_bar.low,
                        close=s_bar.close,
                        stop_price=pos['current_stop'],
                        target_price=pos['target_price'],
                        entry_price=pos['entry_price'],
                        risk_per_share=pos['r_rupees'],
                        bar_start_time=s_bar.timestamp.time(),
                        bar_close_time=liquidation_bar_close,
                        fee_lock_applied=pos.get('fee_lock_applied', False),
                        eod_reason=eod_reason,
                    )
                    pos['current_stop'] = new_stop
                    pos['fee_lock_applied'] = lock_applied
                    if bar_time > time(14, 55):
                        exit_time_text = (
                            s_bar.timestamp.strftime('%H:%M')
                            if exit_event[1] in {"STOP_LOSS", "FEE_LOCK_STOP", "TARGET_HIT"}
                            else "14:55"
                        )
                    elif eod_reason == "EOD_FLATTEN_LAST_SESSION_BAR":
                        exit_time_text = liquidation_bar_close.strftime('%H:%M')
                elif bar_time == time(14, 55):
                    exit_event = resolve_bar_exit(
                        pos['side'],
                        open_price=s_bar.open,
                        high=s_bar.open,
                        low=s_bar.open,
                        stop_price=pos['current_stop'],
                        target_price=pos['target_price'],
                    )
                    if exit_event is None:
                        exit_event = (s_bar.open, "EOD_FLATTEN_1455")
                    elif exit_event[1] == "STOP_LOSS" and pos.get('fee_lock_applied', False):
                        exit_event = (exit_event[0], "FEE_LOCK_STOP")
                else:
                    exit_event, new_stop, lock_applied = resolve_position_bar_exit(
                        pos['side'],
                        open_price=s_bar.open,
                        high=s_bar.high,
                        low=s_bar.low,
                        close=s_bar.close,
                        stop_price=pos['current_stop'],
                        target_price=pos['target_price'],
                        entry_price=pos['entry_price'],
                        risk_per_share=pos['r_rupees'],
                        bar_close_time=bar_close_dt.time(),
                        bar_start_time=bar_time,
                        fee_lock_applied=pos.get('fee_lock_applied', False),
                    )
                    pos['current_stop'] = new_stop
                    pos['fee_lock_applied'] = lock_applied

                if exit_event is not None:
                    base_exit_price, exit_reason = exit_event
                    exit_side = Side.SELL if pos['side'] == Side.BUY else Side.BUY
                    exit_price = apply_adverse_spread(
                        base_exit_price, exit_side, execution_spread_bps
                    )
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
                        'exit_time': exit_time_text,
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
                    close_r = (
                        (s_bar.close - pos['entry_price']) / pos['r_rupees']
                        if pos['side'] == Side.BUY
                        else (pos['entry_price'] - s_bar.close) / pos['r_rupees']
                    )
                    pos['unrealized_r'] = close_r

            for cs in closed_syms:
                del open_positions[cs]

            # The hard daily gate uses realized P&L plus fees and conservative
            # adverse marks for any position still open.
            open_worst_case = 0.0
            for sym, pos in open_positions.items():
                s_idx = stock_sess_map[sym].get(current_dt)
                if s_idx is None:
                    continue
                s_bar = data[sym]['candles'][s_idx]
                worst_mark = s_bar.low if pos['side'] == Side.BUY else s_bar.high
                open_worst_case += estimate_open_position_net_pnl(
                    pos['side'],
                    entry_price=pos['entry_price'],
                    mark_price=worst_mark,
                    quantity=pos['quantity'],
                    costs=config.costs,
                    spread_bps=execution_spread_bps,
                )
            daily_halted = daily_halted or daily_loss_limit_reached(
                day_realized, open_worst_case, daily_stop
            )

            # Signal evaluation only uses a completed bar and can create one
            # pending order for the next aligned stock/index candle.
            next_n_idx = sess_indices[step + 1] if step + 1 < len(sess_indices) else None
            if (
                next_n_idx is not None
                and not daily_halted
                and len(open_positions) < max_positions
                and is_before_entry_cutoff(
                    nifty_candles[next_n_idx].timestamp.time(), cutoff_time
                )
                and bar_close_dt.time() >= time(9, 30)
            ):
                if len(open_positions) == 1:
                    first_p = next(iter(open_positions.values()))
                    if first_p.get('unrealized_r', 0.0) < 0.50:
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
                    today_indices = [
                        idx for idx in stock_sess_indices[sym]
                        if data[sym]['candles'][idx].timestamp <= current_dt
                    ]
                    today_candles = [data[sym]['candles'][idx] for idx in today_indices]
                    if len(today_candles) < 5:
                        continue

                    or_range = opening_ranges[sym]
                    if not or_range.is_complete:
                        continue
                    first_open = data[sym]['candles'][stock_sess_indices[sym][0]].open
                    history_start = max(0, s_idx - 60)
                    stock_history = data[sym]['candles'][history_start:s_idx + 1]
                    atr_series = compute_atr(
                        [c.high for c in stock_history],
                        [c.low for c in stock_history],
                        [c.close for c in stock_history],
                        period=14,
                    )
                    if not atr_series:
                        continue
                    atr_val = atr_series[-1]
                    if atr_val <= 0:
                        continue

                    prior_volume_bars = data[sym]['candles'][max(0, s_idx - 20):s_idx]
                    if len(prior_volume_bars) < 20:
                        continue
                    average_volume = sum(c.volume for c in prior_volume_bars) / len(prior_volume_bars)
                    if average_volume <= 0:
                        continue

                    adr14 = adr_by_symbol[sym].get(session_date, 0.0)
                    if adr14 <= 0:
                        continue

                    stock_closes = [c.close for c in stock_history[-20:]]
                    bench_closes = [c.close for c in bench_slice[-20:]]
                    if len(stock_closes) < 20 or len(bench_closes) < 20:
                        continue
                    stock_return_20 = (stock_closes[-1] - stock_closes[0]) / stock_closes[0] * 100.0
                    nifty_return_20 = (bench_closes[-1] - bench_closes[0]) / bench_closes[0] * 100.0
                    swing_bars = today_candles[-5:]
                    session_high = max(c.high for c in today_candles)
                    session_low = min(c.low for c in today_candles)
                    session_vwap = compute_session_vwap(
                        [c.high for c in today_candles],
                        [c.low for c in today_candles],
                        [c.close for c in today_candles],
                        [c.volume for c in today_candles],
                    )
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
                        bid_ask_spread_bps=execution_spread_bps,
                        upper_circuit=first_open * 1.10,
                        lower_circuit=first_open * 0.90,
                        swing_low_5=min(c.low for c in swing_bars),
                        swing_high_5=max(c.high for c in swing_bars),
                        atr14=atr_val,
                        session_high_so_far=session_high,
                        session_low_so_far=session_low,
                        adr14_pct=adr14,
                    )
                    signal = evaluate_candidate_signal(
                        bar=candidate_bar,
                        regime=regime_eval.regime,
                        active_positions_count=len(open_positions),
                        cfg=config.raw_dict,
                    )
                    if signal is None:
                        continue

                    side = Side.BUY if signal.side == "LONG" else Side.SELL
                    cands.append({
                        'symbol': sym,
                        'side': side,
                        'signal_price': s_bar.close,
                        'signal_time': bar_close_dt,
                        'fill_time': next_fill_dt,
                        'swing_low_5': candidate_bar.swing_low_5,
                        'swing_high_5': candidate_bar.swing_high_5,
                        'atr': atr_val,
                        'session_high': session_high,
                        'session_low': session_low,
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
    charge_keys = ('brokerage', 'stt', 'exchange_txn', 'sebi', 'gst', 'stamp_duty')
    charge_breakdown = {
        key: round(sum(t['charges'][key] for t in trades), 2)
        for key in charge_keys
    }
    charge_breakdown['rounding_residual'] = round(
        tot_charges - sum(charge_breakdown.values()), 2
    )
    net_pnl = round(gross_pnl - tot_charges, 2)
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 999.0

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
        'friction_ratio_pct': friction_ratio_pct(gross_win, tot_charges),
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
    print(f"Statutory Friction Ratio: {friction_ratio:.2f}% of positive gross profit (limit: < 26.0%)")
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

    # Save to JSON
    with open('three_month_conservative_audit.json', 'w') as f:
        json.dump({
            'base_3m': {k: v for k, v in res_3m.items() if k != 'trades'},
            'monthly_table': monthly_table,
            'trades_sample': res_3m['trades'][:20],
        }, f, indent=2)

    # Mandatory quantitative checks report whether the audit meets the agreed
    # benchmark; the replay results are never adjusted to force a pass.
    assert res_3m['net_pnl'] > 1083.53, f"Net P&L Rs. {res_3m['net_pnl']} did not exceed Rs. 1,083.53 threshold"
    assert res_3m['win_rate'] >= 58.0, f"Win Rate {res_3m['win_rate']}% < 58.0% threshold"
    assert res_3m['profit_factor'] >= 2.90, f"Profit Factor {res_3m['profit_factor']} < 2.90 threshold"
    assert friction_ratio < 26.0, f"Friction ratio {friction_ratio:.2f}% exceeds 26.0% limit"
    print("\n[PASS] Net, win-rate, profit-factor, and friction thresholds verified")
