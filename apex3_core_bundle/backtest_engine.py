"""APEX-3 Backtesting Engine for the last 30 days.

Executes the deterministic 12-step pipeline on real 5-minute historical data.
Evaluates:
- Primary Watchlist: RELIANCE, TCS, INFY
- Full Scanner Universe (15 stocks)
- Initial Capital: Rs. 15,000 (with 4x MIS leverage)
- Exact Zerodha charges breakdown
"""
import os
import math
from datetime import datetime, time
import pandas as pd
import numpy as np

from apex3.config import load_config, SignalConfig, StopsConfig, CostsConfig, LimitsConfig, RiskConfig, RegimeConfig
from apex3.models import Candle, Quote, InstrumentInfo, OpeningRange, Side, Decision, GateStatus
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import build_opening_range, evaluate_signal
from apex3.strategy.trail import calculate_initial_stop, evaluate_trailing_stop, calculate_stop_limit_prices
from apex3.indicators.atr import compute_atr

def load_data(symbol: str) -> pd.DataFrame:
    path = os.path.join('scratch_data', f'{symbol}.csv')
    df = pd.read_csv(path)
    df['Datetime'] = pd.to_datetime(df['Datetime'])
    df = df.sort_values('Datetime').reset_index(drop=True)
    return df

def df_to_candles(df_slice: pd.DataFrame) -> list[Candle]:
    candles = []
    for _, row in df_slice.iterrows():
        candles.append(
            Candle(
                timestamp=row['Datetime'].to_pydatetime(),
                open=float(row['Open']),
                high=float(row['High']),
                low=float(row['Low']),
                close=float(row['Close']),
                volume=int(row['Volume']),
                is_complete=True,
            )
        )
    return candles

def calculate_charges(buy_price: float, sell_price: float, qty: int, costs: CostsConfig) -> dict:
    buy = calculate_order_charges(Side.BUY, buy_price, qty, costs)
    sell = calculate_order_charges(Side.SELL, sell_price, qty, costs)
    buy_turnover = buy_price * qty
    sell_turnover = sell_price * qty
    total_turnover = buy_turnover + sell_turnover
    brokerage = buy['brokerage'] + sell['brokerage']
    stt = buy['stt'] + sell['stt']
    exchange_txn = buy['exchange_txn'] + sell['exchange_txn']
    sebi = buy['sebi'] + sell['sebi']
    gst = buy['gst'] + sell['gst']
    stamp_duty = buy['stamp_duty'] + sell['stamp_duty']
    total_charges = buy['total_charges'] + sell['total_charges']

    return {
        'buy_turnover': buy_turnover,
        'sell_turnover': sell_turnover,
        'total_turnover': total_turnover,
        'brokerage': round(brokerage, 2),
        'stt': round(stt, 2),
        'exchange_txn': round(exchange_txn, 2),
        'sebi': round(sebi, 2),
        'gst': round(gst, 2),
        'stamp_duty': round(stamp_duty, 2),
        'total_charges': round(total_charges, 2),
    }


def calculate_order_charges(side: Side, price: float, qty: int, costs: CostsConfig) -> dict:
    """Estimate charges for one executed equity intraday order."""
    turnover = price * qty
    brokerage = min(costs.brokerage_per_order, turnover * 0.0003)
    stt = turnover * (costs.stt_pct / 100.0) if side == Side.SELL else 0.0
    exchange_txn = turnover * (costs.exchange_txn_pct / 100.0)
    sebi = turnover * (costs.sebi_turnover_pct / 100.0)
    gst = (brokerage + exchange_txn + sebi) * (costs.gst_pct / 100.0)
    stamp_duty = turnover * (costs.stamp_duty_pct / 100.0) if side == Side.BUY else 0.0
    return {
        'turnover': turnover,
        'brokerage': round(brokerage, 2),
        'stt': round(stt, 2),
        'exchange_txn': round(exchange_txn, 2),
        'sebi': round(sebi, 2),
        'gst': round(gst, 2),
        'stamp_duty': round(stamp_duty, 2),
        'total_charges': round(brokerage + stt + exchange_txn + sebi + gst + stamp_duty, 2),
    }

def run_backtest(watchlist_symbols: list[str], risk_mode: str = 'capital_allocated'):
    """
    risk_mode:
      - 'strict_config': risk_per_trade_pct = 0.25% (literal config.yaml setting, 37.5 Rs risk)
      - 'capital_allocated': Q sized by notional budget (15k capital * 4x leverage / N)
      - 'one_pct_risk': 1.0% risk per trade (150 Rs risk)
    """
    config = load_config()
    nifty_df = load_data('NSEI')
    stock_dfs = {s: load_data(s) for s in watchlist_symbols}

    # Distinct dates
    dates = sorted(list(set(nifty_df['Datetime'].dt.date)))
    # Use the most recent 30 calendar days available in the local data set.
    last_data_date = dates[-1]
    test_start_date = (pd.Timestamp(last_data_date) - pd.Timedelta(days=30)).date()
    test_dates = [d for d in dates if test_start_date <= d <= last_data_date]

    starting_capital = 15000.0
    current_capital = starting_capital
    leverage = 4.0
    safety_reserve_pct = 5.0
    max_positions = 3

    trades = []
    daily_stats = []

    print(f"\n=======================================================")
    print(f"RUNNING BACKTEST: {len(test_dates)} Trading Sessions (Last 30 Days)")
    print(f"Watchlist: {watchlist_symbols}")
    print(f"Sizing Mode: {risk_mode} | Capital: Rs. {starting_capital:.2f}")
    print(f"=======================================================\n")

    for session_date in test_dates:
        # Pre-filter session bars
        nifty_session = nifty_df[nifty_df['Datetime'].dt.date == session_date].reset_index(drop=True)
        if len(nifty_session) < 6:
            continue

        # Slice historical benchmark up to each bar
        # Build opening range for each stock (09:15 to 09:30, i.e., bars with time < 09:30)
        stock_sessions = {}
        opening_ranges = {}

        for sym in watchlist_symbols:
            sdf = stock_dfs[sym]
            s_sess = sdf[sdf['Datetime'].dt.date == session_date].reset_index(drop=True)
            stock_sessions[sym] = s_sess
            or_bars = s_sess[s_sess['Datetime'].dt.time < time(9, 30)]
            if len(or_bars) >= 3:
                opening_ranges[sym] = OpeningRange(
                    symbol=sym,
                    high=float(or_bars['High'].max()),
                    low=float(or_bars['Low'].min()),
                    is_complete=True,
                    candle_count=len(or_bars),
                )
            else:
                opening_ranges[sym] = OpeningRange(symbol=sym, high=0.0, low=0.0, is_complete=False)

        stock_bars_by_time = {
            sym: {row['Datetime']: row for _, row in frame.iterrows()}
            for sym, frame in stock_sessions.items()
        }

        # Active positions during session: sym -> position dict
        open_positions = {}
        session_trades = []

        # Iterate 5-min bars of the session
        for bar_idx in range(len(nifty_session)):
            current_bar = nifty_session.iloc[bar_idx]
            current_dt = current_bar['Datetime']
            bar_end_dt = current_dt + pd.Timedelta(minutes=5)
            bar_time = bar_end_dt.time()

            # 1. Manage open positions (Check stop loss and trailing stop)
            closed_syms = []
            for sym, pos in open_positions.items():
                s_bar = stock_bars_by_time[sym].get(current_dt)
                if s_bar is None:
                    continue
                s_high = float(s_bar['High'])
                s_low = float(s_bar['Low'])
                s_close = float(s_bar['Close'])

                # Check SL hit
                is_stopped = False
                exit_price = pos['current_stop']
                exit_reason = "STOP_LOSS"

                if pos['side'] == Side.BUY:
                    if s_low <= pos['current_stop']:
                        is_stopped = True
                        # If gapped down below stop, exit at open
                        exit_price = min(pos['current_stop'], float(s_bar['Open']))
                        exit_reason = "TRAILING_STOP_HIT" if pos['trail_activated'] else "STRUCTURAL_STOP_HIT"
                else:  # Side.SELL
                    if s_high >= pos['current_stop']:
                        is_stopped = True
                        exit_price = max(pos['current_stop'], float(s_bar['Open']))
                        exit_reason = "TRAILING_STOP_HIT" if pos['trail_activated'] else "STRUCTURAL_STOP_HIT"

                # Check EOD Flatten (14:55 IST or later)
                if not is_stopped and bar_time >= time(14, 55):
                    is_stopped = True
                    exit_price = s_close
                    exit_reason = "EOD_FLATTEN_1455"

                if is_stopped:
                    # Finalize trade
                    buy_p = pos['entry_price'] if pos['side'] == Side.BUY else exit_price
                    sell_p = exit_price if pos['side'] == Side.BUY else pos['entry_price']
                    qty = pos['quantity']
                    gross_pnl = (sell_p - buy_p) * qty
                    charges = calculate_charges(buy_p, sell_p, qty, config.costs)
                    net_pnl = gross_pnl - charges['total_charges']

                    trade_record = {
                        'date': session_date.strftime('%Y-%m-%d'),
                        'symbol': sym,
                        'side': pos['side'].value,
                        'qty': qty,
                        'entry_time': pos['entry_time'].strftime('%H:%M'),
                        'entry_price': round(pos['entry_price'], 2),
                        'exit_time': bar_end_dt.strftime('%H:%M'),
                        'exit_price': round(exit_price, 2),
                        'exit_reason': exit_reason,
                        'gross_pnl': round(gross_pnl, 2),
                        'charges': charges,
                        'net_pnl': round(net_pnl, 2),
                        'r_multiple': round(gross_pnl / (pos['r_rupees'] * qty), 2) if pos['r_rupees'] > 0 else 0.0,
                    }
                    trades.append(trade_record)
                    session_trades.append(trade_record)
                    closed_syms.append(sym)
                    current_capital += net_pnl
                else:
                    # Update trailing stop
                    if pos['side'] == Side.BUY:
                        pos['best_favorable'] = max(pos['best_favorable'], s_high)
                        fav_excursion = pos['best_favorable'] - pos['entry_price']
                    else:
                        pos['best_favorable'] = min(pos['best_favorable'], s_low)
                        fav_excursion = pos['entry_price'] - pos['best_favorable']

                    current_r = fav_excursion / pos['r_rupees'] if pos['r_rupees'] > 0 else 0.0
                    if current_r >= config.stops.trail.activation_r:
                        pos['trail_activated'] = True

                    if pos['trail_activated']:
                        # Cost adjusted profit floor
                        rt_cost_per_share = (charges_est := (config.costs.brokerage_per_order * 2) / max(1, pos['quantity'])) + (pos['entry_price'] * 0.0003)
                        lock_floor = pos['entry_price'] + rt_cost_per_share + config.stops.trail.profit_lock_rupees if pos['side'] == Side.BUY else pos['entry_price'] - (rt_cost_per_share + config.stops.trail.profit_lock_rupees)
                        
                        atr_mult = config.stops.trail.atr_multiplier_tight if bar_time >= time(13, 30) else config.stops.trail.atr_multiplier_normal
                        atr_trail = pos['best_favorable'] - (atr_mult * pos['atr']) if pos['side'] == Side.BUY else pos['best_favorable'] + (atr_mult * pos['atr'])

                        if pos['side'] == Side.BUY:
                            candidate_stop = max(pos['current_stop'], lock_floor, atr_trail)
                            candidate_stop = min(candidate_stop, s_close - 0.10)
                            if candidate_stop > pos['current_stop'] + 0.10:
                                pos['current_stop'] = round(math.floor(candidate_stop / 0.05) * 0.05, 2)
                        else:
                            candidate_stop = min(pos['current_stop'], lock_floor, atr_trail)
                            candidate_stop = max(candidate_stop, s_close + 0.10)
                            if candidate_stop < pos['current_stop'] - 0.10:
                                pos['current_stop'] = round(math.ceil(candidate_stop / 0.05) * 0.05, 2)

            for cs in closed_syms:
                del open_positions[cs]

            # 2. Check for new entries (Only between 09:30 and 14:15 IST)
            if time(9, 30) <= bar_time <= time(14, 15):
                if len(open_positions) >= max_positions:
                    continue

                # Get all completed candles up to this bar (including historical warm-up)
                nifty_history_end_idx = nifty_df[nifty_df['Datetime'] <= current_dt].index[-1]
                # Slice last 100 bars for regime evaluation
                bench_slice = nifty_df.iloc[max(0, nifty_history_end_idx - 100): nifty_history_end_idx + 1]
                bench_candles = df_to_candles(bench_slice)

                regime_eval = evaluate_market_regime(bench_candles, config.regime)
                if regime_eval.regime == MarketRegime.NEUTRAL:
                    continue

                # Evaluate candidates
                for sym in watchlist_symbols:
                    if sym in open_positions or len(open_positions) >= max_positions:
                        continue

                    s_df = stock_dfs[sym]
                    stock_hist_end_idx = s_df[s_df['Datetime'] <= current_dt].index[-1]
                    s_sess_bars = stock_sessions[sym]
                    if current_dt not in stock_bars_by_time[sym]:
                        continue
                    bars_today = s_sess_bars[s_sess_bars['Datetime'] <= current_dt]
                    if len(bars_today) < 4:
                        continue

                    # Stock candles for signal (pass today's session bars for session VWAP & OR)
                    stock_slice = s_df.iloc[max(0, stock_hist_end_idx - 60): stock_hist_end_idx + 1]
                    stock_candles = df_to_candles(stock_slice)
                    # Note: in apex3 signal.py, VWAP is calculated on completed_candles.
                    # We pass the today session candles so VWAP resets at 09:15 as documented!
                    today_candles = df_to_candles(bars_today)

                    or_range = opening_ranges[sym]
                    if not or_range.is_complete:
                        continue

                    inst = InstrumentInfo(
                        symbol=sym,
                        upper_circuit=float(bars_today.iloc[0]['Open']) * 1.10,
                        lower_circuit=float(bars_today.iloc[0]['Open']) * 0.90,
                        tick_size=0.05,
                    )

                    last_bar = bars_today.iloc[-1]
                    quote = Quote(
                        symbol=sym,
                        exchange="NSE",
                        last_price=float(last_bar['Close']),
                        bid=float(last_bar['Close']) - 0.05,
                        ask=float(last_bar['Close']) + 0.05,
                        volume=int(last_bar['Volume']),
                        timestamp=bar_end_dt.to_pydatetime(),
                        is_fresh=True,
                    )

                    sig = evaluate_signal(
                        symbol=sym,
                        stock_candles_5m=today_candles,
                        benchmark_candles_5m=bench_candles,
                        opening_range=or_range,
                        market_regime=regime_eval.regime,
                        instrument=inst,
                        quote=quote,
                        config=config.signal,
                        is_entry_window=True,
                    )

                    if sig.decision in (Decision.PROPOSED_LONG, Decision.PROPOSED_SHORT):
                        # ATR calculation
                        highs = [c.high for c in stock_candles]
                        lows = [c.low for c in stock_candles]
                        closes = [c.close for c in stock_candles]
                        atr_series = compute_atr(highs, lows, closes, period=14)
                        atr_val = atr_series[-1] if atr_series else float(last_bar['Close']) * 0.01

                        entry_price = float(last_bar['Close'])
                        side = sig.side

                        initial_stop = calculate_initial_stop(
                            side=side,
                            entry_price=entry_price,
                            signal_low=float(last_bar['Low']),
                            signal_high=float(last_bar['High']),
                            atr_value=atr_val,
                            config=config.stops,
                            tick_size=inst.tick_size,
                        )

                        # Sizing
                        per_share_risk = abs(entry_price - initial_stop) + (entry_price * 0.001)
                        usable_margin = max(0.0, current_capital * (1.0 - safety_reserve_pct / 100.0))
                        num_alloc = max_positions - len(open_positions)
                        symbol_budget = (usable_margin * leverage) / max(1, num_alloc)

                        q_notional = math.floor(symbol_budget / entry_price)

                        if risk_mode == 'strict_config':
                            # 0.25% risk per trade on capital
                            rupee_risk = current_capital * (config.risk.risk_per_trade_pct / 100.0)
                            q_risk = math.floor(rupee_risk / per_share_risk) if per_share_risk > 0 else 0
                            qty = min(q_notional, q_risk)
                        elif risk_mode == 'one_pct_risk':
                            rupee_risk = current_capital * 0.01  # 1% risk per trade
                            q_risk = math.floor(rupee_risk / per_share_risk) if per_share_risk > 0 else 0
                            qty = min(q_notional, q_risk)
                        else:  # 'capital_allocated'
                            # Sized to fully utilize the 15k capital budget across qualified names
                            qty = q_notional

                        if qty > 0:
                            open_positions[sym] = {
                                'symbol': sym,
                                'side': side,
                                'entry_price': entry_price,
                                'initial_stop': initial_stop,
                                'current_stop': initial_stop,
                                'quantity': qty,
                                'entry_time': bar_end_dt,
                                'atr': atr_val,
                                'r_rupees': abs(entry_price - initial_stop),
                                'best_favorable': entry_price,
                                'trail_activated': False,
                            }

        # End of day session record
        daily_stats.append({
            'date': session_date.strftime('%Y-%m-%d'),
            'trades_count': len(session_trades),
            'day_pnl': sum(t['net_pnl'] for t in session_trades),
            'closing_capital': round(current_capital, 2),
        })

    return {
        'trades': trades,
        'daily_stats': daily_stats,
        'starting_capital': starting_capital,
        'ending_capital': round(current_capital, 2),
        'total_net_pnl': round(current_capital - starting_capital, 2),
        'profit_pct': round(((current_capital - starting_capital) / starting_capital) * 100.0, 2),
    }

if __name__ == '__main__':
    # Test with primary watchlist: RELIANCE, TCS, INFY
    res_alloc = run_backtest(['RELIANCE', 'TCS', 'INFY'], risk_mode='capital_allocated')
    print("CAPITAL ALLOCATED RESULT:")
    print(f"Total Trades: {len(res_alloc['trades'])}")
    print(f"Ending Capital: Rs. {res_alloc['ending_capital']}")
    print(f"Net P&L: Rs. {res_alloc['total_net_pnl']} ({res_alloc['profit_pct']}%)")
    for t in res_alloc['trades']:
        print(f"{t['date']} | {t['symbol']} | {t['side']} | Qty: {t['qty']} | Entry: {t['entry_price']} | Exit: {t['exit_price']} ({t['exit_reason']}) | Gross: Rs. {t['gross_pnl']} | Charges: Rs. {t['charges']['total_charges']} | Net: Rs. {t['net_pnl']}")
