import os
import math
import json
from datetime import datetime, time, date
import pandas as pd
import numpy as np

from apex3.config import load_config
from apex3.models import Candle, Quote, InstrumentInfo, OpeningRange, Side, Decision
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import evaluate_signal
from apex3.strategy.trail import calculate_initial_stop, calculate_target_price
from apex3.indicators.atr import compute_atr
from apex3.strategy.sizer import calculate_zerodha_intraday_charges
from backtest_3months import load_preparsed_data

def run_quant_backtest(
    symbols: list[str],
    target_r: float = 2.0,
    max_slots: int = 2,
    max_new_per_bar: int = 1,
    min_r_pct: float = 0.0045,
    max_r_pct: float = 0.0085,
    max_adr_consumed: float = 0.60,
    min_atr_pct: float = 0.0022,
    second_slot_req_r: float = 0.50,
    fee_lock_enabled: bool = True,
    fee_lock_trigger_r: float = 1.65,
    late_lock_trigger_r: float = 0.75,
    cutoff_time_str: str = "13:00",
):
    config = load_config()
    all_syms = ['NSEI'] + [s for s in symbols if s != 'NSEI']
    print(f"Loading data for {len(all_syms)} symbols...")
    data = load_preparsed_data(all_syms)
    print("Data loaded successfully!")

    nifty_candles = data['NSEI']['candles']
    nifty_df = data['NSEI']['df']

    dates = sorted(list(set(nifty_df['Datetime'].dt.date)))
    test_dates = dates[3:]

    starting_capital = 15000.0
    current_capital = starting_capital
    leverage = 4.0
    safety_reserve_pct = 5.0
    usable_margin = starting_capital * (1.0 - safety_reserve_pct / 100.0) # 14,250
    slot_budget = (usable_margin * leverage) / max_slots # 28,500
    risk_per_trade_cap = starting_capital * 0.0080 # 120.0 INR
    daily_loss_limit = -1.0 * starting_capital * 0.0160 # -240.0 INR

    # Precalculate 14-day ADR for each stock
    stock_adr = {}
    for sym in symbols:
        sdf = data[sym]['df'].copy()
        sdf['date'] = sdf['Datetime'].dt.date
        d_stats = sdf.groupby('date').agg(
            day_high=('High', 'max'),
            day_low=('Low', 'min'),
            day_close=('Close', 'last')
        ).reset_index()
        d_stats['day_range_pct'] = (d_stats['day_high'] - d_stats['day_low']) / d_stats['day_close']
        d_stats['adr14_pct'] = d_stats['day_range_pct'].rolling(14, min_periods=1).mean()
        stock_adr[sym] = dict(zip(d_stats['date'], d_stats['adr14_pct']))

    cutoff_t = time(13, 0)
    trades = []
    daily_stats = []

    for session_date in test_dates:
        n_sess_mask = (nifty_df['Datetime'].dt.date == session_date)
        sess_indices = nifty_df[n_sess_mask].index.tolist()
        if len(sess_indices) < 6:
            continue

        opening_ranges = {}
        stock_sess_map = {}
        for sym in symbols:
            s_candles = data[sym]['candles']
            s_df = data[sym]['df']
            s_indices = s_df[s_df['Datetime'].dt.date == session_date].index.tolist()
            stock_sess_map[sym] = s_indices

            or_indices = [idx for idx in s_indices if s_candles[idx].timestamp.time() < time(9, 30)]
            if len(or_indices) >= 3:
                or_high = max(s_candles[i].high for i in or_indices)
                or_low = min(s_candles[i].low for i in or_indices)
                opening_ranges[sym] = OpeningRange(symbol=sym, high=or_high, low=or_low, is_complete=True, candle_count=len(or_indices))
            else:
                opening_ranges[sym] = OpeningRange(symbol=sym, high=0.0, low=0.0, is_complete=False)

        open_positions = {}
        session_trades = []
        day_realized_net = 0.0

        for step, n_idx in enumerate(sess_indices):
            c_bar = nifty_candles[n_idx]
            current_dt = c_bar.timestamp
            bar_time = current_dt.time()

            # 1. Manage Open Positions
            closed_syms = []
            for sym, pos in open_positions.items():
                s_indices = stock_sess_map[sym]
                if step >= len(s_indices):
                    continue
                s_idx = s_indices[step]
                s_bar = data[sym]['candles'][s_idx]
                s_high = s_bar.high
                s_low = s_bar.low
                s_close = s_bar.close

                # Update unrealized R & fee lock check
                if pos['side'] == Side.BUY:
                    unrealized_r = (s_close - pos['entry_price']) / pos['r_rupees']
                else:
                    unrealized_r = (pos['entry_price'] - s_close) / pos['r_rupees']
                pos['unrealized_r'] = unrealized_r

                # Fee-protected breakeven step
                if fee_lock_enabled and not pos['fee_lock_applied']:
                    should_lock = False
                    if unrealized_r >= fee_lock_trigger_r:
                        should_lock = True
                    elif bar_time >= time(13, 45) and unrealized_r >= late_lock_trigger_r:
                        should_lock = True
                    
                    if should_lock:
                        pos['fee_lock_applied'] = True
                        if pos['side'] == Side.BUY:
                            lock_stop = round(math.floor((pos['entry_price'] * 1.0015) / 0.05) * 0.05, 2)
                            if lock_stop > pos['current_stop']:
                                pos['current_stop'] = lock_stop
                        else:
                            lock_stop = round(math.ceil((pos['entry_price'] * 0.9985) / 0.05) * 0.05, 2)
                            if lock_stop < pos['current_stop']:
                                pos['current_stop'] = lock_stop

                is_stopped = False
                exit_price = pos['current_stop']
                exit_reason = "STOP_LOSS"

                # Target check
                if pos['side'] == Side.BUY and s_high >= pos['target_price']:
                    is_stopped = True
                    exit_price = pos['target_price']
                    exit_reason = f"TARGET_HIT_{target_r}R"
                elif pos['side'] == Side.SELL and s_low <= pos['target_price']:
                    is_stopped = True
                    exit_price = pos['target_price']
                    exit_reason = f"TARGET_HIT_{target_r}R"

                # Stop check
                if not is_stopped:
                    if pos['side'] == Side.BUY and s_low <= pos['current_stop']:
                        is_stopped = True
                        exit_price = min(pos['current_stop'], s_bar.open)
                        exit_reason = "FEE_LOCK_STOP" if pos['fee_lock_applied'] else "STRUCTURAL_STOP"
                    elif pos['side'] == Side.SELL and s_high >= pos['current_stop']:
                        is_stopped = True
                        exit_price = max(pos['current_stop'], s_bar.open)
                        exit_reason = "FEE_LOCK_STOP" if pos['fee_lock_applied'] else "STRUCTURAL_STOP"

                # EOD check (14:55)
                if not is_stopped and bar_time >= time(14, 55):
                    is_stopped = True
                    exit_price = s_close
                    exit_reason = "EOD_FLATTEN_1455"

                if is_stopped:
                    buy_p = pos['entry_price'] if pos['side'] == Side.BUY else exit_price
                    sell_p = exit_price if pos['side'] == Side.BUY else pos['entry_price']
                    qty = pos['quantity']
                    gross_pnl = (sell_p - buy_p) * qty
                    charges_val = calculate_zerodha_intraday_charges(buy_p, sell_p, qty)
                    net_pnl = round(gross_pnl - charges_val, 2)

                    trade_record = {
                        'date': session_date.strftime('%Y-%m-%d'),
                        'symbol': sym,
                        'side': pos['side'].value,
                        'qty': qty,
                        'entry_time': pos['entry_time'].strftime('%H:%M'),
                        'entry_price': round(pos['entry_price'], 2),
                        'exit_time': current_dt.strftime('%H:%M'),
                        'exit_price': round(exit_price, 2),
                        'exit_reason': exit_reason,
                        'gross_pnl': round(gross_pnl, 2),
                        'charges': charges_val,
                        'net_pnl': net_pnl,
                        'r_multiple': round(gross_pnl / (pos['r_rupees'] * qty), 2) if pos['r_rupees'] > 0 else 0.0,
                        'conviction_score': pos['conviction_score'],
                    }
                    trades.append(trade_record)
                    session_trades.append(trade_record)
                    closed_syms.append(sym)
                    day_realized_net += net_pnl
                    current_capital += net_pnl

            for cs in closed_syms:
                del open_positions[cs]

            # 2. Entry Checks
            if not (time(9, 30) <= bar_time <= cutoff_t):
                continue

            if day_realized_net <= daily_loss_limit:
                continue

            if len(open_positions) >= max_slots:
                continue

            # Second slot unlock gate: requires slot 1 in profit >= +0.50R
            if len(open_positions) == 1:
                first_pos = next(iter(open_positions.values()))
                if first_pos.get('unrealized_r', 0.0) < second_slot_req_r:
                    continue

            bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
            regime_eval = evaluate_market_regime(bench_slice, config.regime)
            if regime_eval.regime == MarketRegime.NEUTRAL:
                continue

            # Scan all symbols for candidates
            bar_candidates = []
            for sym in symbols:
                if sym in open_positions:
                    continue
                s_indices = stock_sess_map[sym]
                if step >= len(s_indices) or step < 3:
                    continue

                s_idx = s_indices[step]
                s_bar = data[sym]['candles'][s_idx]
                today_candles = [data[sym]['candles'][i] for i in s_indices[:step + 1]]

                or_range = opening_ranges[sym]
                if not or_range.is_complete:
                    continue

                first_open = data[sym]['candles'][s_indices[0]].open
                inst = InstrumentInfo(
                    symbol=sym,
                    upper_circuit=first_open * 1.10,
                    lower_circuit=first_open * 0.90,
                    tick_size=0.05,
                )
                quote = Quote(
                    symbol=sym,
                    exchange="NSE",
                    last_price=s_bar.close,
                    bid=s_bar.close - 0.05,
                    ask=s_bar.close + 0.05,
                    volume=s_bar.volume,
                    timestamp=current_dt,
                    is_fresh=True,
                )

                sig = evaluate_signal(
                    symbol=sym,
                    stock_candles_5m=today_candles,
                    benchmark_candles_5m=bench_slice,
                    opening_range=or_range,
                    market_regime=regime_eval.regime,
                    instrument=inst,
                    quote=quote,
                    config=config.signal,
                    is_entry_window=True,
                )

                if sig.decision in (Decision.PROPOSED_LONG, Decision.PROPOSED_SHORT):
                    stock_slice = data[sym]['candles'][max(0, s_idx - 60): s_idx + 1]
                    highs = [c.high for c in stock_slice]
                    lows = [c.low for c in stock_slice]
                    closes = [c.close for c in stock_slice]
                    atr_series = compute_atr(highs, lows, closes, period=14)
                    atr_val = atr_series[-1] if atr_series else s_bar.close * 0.01

                    # Gate 9A: Min ATR check
                    if s_bar.close <= 0 or (atr_val / s_bar.close) < min_atr_pct:
                        continue

                    entry_price = s_bar.close
                    side = sig.side

                    # Structural Stop Anchor (Swing 5 +/- 1.5 ATR)
                    swing_lookback = min(5, len(today_candles))
                    swing_slice = today_candles[-swing_lookback:]
                    swing_low = min(c.low for c in swing_slice)
                    swing_high = max(c.high for c in swing_slice)

                    pad = 1.5 * atr_val
                    if side == Side.BUY:
                        raw_stop = swing_low - pad
                        initial_stop = round(math.floor(raw_stop / 0.05) * 0.05, 2)
                        if initial_stop >= entry_price:
                            continue
                    else:
                        raw_stop = swing_high + pad
                        initial_stop = round(math.ceil(raw_stop / 0.05) * 0.05, 2)
                        if initial_stop <= entry_price:
                            continue

                    r_dist = abs(entry_price - initial_stop)
                    r_pct = r_dist / entry_price

                    # Gate 9B: Stop-width sweet spot (0.45% - 0.85%)
                    if not (min_r_pct <= r_pct <= max_r_pct):
                        continue

                    # Gate 9C: ADR exhaustion check
                    adr_val = stock_adr[sym].get(session_date, 0.025)
                    sess_candles_so_far = [data[sym]['candles'][i] for i in s_indices[:step + 1]]
                    day_h_so_far = max(c.high for c in sess_candles_so_far)
                    day_l_so_far = min(c.low for c in sess_candles_so_far)
                    if day_l_so_far > 0 and adr_val > 0:
                        consumed_pct = ((day_h_so_far - day_l_so_far) / day_l_so_far) / adr_val
                        if consumed_pct > max_adr_consumed:
                            continue

                    # Position Sizing: Q = min(Q_risk, Q_budget)
                    q_risk = math.floor(risk_per_trade_cap / r_dist) if r_dist > 0 else 0
                    q_budget = math.floor(slot_budget / entry_price)
                    qty = min(q_risk, q_budget)

                    if qty <= 0:
                        continue

                    tgt_price = calculate_target_price(side, entry_price, initial_stop, target_r=target_r, tick_size=0.05)
                    est_charges = calculate_zerodha_intraday_charges(entry_price, tgt_price, qty)
                    expected_win = abs(tgt_price - entry_price) * qty
                    # Friction Gate: charges must be <= 18% of expected gross profit
                    if expected_win <= 0 or (est_charges / expected_win) > 0.18:
                        continue

                    # Conviction Score = |RS| * 100 * Vol_Ratio
                    rs_val = sig.relative_strength
                    vol_val = sig.relative_volume
                    conviction = abs(rs_val) * 100.0 * vol_val

                    bar_candidates.append({
                        'symbol': sym,
                        'side': side,
                        'entry_price': entry_price,
                        'initial_stop': initial_stop,
                        'target_price': tgt_price,
                        'quantity': qty,
                        'entry_time': current_dt,
                        'atr': atr_val,
                        'r_rupees': r_dist,
                        'conviction_score': round(conviction, 2),
                    })

            if not bar_candidates:
                continue

            # Sort by conviction score descending and select top max_new_per_bar (1)
            bar_candidates.sort(key=lambda x: x['conviction_score'], reverse=True)
            for cand in bar_candidates[:max_new_per_bar]:
                if len(open_positions) >= max_slots:
                    break
                open_positions[cand['symbol']] = {
                    'symbol': cand['symbol'],
                    'side': cand['side'],
                    'entry_price': cand['entry_price'],
                    'initial_stop': cand['initial_stop'],
                    'current_stop': cand['initial_stop'],
                    'target_price': cand['target_price'],
                    'quantity': cand['quantity'],
                    'entry_time': cand['entry_time'],
                    'atr': cand['atr'],
                    'r_rupees': cand['r_rupees'],
                    'conviction_score': cand['conviction_score'],
                    'fee_lock_applied': False,
                    'unrealized_r': 0.0,
                }

        daily_stats.append({
            'date': session_date.strftime('%Y-%m-%d'),
            'trades_count': len(session_trades),
            'day_pnl': sum(t['net_pnl'] for t in session_trades),
            'closing_capital': round(current_capital, 2),
        })

    wins = [t for t in trades if t['net_pnl'] > 0]
    losses = [t for t in trades if t['net_pnl'] < 0]
    be = [t for t in trades if t['net_pnl'] == 0]
    gross_win = sum(t['gross_pnl'] for t in trades if t['gross_pnl'] > 0)
    gross_loss = sum(abs(t['gross_pnl']) for t in trades if t['gross_pnl'] < 0)
    gross_pnl = sum(t['gross_pnl'] for t in trades)
    tot_charges = sum(t['charges'] for t in trades)
    net_pnl = gross_pnl - tot_charges
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 999.0
    fee_ratio = (tot_charges / gross_win * 100.0) if gross_win > 0 else 0.0

    return {
        'total_trades': len(trades),
        'winning_trades': len(wins),
        'losing_trades': len(losses),
        'breakeven_trades': len(be),
        'win_rate': round(win_rate, 2),
        'gross_pnl': round(gross_pnl, 2),
        'gross_profit': round(gross_win, 2),
        'gross_loss': round(gross_loss, 2),
        'profit_factor': round(profit_factor, 2),
        'total_charges': round(tot_charges, 2),
        'net_pnl': round(net_pnl, 2),
        'profit_pct': round((net_pnl / starting_capital) * 100.0, 2),
        'ending_capital': round(current_capital, 2),
        'fee_ratio': round(fee_ratio, 2),
        'trades': trades,
        'daily_stats': daily_stats,
    }

if __name__ == '__main__':
    elite_20 = [
        'RELIANCE', 'HDFCBANK', 'ICICIBANK', 'SBIN', 'AXISBANK',
        'KOTAKBANK', 'BAJFINANCE', 'INFY', 'TCS', 'MARUTI',
        'M&M', 'BAJAJ-AUTO', 'TATASTEEL', 'HINDALCO', 'JSWSTEEL',
        'LT', 'SUNPHARMA', 'BEL', 'HAL', 'TRENT', 'BHARTIARTL', 'ITC'
    ]
    res = run_quant_backtest(elite_20)
    print("\n" + "=" * 60)
    print("APEX-3 3-MONTH CONVICTION BACKTEST ON ELITE 20 UNIVERSE")
    print("=" * 60)
    print(f"Total Trades: {res['total_trades']}")
    print(f"Win Rate: {res['win_rate']}% ({res['winning_trades']} wins, {res['losing_trades']} losses, {res['breakeven_trades']} breakeven)")
    print(f"Gross P&L: Rs. {res['gross_pnl']:,.2f}")
    print(f"Total Zerodha Charges: Rs. {res['total_charges']:,.2f} ({res['fee_ratio']}% friction)")
    print(f"Net Realized P&L: Rs. {res['net_pnl']:,.2f} (+{res['profit_pct']}%)")
    print(f"Ending Capital: Rs. {res['ending_capital']:,.2f}")
    print(f"Profit Factor: {res['profit_factor']}")
    print("=" * 60)
