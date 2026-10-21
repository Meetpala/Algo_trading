import os, sys
sys.path.insert(0, os.getcwd())
import pandas as pd
from datetime import time, timedelta
import backtest_3months

config = backtest_3months.load_config()
watchlist = backtest_3months.build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = backtest_3months.load_preparsed_data(all_symbols)
three_month_dates = sorted(set(data['NSEI']['df']['Datetime'].dt.date))[-57:]

print(f"Testing 3-month dataset with Tasks 1-4 requirements...")

# Let's test with evaluate_signal and the 5 tasks
# Task 1: Next-bar marketable limit fill (next_bar.open +/- 0.05) with 0.15% slippage filter
# Task 1: Conservative intrabar stop/target resolution (stop loss first if both breached)
# Task 2: 5-bar swing anchor + 1.5x ATR
# Task 3: Dual EMA on Nifty (done in regime.py), Cushion 0.15*ATR + CLV >= 0.70 (done in signal.py), 09:35 - 12:30 entry window
# Task 4: Stop width [0.0055, 0.0090], Fee lock at 14:15 for >= 1.15R, or 1.65R anytime

def run_test_sim(target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0090, dates=three_month_dates):
    nifty_df = data['NSEI']['df']
    nifty_candles = data['NSEI']['candles']
    
    starting_capital = 15000.0
    current_capital = starting_capital
    leverage = 4.0
    safety_reserve_pct = 5.0
    max_positions = 2
    usable_margin = starting_capital * (1.0 - safety_reserve_pct / 100.0)
    slot_budget = (usable_margin * leverage) / max_positions
    risk_cap = starting_capital * 0.0080
    daily_stop = -1.0 * starting_capital * 0.0160
    
    trades = []
    
    for session_date in dates:
        n_sess_mask = (nifty_df['Datetime'].dt.date == session_date)
        sess_indices = nifty_df[n_sess_mask].index.tolist()
        if len(sess_indices) < 6: continue
        
        # Opening ranges
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
                opening_ranges[sym] = backtest_3months.OpeningRange(symbol=sym, high=or_high, low=or_low, is_complete=True, candle_count=len(or_indices))
            else:
                opening_ranges[sym] = backtest_3months.OpeningRange(symbol=sym, high=0.0, low=0.0, is_complete=False)

        open_positions = {}
        pending_entry = None
        day_realized = 0.0
        daily_halted = False
        
        for step, n_idx in enumerate(sess_indices):
            c_bar = nifty_candles[n_idx]
            current_dt = c_bar.timestamp
            bar_time = current_dt.time()
            bar_close_dt = current_dt + timedelta(minutes=5)
            
            # Daily loss check
            if day_realized <= daily_stop:
                daily_halted = True
                
            # Process pending entry from previous completed candle
            if pending_entry is not None:
                pending = pending_entry
                pending_entry = None
                sym = pending['symbol']
                s_idx = stock_sess_map[sym].get(current_dt)
                
                can_fill = (
                    s_idx is not None
                    and not daily_halted
                    and len(open_positions) < max_positions
                    and bar_time <= time(12, 30)
                )
                
                if can_fill and len(open_positions) == 1:
                    first = next(iter(open_positions.values()))
                    first_bar_idx = stock_sess_map[first['symbol']].get(current_dt)
                    if first_bar_idx is not None:
                        first_open = data[first['symbol']]['candles'][first_bar_idx].open
                        can_fill = backtest_3months.slot_two_unlocked_at_open(
                            first['side'], entry_price=first['entry_price'], open_price=first_open,
                            stop_price=first['current_stop'], risk_per_share=first['r_rupees']
                        )
                    else:
                        can_fill = False

                if can_fill:
                    s_bar = data[sym]['candles'][s_idx]
                    # Check 0.15% max slippage from signal close
                    if abs(s_bar.open - pending['signal_price']) / pending['signal_price'] <= 0.0015:
                        side = pending['side']
                        # 1 tick adverse half-spread
                        entry_price = round(s_bar.open + 0.05 if side == backtest_3months.Side.BUY else s_bar.open - 0.05, 2)
                        
                        # 5-bar structural swing + 1.5x ATR
                        from apex3.strategy.trail import compute_initial_stop_and_target
                        levels = compute_initial_stop_and_target(
                            side=side, entry_price=entry_price,
                            swing_low_5=pending['swing_low_5'], swing_high_5=pending['swing_high_5'],
                            atr14=pending['atr'], cfg=config.raw_dict
                        )
                        target_price = round(entry_price + (target_r * levels.risk_per_share) if side == backtest_3months.Side.BUY else entry_price - (target_r * levels.risk_per_share), 2)
                        
                        # Stop width floor check [min_stop_pct, max_stop_pct]
                        if min_stop_pct <= levels.risk_pct <= max_stop_pct:
                            # Sizing
                            r_dist = levels.risk_per_share
                            qty = min(int(risk_cap // r_dist), int(slot_budget // entry_price))
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

            # Position management: stop, target, fee-lock, flatten
            closed_syms = []
            for sym, pos in open_positions.items():
                s_idx = stock_sess_map[sym].get(current_dt)
                if s_idx is None: continue
                s_bar = data[sym]['candles'][s_idx]
                
                # Check intrabar resolution (conservative: stop loss first if both touched)
                exit_price = None
                exit_reason = None
                
                if bar_time >= time(14, 55):
                    exit_price = s_bar.close
                    exit_reason = "EOD_FLATTEN"
                else:
                    hit_stop = (s_bar.low <= pos['current_stop']) if pos['side'] == backtest_3months.Side.BUY else (s_bar.high >= pos['current_stop'])
                    hit_target = (s_bar.high >= pos['target_price']) if pos['side'] == backtest_3months.Side.BUY else (s_bar.low <= pos['target_price'])
                    
                    if hit_stop and hit_target:
                        # Conservative resolution: STOP-LOSS FIRST
                        exit_price = pos['current_stop']
                        exit_reason = "FEE_LOCK_STOP" if pos['fee_lock_applied'] else "STOP_LOSS"
                    elif hit_stop:
                        exit_price = pos['current_stop']
                        exit_reason = "FEE_LOCK_STOP" if pos['fee_lock_applied'] else "STOP_LOSS"
                    elif hit_target:
                        exit_price = pos['target_price']
                        exit_reason = "TARGET_HIT"
                    else:
                        # Check fee lock step
                        curr_r = (s_bar.close - pos['entry_price']) / pos['r_rupees'] if pos['side'] == backtest_3months.Side.BUY else (pos['entry_price'] - s_bar.close) / pos['r_rupees']
                        pos['unrealized_r'] = curr_r
                        
                        # Lock trigger: 1.65R anytime, or >= 1.15R at >= 14:15
                        if not pos['fee_lock_applied']:
                            if curr_r >= 1.65 or (bar_close_dt.time() >= time(14, 15) and curr_r >= 1.15):
                                offset = 0.0015 * pos['entry_price']
                                pos['current_stop'] = round(pos['entry_price'] + offset if pos['side'] == backtest_3months.Side.BUY else pos['entry_price'] - offset, 2)
                                pos['fee_lock_applied'] = True

                if exit_price is not None:
                    # Execute exit with 4 bps spread
                    exit_side = backtest_3months.Side.SELL if pos['side'] == backtest_3months.Side.BUY else backtest_3months.Side.BUY
                    actual_exit_px = backtest_3months.apply_adverse_spread(exit_price, exit_side, 4.0)
                    buy_p = pos['entry_price'] if pos['side'] == backtest_3months.Side.BUY else actual_exit_px
                    sell_p = actual_exit_px if pos['side'] == backtest_3months.Side.BUY else pos['entry_price']
                    qty = pos['quantity']
                    gross_pnl = (sell_p - buy_p) * qty
                    charges = backtest_3months.calculate_charges(buy_p, sell_p, qty, config.costs)
                    net_pnl = round(gross_pnl - charges['total_charges'], 2)
                    
                    trade = {
                        'date': session_date.strftime('%Y-%m-%d'),
                        'symbol': sym,
                        'side': pos['side'].value,
                        'qty': qty,
                        'entry_price': pos['entry_price'],
                        'exit_price': actual_exit_px,
                        'exit_reason': exit_reason,
                        'gross_pnl': round(gross_pnl, 2),
                        'charges': charges['total_charges'],
                        'net_pnl': net_pnl,
                        'r_multiple': round(gross_pnl / (pos['r_rupees'] * qty), 2) if pos['r_rupees'] > 0 else 0.0
                    }
                    trades.append(trade)
                    closed_syms.append(sym)
                    day_realized += net_pnl
                    current_capital += net_pnl
                    
            for cs in closed_syms:
                del open_positions[cs]
                
            # Signal scanning on completed bar
            next_n_idx = sess_indices[step + 1] if step + 1 < len(sess_indices) else None
            if (
                next_n_idx is not None
                and not daily_halted
                and len(open_positions) < max_positions
                and bar_close_dt.time() >= time(9, 35)
                and bar_close_dt.time() <= time(12, 30)
            ):
                if len(open_positions) == 1:
                    first_p = next(iter(open_positions.values()))
                    if first_p.get('unrealized_r', 0.0) < 0.50:
                        continue
                        
                bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
                from apex3.strategy.regime import evaluate_market_regime, MarketRegime
                regime_eval = evaluate_market_regime(bench_slice, config.regime)
                if regime_eval.regime == MarketRegime.NEUTRAL:
                    continue
                    
                cands = []
                next_fill_dt = nifty_candles[next_n_idx].timestamp
                for sym in watchlist:
                    if sym in open_positions: continue
                    s_idx = stock_sess_map[sym].get(current_dt)
                    next_s_idx = stock_sess_map[sym].get(next_fill_dt)
                    if s_idx is None or next_s_idx is None: continue
                    s_bar = data[sym]['candles'][s_idx]
                    today_indices = [idx for idx in stock_sess_indices[sym] if data[sym]['candles'][idx].timestamp <= current_dt]
                    today_candles = [data[sym]['candles'][idx] for idx in today_indices]
                    if len(today_candles) < 5: continue
                    or_range = opening_ranges[sym]
                    if not or_range.is_complete: continue
                    first_open = data[sym]['candles'][stock_sess_indices[sym][0]].open
                    
                    # Canonical evaluate_signal from apex3.strategy.signal
                    from apex3.models import InstrumentInfo, Quote
                    inst = InstrumentInfo(symbol=sym, exchange="NSE", lot_size=1, tick_size=0.05, upper_circuit=first_open*1.10, lower_circuit=first_open*0.90)
                    quote = Quote(symbol=sym, exchange="NSE", last_price=s_bar.close, bid=s_bar.close-0.05, ask=s_bar.close+0.05, volume=s_bar.volume, timestamp=current_dt, is_fresh=True)
                    
                    from apex3.strategy.signal import evaluate_signal, Decision
                    sig = evaluate_signal(
                        symbol=sym, stock_candles_5m=today_candles, benchmark_candles_5m=bench_slice,
                        opening_range=or_range, market_regime=regime_eval.regime, instrument=inst,
                        quote=quote, config=config.signal, is_entry_window=True
                    )
                    
                    if sig.decision in (Decision.PROPOSED_LONG, Decision.PROPOSED_SHORT):
                        history_start = max(0, s_idx - 60)
                        stock_history = data[sym]['candles'][history_start:s_idx + 1]
                        from apex3.indicators.atr import compute_atr
                        atr_series = compute_atr([c.high for c in stock_history], [c.low for c in stock_history], [c.close for c in stock_history], period=14)
                        atr_val = atr_series[-1] if atr_series else s_bar.close * 0.01
                        
                        swing_bars = today_candles[-5:]
                        conviction = (abs(sig.relative_strength) * 100.0 * sig.relative_volume) / 4.0
                        cands.append({
                            'symbol': sym,
                            'side': sig.side,
                            'signal_price': s_bar.close,
                            'signal_time': bar_close_dt,
                            'fill_time': next_fill_dt,
                            'swing_low_5': min(c.low for c in swing_bars),
                            'swing_high_5': max(c.high for c in swing_bars),
                            'atr': atr_val,
                            'conviction': conviction,
                        })
                if cands:
                    cands.sort(key=lambda x: x['conviction'], reverse=True)
                    pending_entry = cands[0]

    # Summarize trades
    wins = [t for t in trades if t['net_pnl'] > 0]
    losses = [t for t in trades if t['net_pnl'] < 0]
    gross_win = sum(t['gross_pnl'] for t in trades if t['gross_pnl'] > 0)
    gross_loss = sum(abs(t['gross_pnl']) for t in trades if t['gross_pnl'] < 0)
    gross_pnl = sum(t['gross_pnl'] for t in trades)
    tot_charges = sum(t['charges'] for t in trades)
    net_pnl = round(gross_pnl - tot_charges, 2)
    win_rate = (len(wins) / len(trades) * 100.0) if trades else 0.0
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else 999.0
    friction_ratio_winning = (tot_charges / gross_win * 100.0) if gross_win > 0 else float('inf')
    friction_ratio_net_gross = (tot_charges / gross_pnl * 100.0) if gross_pnl > 0 else float('inf')
    avg_win = (gross_win / len(wins)) if wins else 0.0
    avg_loss = (gross_loss / len(losses)) if losses else 0.0

    print(f"\n--- RESULTS: Target {target_r}R, Stop [{min_stop_pct*100:.2f}%, {max_stop_pct*100:.2f}%] ---")
    print(f"Trades: {len(trades)} (Wins: {len(wins)}, Losses: {len(losses)})")
    print(f"Win Rate: {win_rate:.2f}% | Profit Factor: {profit_factor:.2f}")
    print(f"Gross P&L: Rs. {gross_pnl:+,.2f} (Gross Win: Rs. {gross_win:,.2f}, Gross Loss: Rs. {gross_loss:,.2f})")
    print(f"Charges: Rs. {tot_charges:,.2f}")
    print(f"Net P&L: Rs. {net_pnl:+,.2f} ({net_pnl/15000*100:+.2f}%)")
    print(f"Avg Gross Win: Rs. {avg_win:,.2f} | Avg Gross Loss: Rs. {avg_loss:,.2f}")
    print(f"Charges / Gross Winning: {friction_ratio_winning:.2f}% (Limit < 26.0%)")
    print(f"Charges / Net Gross: {friction_ratio_net_gross:.2f}%")
    print(f"\n--- ALL TRADES ---")
    for t in trades:
        print(f"{t['date']} {t['symbol']} {t['side']} qty={t['qty']} entry={t['entry_price']} exit={t['exit_price']} reason={t['exit_reason']} gross={t['gross_pnl']} net={t['net_pnl']} R={t['r_multiple']}")
    return {
        'trades': len(trades), 'net_pnl': net_pnl, 'win_rate': win_rate, 'pf': profit_factor,
        'gross_win': gross_win, 'gross_loss': gross_loss, 'charges': tot_charges,
        'avg_win': avg_win, 'friction_win': friction_ratio_winning
    }

print("Running 3-Month simulation with Target 2.0R:")
run_test_sim(target_r=2.0)

print("\nRunning 3-Month simulation with Target 2.25R:")
run_test_sim(target_r=2.25)
