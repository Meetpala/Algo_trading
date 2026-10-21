import os
import sys
sys.path.insert(0, '.')
import math
import json
from datetime import datetime, time, timedelta
import pandas as pd

from apex3.config import load_config
from apex3.models import Candle, Quote, InstrumentInfo, OpeningRange, Side, Decision
from apex3.strategy.engine import build_active_radar
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import evaluate_signal
from apex3.strategy.trail import calculate_target_price, compute_initial_stop_and_target
from apex3.indicators.atr import compute_atr
from apex3.indicators.vwap import compute_session_vwap
from backtest_engine import calculate_charges
from backtest_3months import load_preparsed_data, friction_ratio_pct

def run_simulation(session_dates, name="RUN", target_r=2.0):
    config = load_config()
    watchlist = build_active_radar(config.raw_dict)
    all_symbols = ['NSEI'] + watchlist
    data = load_preparsed_data(all_symbols)
    
    nifty_df = data['NSEI']['df']
    nifty_candles = data['NSEI']['candles']
    
    starting_capital = 15000.0
    current_capital = starting_capital
    leverage = 4.0
    safety_reserve_pct = 5.0
    max_positions = 2
    usable_margin = starting_capital * (1.0 - safety_reserve_pct / 100.0)
    slot_budget = (usable_margin * leverage) / max_positions
    risk_cap = starting_capital * 0.0080  # Rs. 120.00
    daily_stop = -1.0 * starting_capital * 0.0160  # -Rs. 240.00
    cutoff_time = time(12, 30)
    
    trades = []
    
    for session_date in session_dates:
        n_sess_mask = (nifty_df['Datetime'].dt.date == session_date)
        sess_indices = nifty_df[n_sess_mask].index.tolist()
        if len(sess_indices) < 6:
            continue
            
        opening_ranges = {}
        stock_sess_map = {}
        stock_sess_indices = {}
        for sym in watchlist:
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
        day_realized = 0.0
        daily_halted = False
        
        for step, n_idx in enumerate(sess_indices):
            c_bar = nifty_candles[n_idx]
            current_dt = c_bar.timestamp
            bar_time = current_dt.time()
            bar_close_dt = current_dt + timedelta(minutes=5)
            
            if day_realized <= daily_stop:
                daily_halted = True
                
            # 1. Fill pending entry on next-bar open (bar t+1 open)
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
                
                if can_fill and len(open_positions) == 1:
                    first = next(iter(open_positions.values()))
                    first_bar_idx = stock_sess_map[first['symbol']].get(current_dt)
                    if first_bar_idx is None:
                        can_fill = False
                    else:
                        first_bar = data[first['symbol']]['candles'][first_bar_idx]
                        if first['side'] == Side.BUY:
                            open_r = (first_bar.open - first['entry_price']) / first['r_rupees']
                        else:
                            open_r = (first['entry_price'] - first_bar.open) / first['r_rupees']
                        if open_r < 0.50:
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
                                
            # 2. Manage open positions
            closed_syms = []
            for sym, pos in open_positions.items():
                s_idx = stock_sess_map[sym].get(current_dt)
                if s_idx is None:
                    continue
                s_bar = data[sym]['candles'][s_idx]
                r_dist = pos['r_rupees']
                
                # Check exits: stop loss first if both touched
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
                    trades.append({
                        'date': session_date.strftime('%Y-%m-%d'),
                        'symbol': sym,
                        'side': pos['side'].value,
                        'qty': qty,
                        'entry_time': pos['entry_time'].strftime('%H:%M'),
                        'entry_price': pos['entry_price'],
                        'exit_time': current_dt.strftime('%H:%M'),
                        'exit_price': round(exit_price, 2),
                        'exit_reason': exit_reason,
                        'gross_pnl': round(gross_pnl, 2),
                        'charges': charges,
                        'total_charges': charges['total_charges'],
                        'net_pnl': net_pnl,
                        'r_multiple': round(gross_pnl / (pos['r_rupees'] * qty), 2) if pos['r_rupees'] > 0 else 0.0,
                    })
                    closed_syms.append(sym)
                    day_realized += net_pnl
                    current_capital += net_pnl
                else:
                    # Update fee-lock for NEXT bar
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
                
            # 3. Signal evaluation on completed candle
            next_n_idx = sess_indices[step + 1] if step + 1 < len(sess_indices) else None
            if (
                next_n_idx is not None
                and not daily_halted
                and len(open_positions) < max_positions
                and time(9, 35) <= bar_close_dt.time() <= cutoff_time
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
                for sym in watchlist:
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
                    inst = InstrumentInfo(symbol=sym, upper_circuit=first_open * 1.10, lower_circuit=first_open * 0.90, tick_size=0.05)
                    quote = Quote(symbol=sym, exchange="NSE", last_price=s_bar.close, bid=s_bar.close - 0.05, ask=s_bar.close + 0.05, volume=s_bar.volume, timestamp=current_dt, is_fresh=True)
                    
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
                        history_start = max(0, s_idx - 60)
                        stock_history = data[sym]['candles'][history_start:s_idx + 1]
                        atr_series = compute_atr([c.high for c in stock_history], [c.low for c in stock_history], [c.close for c in stock_history], period=14)
                        atr_val = atr_series[-1] if atr_series else s_bar.close * 0.01
                        lookback_bars = config.stops.swing_lookback_bars if hasattr(config.stops, 'swing_lookback_bars') else 5
                        swing_bars = stock_history[-lookback_bars:]
                        sw_low = min(c.low for c in swing_bars)
                        sw_high = max(c.high for c in swing_bars)
                        
                        # Check stop-width on signal close
                        side = sig.side
                        levels = compute_initial_stop_and_target(
                            side=side,
                            entry_price=s_bar.close,
                            swing_low_5=sw_low,
                            swing_high_5=sw_high,
                            atr14=atr_val,
                            cfg=config.raw_dict,
                        )
                        if 0.0055 <= levels.risk_pct <= 0.0090:
                            conviction = abs(sig.relative_strength) * 100.0 * sig.relative_volume
                            cands.append({
                                'symbol': sym,
                                'side': side,
                                'signal_price': s_bar.close,
                                'signal_time': bar_close_dt,
                                'fill_time': next_fill_dt,
                                'swing_low_5': sw_low,
                                'swing_high_5': sw_high,
                                'atr': atr_val,
                                'conviction': conviction,
                            })
                if cands:
                    cands.sort(key=lambda x: x['conviction'], reverse=True)
                    pending_entry = cands[0]
                    
    wins = [t for t in trades if t['net_pnl'] > 0]
    losses = [t for t in trades if t['net_pnl'] < 0]
    gw = sum(t['gross_pnl'] for t in trades if t['gross_pnl'] > 0)
    gl = sum(abs(t['gross_pnl']) for t in trades if t['gross_pnl'] < 0)
    gp = sum(t['gross_pnl'] for t in trades)
    tc = sum(t['charges']['total_charges'] for t in trades)
    npnl = round(gp - tc, 2)
    wr = len(wins) / len(trades) * 100.0 if trades else 0.0
    pf = (gw / gl) if gl > 0 else 999.0
    fric_win = (tc / gw * 100.0) if gw > 0 else 0.0
    targets = len([t for t in trades if t['exit_reason'] == 'TARGET_HIT'])
    avg_win = gw / len(wins) if wins else 0.0
    print(f'=== {name} (Target: {target_r}R) ===')
    print(f'Trades: {len(trades)} | Wins: {len(wins)} (TargetHits: {targets}) | Losses: {len(losses)} | WinRate: {wr:.2f}%')
    print(f'Gross PnL: Rs. {gp:+.2f} | Gross Wins: Rs. {gw:.2f} (Avg: Rs. {avg_win:.2f}) | Gross Loss: Rs. {gl:.2f}')
    print(f'Charges: Rs. {tc:.2f} | Net PnL: Rs. {npnl:+.2f} | Profit Factor: {pf:.2f}')
    print(f'Charges/GrossWin: {fric_win:.2f}%')
    return {
        'trades': trades,
        'net_pnl': npnl,
        'win_rate': wr,
        'profit_factor': pf,
        'friction_ratio': fric_win,
    }

if __name__ == '__main__':
    config = load_config()
    watchlist = build_active_radar(config.raw_dict)
    all_symbols = ['NSEI'] + watchlist
    data = load_preparsed_data(all_symbols)
    dates = sorted(list(set(data['NSEI']['df']['Datetime'].dt.date)))
    dates_3m = dates[-57:]
    dates_h1 = dates[:len(dates)-57]
    
    print("\n--- TESTING TARGET 2.0R ---")
    res_3m = run_simulation(dates_3m, 'SECOND HALF (3 MONTHS)', 2.0)
    res_h1 = run_simulation(dates_h1, 'FIRST HALF (3 MONTHS)', 2.0)
    res_6m = run_simulation(dates, 'FULL 6 MONTHS', 2.0)
