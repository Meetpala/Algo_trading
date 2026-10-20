"""Costed APEX-3 backtests for the latest three months and latest 30 days.

The most recent 20 common market sessions are also retained as a chronological
holdout, and earlier sessions are reported separately. No live configuration
is changed by this script.
"""
from __future__ import annotations

import json
import math
from datetime import date, time
from pathlib import Path
from typing import Any

import pandas as pd

from apex3.config import AppConfig, load_config
from apex3.models import Decision, InstrumentInfo, OpeningRange, Quote, Side
from apex3.strategy.regime import MarketRegime, evaluate_market_regime
from apex3.strategy.signal import evaluate_signal
from apex3.strategy.trail import calculate_initial_stop
from apex3.indicators.atr import compute_atr
from backtest_engine import calculate_order_charges, df_to_candles, load_data

STARTING_CAPITAL = 15_000.0
MAX_OOS_SESSIONS = 20
VARIANTS: dict[str, dict[str, Any]] = {
    "fixed_1_5R": {"target_r": 1.5, "partial_at_1r": False},
    "fixed_2R": {"target_r": 2.0, "partial_at_1r": False},
    "partial_1R_runner_2R": {"target_r": 2.0, "partial_at_1r": True},
}


def _common_sessions(data: dict[str, pd.DataFrame]) -> list[date]:
    session_sets = [set(frame["Datetime"].dt.date) for frame in data.values()]
    if not session_sets:
        return []
    return sorted(set.intersection(*session_sets))


def _charge_totals() -> dict[str, float]:
    return {"brokerage": 0.0, "stt": 0.0, "exchange_txn": 0.0, "sebi": 0.0,
            "gst": 0.0, "stamp_duty": 0.0, "total_charges": 0.0}


def _add_charges(total: dict[str, float], addition: dict[str, float]) -> None:
    for key in total:
        total[key] += float(addition.get(key, 0.0))
    for key in total:
        total[key] = round(total[key], 2)


def _gross_pnl(side: Side, entry: float, exit_price: float, quantity: int) -> float:
    sign = 1.0 if side == Side.BUY else -1.0
    return (exit_price - entry) * quantity * sign


def _exit_side(side: Side) -> Side:
    return Side.SELL if side == Side.BUY else Side.BUY


def _signal_rank(signal: Any) -> tuple[float, float, str]:
    directional_strength = (
        signal.relative_strength if signal.side == Side.BUY else -signal.relative_strength
    )
    return (directional_strength, signal.relative_volume, signal.symbol)


def _merge_order_charge(total: dict[str, float], one_order: dict[str, float]) -> None:
    _add_charges(total, one_order)


def run_period(
    symbols: list[str],
    data: dict[str, pd.DataFrame],
    session_dates: list[date],
    config: AppConfig,
    variant_name: str,
) -> dict[str, Any]:
    """Replay one chronological period with fixed capital and production risk caps."""
    variant = VARIANTS[variant_name]
    capital = STARTING_CAPITAL
    all_trades: list[dict[str, Any]] = []
    daily_stats: list[dict[str, Any]] = []
    peak_equity = capital
    max_drawdown = 0.0

    for session_date in session_dates:
        day_start_equity = capital
        session_realized = 0.0
        session_charges = 0.0
        daily_halted = False
        session_trade_records: list[dict[str, Any]] = []

        nifty_session = data["NSEI"][data["NSEI"]["Datetime"].dt.date == session_date].reset_index(drop=True)
        benchmark_times = data["NSEI"]["Datetime"]
        if len(nifty_session) < 6:
            daily_stats.append({"date": session_date.isoformat(), "trades": 0, "net_pnl": 0.0,
                                "ending_equity": round(capital, 2), "daily_halt": False,
                                "skipped": "insufficient benchmark candles"})
            continue

        stock_sessions: dict[str, pd.DataFrame] = {}
        stock_bar_maps: dict[str, dict[pd.Timestamp, pd.Series]] = {}
        stock_time_maps: dict[str, dict[pd.Timestamp, int]] = {}
        opening_ranges: dict[str, OpeningRange] = {}
        for symbol in symbols:
            frame = data[symbol][data[symbol]["Datetime"].dt.date == session_date].reset_index(drop=True)
            stock_sessions[symbol] = frame
            stock_bar_maps[symbol] = {row["Datetime"]: row for _, row in frame.iterrows()}
            stock_time_maps[symbol] = {timestamp: index for index, timestamp in enumerate(frame["Datetime"])}
            opening_bars = frame[frame["Datetime"].dt.time < time(9, 30)]
            opening_ranges[symbol] = OpeningRange(
                symbol=symbol,
                high=float(opening_bars["High"].max()) if len(opening_bars) >= 3 else 0.0,
                low=float(opening_bars["Low"].min()) if len(opening_bars) >= 3 else 0.0,
                is_complete=len(opening_bars) >= 3,
                candle_count=len(opening_bars),
            )

        # At most three open positions, each sized from the same 0.25% equity risk.
        open_positions: dict[str, dict[str, Any]] = {}

        def close_quantity(
            symbol: str,
            position: dict[str, Any],
            quantity: int,
            exit_price: float,
            reason: str,
            exit_time: pd.Timestamp,
        ) -> None:
            nonlocal capital, session_realized, session_charges
            if quantity <= 0:
                return
            quantity = min(quantity, position["remaining_qty"])
            gross = _gross_pnl(position["side"], position["entry_price"], exit_price, quantity)
            fee = calculate_order_charges(_exit_side(position["side"]), exit_price, quantity, config.costs)
            position["gross_pnl"] += gross
            position["charges_total"] += fee["total_charges"]
            _merge_order_charge(position["charges"], fee)
            position["remaining_qty"] -= quantity
            session_realized += gross
            session_charges += fee["total_charges"]
            capital += gross - fee["total_charges"]

            if position["remaining_qty"] == 0:
                net = position["gross_pnl"] - position["charges_total"]
                record = {
                    "date": session_date.isoformat(),
                    "symbol": symbol,
                    "side": position["side"].value,
                    "qty": position["quantity"],
                    "partial_exit_qty": position["partial_exit_qty"],
                    "entry_time": position["entry_time"].isoformat(),
                    "entry_price": round(position["entry_price"], 2),
                    "exit_time": exit_time.isoformat(),
                    "exit_price_last_leg": round(exit_price, 2),
                    "exit_reason": reason,
                    "gross_pnl": round(position["gross_pnl"], 2),
                    "charges": {key: round(value, 2) for key, value in position["charges"].items()},
                    "net_pnl": round(net, 2),
                    "r_multiple_net": round(net / (position["r_per_share"] * position["quantity"]), 2)
                    if position["r_per_share"] * position["quantity"] > 0 else 0.0,
                }
                all_trades.append(record)
                session_trade_records.append(record)
                del open_positions[symbol]

        for _, benchmark_bar in nifty_session.iterrows():
            current_dt = benchmark_bar["Datetime"]
            bar_end_dt = current_dt + pd.Timedelta(minutes=5)
            bar_time = bar_end_dt.time()

            # Manage open trades using the matching symbol timestamp only.
            for symbol, position in list(open_positions.items()):
                stock_bar = stock_bar_maps[symbol].get(current_dt)
                if stock_bar is None:
                    continue
                high, low = float(stock_bar["High"]), float(stock_bar["Low"])
                opening, close = float(stock_bar["Open"]), float(stock_bar["Close"])
                side = position["side"]
                stop = position["current_stop"]

                stop_hit = (low <= stop) if side == Side.BUY else (high >= stop)
                if stop_hit:
                    stop_fill = min(stop, opening) if side == Side.BUY else max(stop, opening)
                    close_quantity(symbol, position, position["remaining_qty"], stop_fill,
                                   "BREAKEVEN_STOP" if position["partial_taken"] else "STRUCTURAL_STOP",
                                   bar_end_dt)
                    continue

                if variant["partial_at_1r"] and not position["partial_taken"]:
                    first_target_hit = high >= position["target_1r"] if side == Side.BUY else low <= position["target_1r"]
                    if first_target_hit and position["partial_exit_qty"] > 0:
                        close_quantity(symbol, position, position["partial_exit_qty"], position["target_1r"],
                                       "PARTIAL_TARGET_1R", bar_end_dt)
                        position["partial_taken"] = True
                        position["current_stop"] = position["entry_price"]

                # Check the runner's new break-even stop before its 2R target.
                if symbol not in open_positions:
                    continue
                if position["partial_taken"]:
                    breakeven_hit = (low <= position["entry_price"]) if side == Side.BUY else (high >= position["entry_price"])
                    runner_target_hit = high >= position["target_price"] if side == Side.BUY else low <= position["target_price"]
                    if breakeven_hit:
                        close_quantity(symbol, position, position["remaining_qty"], position["entry_price"],
                                       "BREAKEVEN_STOP", bar_end_dt)
                        continue
                    if runner_target_hit:
                        close_quantity(symbol, position, position["remaining_qty"], position["target_price"],
                                       "TARGET_HIT_2R", bar_end_dt)
                        continue
                else:
                    target_hit = high >= position["target_price"] if side == Side.BUY else low <= position["target_price"]
                    if target_hit:
                        reason = f"TARGET_HIT_{variant['target_r']:g}R"
                        close_quantity(symbol, position, position["remaining_qty"], position["target_price"],
                                       reason, bar_end_dt)
                        continue

                if bar_time >= time(14, 55) and symbol in open_positions:
                    close_quantity(symbol, position, position["remaining_qty"], close, "EOD_FLATTEN_1455", bar_end_dt)

            open_unrealized = 0.0
            for symbol, position in open_positions.items():
                current_bar = stock_bar_maps[symbol].get(current_dt)
                if current_bar is None:
                    continue
                open_unrealized += _gross_pnl(position["side"], position["entry_price"],
                                              float(current_bar["Close"]), position["remaining_qty"])
            net_day_pnl = session_realized + open_unrealized - session_charges
            if net_day_pnl <= -(day_start_equity * config.risk.daily_loss_limit_pct / 100.0):
                daily_halted = True

            # Signal decisions are made only after the current bar is complete.
            if not (time(9, 30) <= bar_time <= time(14, 15)) or daily_halted:
                continue
            if len(open_positions) >= config.limits.max_symbols:
                continue

            benchmark_history_end = int(benchmark_times.searchsorted(current_dt, side="right"))
            if benchmark_history_end <= 0:
                continue
            benchmark_candles = df_to_candles(
                data["NSEI"].iloc[max(0, benchmark_history_end - 100):benchmark_history_end]
            )
            regime = evaluate_market_regime(benchmark_candles, config.regime)
            if regime.regime == MarketRegime.NEUTRAL:
                continue

            candidates: list[tuple[Any, InstrumentInfo, list[Any], pd.Series]] = []
            for symbol in symbols:
                if symbol in open_positions:
                    continue
                signal_bar = stock_bar_maps[symbol].get(current_dt)
                if signal_bar is None or not opening_ranges[symbol].is_complete:
                    continue
                today_bars = stock_sessions[symbol]
                today_end = stock_time_maps[symbol].get(current_dt)
                if today_end is None:
                    continue
                today_bars = today_bars.iloc[:today_end + 1]
                if len(today_bars) < 4:
                    continue
                stock_history_end = int(data[symbol]["Datetime"].searchsorted(current_dt, side="right"))
                stock_candles = df_to_candles(
                    data[symbol].iloc[max(0, stock_history_end - 60):stock_history_end]
                )
                today_candles = df_to_candles(today_bars)
                instrument = InstrumentInfo(
                    symbol=symbol,
                    upper_circuit=float(today_bars.iloc[0]["Open"]) * 1.10,
                    lower_circuit=float(today_bars.iloc[0]["Open"]) * 0.90,
                    tick_size=0.05,
                )
                close_price = float(signal_bar["Close"])
                quote = Quote(
                    symbol=symbol,
                    exchange="NSE",
                    last_price=close_price,
                    bid=close_price - instrument.tick_size,
                    ask=close_price + instrument.tick_size,
                    volume=int(signal_bar["Volume"]),
                    timestamp=bar_end_dt.to_pydatetime(),
                    is_fresh=True,
                )
                signal = evaluate_signal(
                    symbol=symbol,
                    stock_candles_5m=today_candles,
                    benchmark_candles_5m=benchmark_candles,
                    opening_range=opening_ranges[symbol],
                    market_regime=regime.regime,
                    instrument=instrument,
                    quote=quote,
                    config=config.signal,
                    is_entry_window=True,
                )
                if signal.decision in (Decision.PROPOSED_LONG, Decision.PROPOSED_SHORT):
                    candidates.append((signal, instrument, stock_candles, signal_bar))

            candidates.sort(key=lambda item: _signal_rank(item[0]), reverse=True)
            qualified_count = min(config.limits.max_symbols, len(candidates))

            for signal, instrument, stock_candles, signal_bar in candidates:
                if len(open_positions) >= config.limits.max_symbols:
                    break
                entry_price = signal.signal_price
                side = signal.side
                atr_series = compute_atr(
                    [c.high for c in stock_candles],
                    [c.low for c in stock_candles],
                    [c.close for c in stock_candles],
                    period=config.stops.initial_atr_period,
                )
                atr_value = atr_series[-1] if atr_series else entry_price * 0.01
                stop = calculate_initial_stop(
                    side=side,
                    entry_price=entry_price,
                    signal_low=float(signal_bar["Low"]),
                    signal_high=float(signal_bar["High"]),
                    atr_value=atr_value,
                    config=config.stops,
                    tick_size=instrument.tick_size,
                )
                if (side == Side.BUY and stop >= entry_price) or (side == Side.SELL and stop <= entry_price):
                    continue

                equity_for_sizing = capital + open_unrealized
                usable_margin = max(0.0, equity_for_sizing * (1.0 - config.limits.safety_reserve_pct / 100.0))
                allocation_count = max(1, min(config.limits.max_symbols, qualified_count))
                notional_budget = usable_margin * config.limits.max_leverage / allocation_count
                q_notional = math.floor(notional_budget / entry_price)
                per_share_risk = abs(entry_price - stop) + entry_price * config.risk.slippage_allowance_pct / 100.0
                risk_budget = equity_for_sizing * config.risk.risk_per_trade_pct / 100.0
                q_risk = math.floor(risk_budget / per_share_risk) if per_share_risk > 0 else 0
                quantity = min(q_notional, q_risk)
                if quantity <= 0:
                    continue

                combined_risk = sum(
                    position["remaining_qty"] * abs(position["entry_price"] - position["current_stop"])
                    for position in open_positions.values()
                )
                quantity = min(
                    quantity,
                    math.floor(max(0.0, equity_for_sizing * config.risk.max_combined_open_risk_pct / 100.0 - combined_risk)
                               / per_share_risk),
                )
                if quantity <= 0:
                    continue

                entry_charges = calculate_order_charges(side, entry_price, quantity, config.costs)
                charges = _charge_totals()
                _merge_order_charge(charges, entry_charges)
                capital -= entry_charges["total_charges"]
                session_charges += entry_charges["total_charges"]
                risk_per_share = abs(entry_price - stop)
                target_1r = entry_price + risk_per_share if side == Side.BUY else entry_price - risk_per_share
                target_price = entry_price + variant["target_r"] * risk_per_share if side == Side.BUY else entry_price - variant["target_r"] * risk_per_share
                partial_exit_qty = max(1, quantity // 2) if variant["partial_at_1r"] and quantity >= 2 else 0
                open_positions[signal.symbol] = {
                    "side": side,
                    "quantity": quantity,
                    "remaining_qty": quantity,
                    "partial_exit_qty": partial_exit_qty,
                    "partial_taken": False,
                    "entry_price": entry_price,
                    "entry_time": bar_end_dt,
                    "initial_stop": stop,
                    "current_stop": stop,
                    "target_1r": target_1r,
                    "target_price": target_price,
                    "r_per_share": risk_per_share,
                    "gross_pnl": 0.0,
                    "charges_total": entry_charges["total_charges"],
                    "charges": charges,
                }

        # The configured flatten window should have closed all positions. Mark any
        # data gaps conservatively at the last aligned close before recording the day.
        for symbol, position in list(open_positions.items()):
            last_bar = stock_sessions[symbol].iloc[-1]
            last_time = last_bar["Datetime"] + pd.Timedelta(minutes=5)
            close_quantity(symbol, position, position["remaining_qty"], float(last_bar["Close"]),
                           "DATA_END_FLATTEN", last_time)

        # Reconcile equity to the closed trade ledger at each session boundary.
        # This removes cumulative floating-point drift from per-fill arithmetic.
        day_pnl = round(sum(record["net_pnl"] for record in session_trade_records), 2)
        capital = round(day_start_equity + day_pnl, 2)
        peak_equity = max(peak_equity, capital)
        max_drawdown = max(max_drawdown, peak_equity - capital)
        daily_stats.append({
            "date": session_date.isoformat(),
            "trades": len(session_trade_records),
            "net_pnl": day_pnl,
            "ending_equity": round(capital, 2),
            "daily_halt": daily_halted,
        })

    wins = [trade for trade in all_trades if trade["net_pnl"] > 0]
    losses = [trade for trade in all_trades if trade["net_pnl"] < 0]
    gross_pnl = round(sum(trade["gross_pnl"] for trade in all_trades), 2)
    net_profit = round(sum(trade["net_pnl"] for trade in all_trades), 2)
    positive_days = sum(1 for row in daily_stats if row["net_pnl"] > 0)
    no_trade_days = sum(1 for row in daily_stats if row["trades"] == 0)
    total_fees = round(sum(trade["charges"]["total_charges"] for trade in all_trades), 2)
    gross_profit = sum(trade["gross_pnl"] for trade in all_trades if trade["gross_pnl"] > 0)
    gross_loss = abs(sum(trade["gross_pnl"] for trade in all_trades if trade["gross_pnl"] < 0))
    net_wins = sum(trade["net_pnl"] for trade in wins)
    net_losses = abs(sum(trade["net_pnl"] for trade in losses))
    charge_breakdown = {
        key: round(sum(float(trade["charges"].get(key, 0.0)) for trade in all_trades), 2)
        for key in _charge_totals()
    }

    return {
        "sessions": len(session_dates),
        "trades": len(all_trades),
        "wins_after_charges": len(wins),
        "losses_after_charges": len(losses),
        "trade_win_rate_pct": round(100.0 * len(wins) / len(all_trades), 2) if all_trades else 0.0,
        "positive_session_probability_pct": round(100.0 * positive_days / len(daily_stats), 2) if daily_stats else 0.0,
        "positive_sessions": positive_days,
        "no_trade_sessions": no_trade_days,
        "starting_capital": STARTING_CAPITAL,
        "ending_capital": round(STARTING_CAPITAL + net_profit, 2),
        "gross_pnl": gross_pnl,
        "estimated_charges": total_fees,
        "charges_breakdown": charge_breakdown,
        "net_pnl": net_profit,
        "net_return_pct": round(net_profit / STARTING_CAPITAL * 100.0, 2),
        "gross_profit_factor": round(gross_profit / gross_loss, 2) if gross_loss else None,
        "net_profit_factor": round(net_wins / net_losses, 2) if net_losses else None,
        "max_daily_close_drawdown": round(max_drawdown, 2),
        "daily_stats": daily_stats,
        "trades_detail": all_trades,
    }


def build_report() -> dict[str, Any]:
    config = load_config()
    symbols = list(config.watchlist.scanner_universe)
    if not symbols:
        raise ValueError("The scanner universe is empty; there is no symbol data to compare.")
    data = {"NSEI": load_data("NSEI")}
    data.update({symbol: load_data(symbol) for symbol in symbols})
    dates = _common_sessions(data)
    if len(dates) <= MAX_OOS_SESSIONS:
        raise ValueError(f"Need more than {MAX_OOS_SESSIONS} common sessions; found {len(dates)}.")

    latest_data_date = pd.Timestamp(dates[-1])
    three_month_cutoff = (latest_data_date - pd.DateOffset(months=3)).date()
    three_month_dates = [day for day in dates if day >= three_month_cutoff]
    last_30_day_cutoff = (latest_data_date - pd.Timedelta(days=30)).date()
    last_30_day_dates = [day for day in dates if day >= last_30_day_cutoff]
    if not three_month_dates or not last_30_day_dates:
        raise ValueError("Not enough common market data for the requested periods.")

    out_of_sample_dates = dates[-MAX_OOS_SESSIONS:]
    in_sample_dates = dates[:-MAX_OOS_SESSIONS]
    result_sets: dict[str, dict[str, Any]] = {}
    for variant_name in VARIANTS:
        out_of_sample = run_period(symbols, data, out_of_sample_dates, config, variant_name)
        # When the calendar lookback matches the 20-session holdout, reuse the
        # exact replay. This avoids reporting two subtly different versions of
        # the same latest-month result.
        last_30_days = (
            out_of_sample
            if last_30_day_dates == out_of_sample_dates
            else run_period(symbols, data, last_30_day_dates, config, variant_name)
        )
        result_sets[variant_name] = {
            "in_sample": run_period(symbols, data, in_sample_dates, config, variant_name),
            "out_of_sample": out_of_sample,
            "three_month_period": run_period(symbols, data, three_month_dates, config, variant_name),
            "last_30_calendar_days": last_30_days,
        }

    best_train_variant = max(
        VARIANTS,
        key=lambda name: result_sets[name]["in_sample"]["net_pnl"],
    )
    best_holdout_variant = max(
        VARIANTS,
        key=lambda name: result_sets[name]["out_of_sample"]["net_pnl"],
    )
    positive_holdout_variants = [
        name for name in VARIANTS
        if result_sets[name]["out_of_sample"]["net_pnl"] > 0
    ]
    consistent_positive_variants = [
        name for name in VARIANTS
        if result_sets[name]["three_month_period"]["net_pnl"] > 0
        and result_sets[name]["out_of_sample"]["net_pnl"] > 0
    ]
    best_three_month_variant = max(
        VARIANTS,
        key=lambda name: result_sets[name]["three_month_period"]["net_pnl"],
    )
    best_last_30_day_variant = max(
        VARIANTS,
        key=lambda name: result_sets[name]["last_30_calendar_days"]["net_pnl"],
    )
    return {
        "strategy": "APEX-3 opening-range breakout",
        "universe": symbols,
        "capital_per_variant": STARTING_CAPITAL,
        "requested_periods": {
            "three_month_calendar_cutoff": three_month_cutoff.isoformat(),
            "three_month_available_start": three_month_dates[0].isoformat(),
            "three_month_end": three_month_dates[-1].isoformat(),
            "three_month_available_sessions": len(three_month_dates),
            "last_30_calendar_days_cutoff": last_30_day_cutoff.isoformat(),
            "last_30_available_start": last_30_day_dates[0].isoformat(),
            "last_30_end": last_30_day_dates[-1].isoformat(),
            "last_30_available_sessions": len(last_30_day_dates),
            "periods_anchored_to_latest_available_data_date": latest_data_date.date().isoformat(),
        },
        "split": {
            "in_sample_start": in_sample_dates[0].isoformat(),
            "in_sample_end": in_sample_dates[-1].isoformat(),
            "in_sample_sessions": len(in_sample_dates),
            "out_of_sample_start": out_of_sample_dates[0].isoformat(),
            "out_of_sample_end": out_of_sample_dates[-1].isoformat(),
            "out_of_sample_sessions": len(out_of_sample_dates),
            "out_of_sample_is_latest_30_calendar_days": True,
        },
        "assumptions": {
            "risk_per_trade_pct": config.risk.risk_per_trade_pct,
            "max_combined_open_risk_pct": config.risk.max_combined_open_risk_pct,
            "daily_loss_limit_pct": config.risk.daily_loss_limit_pct,
            "max_simultaneous_positions": config.limits.max_symbols,
            "notional_leverage_ceiling": config.limits.max_leverage,
            "position_sizing": "min(0.25% equity risk budget, equal notional budget, combined-risk headroom); 0.10% per-share slippage allowance included in sizing",
            "entry_fill": "completed 5-minute signal candle close",
            "stop_fill": "stop price, or worse candle open if gapped through; stop has priority when stop and target are both touched in one candle",
            "target_fill": "target price when touched; no extra target slippage",
            "eod_fill": "close of first bar ending at or after 14:55 IST",
            "daily_loss_action": "block new entries for the rest of the session; manage open positions to their configured exits",
            "partial_exit": "50% of whole shares at 1R (minimum one share when quantity >= 2), then move remaining stop to entry and target the remaining 2R level",
            "candidate_selection": "for a simultaneous scanner signal, directional relative strength descending, then relative volume descending, then symbol name",
            "fees": "Zerodha NSE equity intraday estimates, charged for entry and each exit order",
            "fees_include": ["brokerage", "STT", "NSE transaction charges", "SEBI fee", "GST", "stamp duty"],
            "fee_rounding_note": "Order totals round the raw fee components to paise; independently rounded component rollups may differ slightly from the total.",
            "scanner_live_config": config.watchlist.scanner_enabled,
        },
        "selected_on_in_sample_only": best_train_variant,
        "best_out_of_sample_net_pnl": best_holdout_variant,
        "best_three_month_net_pnl": best_three_month_variant,
        "best_last_30_days_net_pnl": best_last_30_day_variant,
        "positive_out_of_sample_variants": positive_holdout_variants,
        "positive_three_month_and_out_of_sample_variants": consistent_positive_variants,
        "variants": result_sets,
        "recommendation": (
            f"{consistent_positive_variants[0]} was profitable after estimated charges over both the three-month sample and the latest holdout; paper-validate before changing any live target."
            if consistent_positive_variants
            else f"No variant was profitable after estimated charges in both the three-month sample and latest holdout. Keep the configured 2R target, 0.25% risk, and scanner setting unchanged; {best_last_30_day_variant} was the least-negative or most-profitable latest-month candidate, but it is not validated for deployment."
        ),
        "deployment_note": "This historical replay is not a profit guarantee or a realized broker statement. Profitability percentages are historical sample frequencies, not a forecast probability.",
    }


def print_summary(report: dict[str, Any], output_path: Path) -> None:
    """Print an ASCII-safe summary that works in Windows PowerShell consoles."""
    print(f"Backtest report: {output_path.resolve()}")
    periods = report["requested_periods"]
    print(
        f"Three-month window: requested from {periods['three_month_calendar_cutoff']} "
        f"through {periods['three_month_end']}; available {periods['three_month_available_start']} "
        f"to {periods['three_month_end']} ({periods['three_month_available_sessions']} sessions)"
    )
    print(
        f"Last 30 calendar days: requested from {periods['last_30_calendar_days_cutoff']} "
        f"through {periods['last_30_end']}; available {periods['last_30_available_start']} "
        f"to {periods['last_30_end']} ({periods['last_30_available_sessions']} sessions)"
    )
    print(
        f"In sample: {report['split']['in_sample_start']} to {report['split']['in_sample_end']} "
        f"({report['split']['in_sample_sessions']} sessions)"
    )
    print(
        f"Out of sample: {report['split']['out_of_sample_start']} to {report['split']['out_of_sample_end']} "
        f"({report['split']['out_of_sample_sessions']} sessions)"
    )
    print(f"Selected using training data only: {report['selected_on_in_sample_only']}")
    print("Variant                         Window    Trades Win%  Profitable days Gross P&L  Charges  Net P&L  Net %  End capital")
    for name, result in report["variants"].items():
        for label, key in (("3 months", "three_month_period"), ("30 days", "last_30_calendar_days")):
            result_period = result[key]
            print(
                f"{name:30} {label:9} {result_period['trades']:>6} "
                f"{result_period['trade_win_rate_pct']:>5.1f}% "
                f"{result_period['positive_sessions']:>3}/{result_period['sessions']:<3} "
                f"Rs.{result_period['gross_pnl']:>8.2f} Rs.{result_period['estimated_charges']:>7.2f} "
                f"Rs.{result_period['net_pnl']:>8.2f} {result_period['net_return_pct']:>6.2f}% "
                f"Rs.{result_period['ending_capital']:>9.2f}"
            )
    print(f"Recommendation: {report['recommendation']}")


def main() -> None:
    report = build_report()
    output_path = Path("strategy_oos_report.json")
    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print_summary(report, output_path)


if __name__ == "__main__":
    main()
