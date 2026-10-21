import os, sys
sys.path.insert(0, os.getcwd())
import pandas as pd
from datetime import time, timedelta
from apex3.config import load_config
from apex3.strategy.engine import build_active_radar
from apex3.models import InstrumentInfo, Quote, Side, Decision, OpeningRange
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import evaluate_signal
from apex3.strategy.trail import compute_initial_stop_and_target
from apex3.indicators.atr import compute_atr
from backtest_3months import load_preparsed_data, calculate_charges, apply_adverse_spread, is_entry_fill_acceptable

config = load_config()
watchlist = build_active_radar(config.raw_dict)
all_symbols = ['NSEI'] + watchlist
data = load_preparsed_data(all_symbols)

nifty_df = data['NSEI']['df']
nifty_candles = data['NSEI']['candles']
all_dates = sorted(set(nifty_df['Datetime'].dt.date))
three_month_dates = all_dates[-57:]
first_half_dates = [d for d in all_dates if d < three_month_dates[0]]

def simulate(dates, target_r=2.0, min_stop_pct=0.0055, max_stop_pct=0.0150):
    starting_capital = 15000.0
    current_capital = starting_capital
    leverage = 4.0
    safety_reserve_pct = 5.0
    max_positions = 2
    usable_margin = starting_capital * (1.0 - safety_reserve_pct / 100.0)
    slot_budget = (usable_margin * leverage) / max_positions
    risk_cap = starting_capital * 0.0080  # Rs. 120
    daily_stop = -1.0 * starting_capital * 0.0160  # -Rs. 240
    
    trades = []
    
    for session_date in dates:
        n_sess_mask = (nifty_df['Datetime'].dt.date == session_date)
        sess_indices = nifty_df[n_sess_mask].index.tolist()
        if len(sess_indices) < 6: continue
        
        # Build opening ranges for this session (09:15 - 09:30)
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
            
            # Daily halt check
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
                
                # Slot-2 Lock Gate
                if can_fill and len(open_positions) == 1:
                    first = next(iter(open_positions.values()))
                    first_bar_idx = stock_sess_map[first['symbol']].get(current_dt)
                    if first_bar_idx is not None:
                        first_open = data[first['symbol']]['candles'][first_bar_idx].open
                        first_r = (first_open - first['entry_price']) / first['r_rupees'] if first['side'] == Side.BUY else (first['entry_price'] - first_open) / first['r_rupees']
                        if first_r < 0.50:
                            can_fill = False
                    else:
                        can_fill = False

                if can_fill:
                    s_bar = data[sym]['candles'][s_idx]
                    # Check 0.15% max slippage from signal close
                    if is_entry_fill_acceptable(pending['signal_price'], s_bar.open, 0.15):
                        side = pending['side']
                        # Next-bar open + 1 tick adverse half-spread
                        entry_price = round(s_bar.open + 0.05 if side == Side.BUY else s_bar.open - 0.05, 2)
                        
                        # 5-bar structural swing anchor + 1.5x ATR
                        levels = compute_initial_stop_and_target(
                            side=side, entry_price=entry_price,
                            swing_low_5=pending['swing_low_5'], swing_high_5=pending['swing_high_5'],
                            atr14=pending['atr'], cfg=config.raw_dict
                        )
                        target_price = round(entry_price + (target_r * levels.risk_per_share) if side == Side.BUY else entry_price - (target_r * levels.risk_per_share), 2)
                        
                        # Stop width sweet spot check
                        if min_stop_pct <= levels.risk_pct <= max_stop_pct:
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
                
                exit_price = None
                exit_reason = None
                
                if bar_time >= time(14, 55):
                    exit_price = s_bar.close
                    exit_reason = "EOD_FLATTEN"
                else:
                    hit_stop = (s_bar.low <= pos['current_stop']) if pos['side'] == Side.BUY else (s_bar.high >= pos['current_stop'])
                    hit_target = (s_bar.high >= pos['target_price']) if pos['side'] == Side.BUY else (s_bar.low <= pos['target_price'])
                    
                    if hit_stop and hit_target:
                        # CONSERVATIVE INTRABAR RESOLUTION: STOP LOSS HIT FIRST
                        exit_price = pos['current_stop']
                        exit_reason = "FEE_LOCK_STOP" if pos['fee_lock_applied'] else "STOP_LOSS"
                    elif hit_stop:
                        exit_price = pos['current_stop']
                        exit_reason = "FEE_LOCK_STOP" if pos['fee_lock_applied'] else "STOP_LOSS"
                    elif hit_target:
                        exit_price = pos['target_price']
                        exit_reason = f"TARGET_HIT_{target_r}R"
                    else:
                        # Fee-lock protection evaluation
                        curr_r = (s_bar.close - pos['entry_price']) / pos['r_rupees'] if pos['side'] == Side.BUY else (pos['entry_price'] - s_bar.close) / pos['r_rupees']
                        pos['unrealized_r'] = curr_r
                        if not pos['fee_lock_applied']:
                            # Trigger at +1.65R anytime, OR >= +1.15R at >= 14:15 IST
                            if curr_r >= 1.65 or (bar_close_dt.time() >= time(14, 15) and curr_r >= 1.15):
                                offset = 0.0015 * pos['entry_price']
                                pos['current_stop'] = round(pos['entry_price'] + offset if pos['side'] == Side.BUY else pos['entry_price'] - offset, 2)
                                pos['fee_lock_applied'] = True

                if exit_price is not None:
                    exit_side = Side.SELL if pos['side'] == Side.BUY else Side.BUY
                    actual_exit_px = apply_adverse_spread(exit_price, exit_side, 4.0)
                    buy_p = pos['entry_price'] if pos['side'] == Side.BUY else actual_exit_px
                    sell_p = actual_exit_px if pos['side'] == Side.BUY else pos['entry_price']
                    qty = pos['quantity']
                    gross_pnl = (sell_p - buy_p) * qty
                    charges = calculate_charges(buy_p, sell_p, qty, config.costs)
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
            # Morning stability window: 09:35 - 12:30 IST
            if (
                next_n_idx is not None
                and not daily_halted
                and len(open_positions) < max_positions
                and time(9, 35) <= bar_close_dt.time() <= time(12, 30)
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
                    
                    inst = InstrumentInfo(symbol=sym, exchange="NSE", lot_size=1, tick_size=0.05, upper_circuit=first_open*1.10, lower_circuit=first_open*0.90)
                    quote = Quote(symbol=sym, exchange="NSE", last_price=s_bar.close, bid=s_bar.close-0.05, ask=s_bar.close+0.05, volume=s_bar.volume, timestamp=current_dt, is_fresh=True)
                    
                    sig = evaluate_signal(
                        symbol=sym, stock_candles_5m=today_candles, benchmark_candles_5m=bench_slice,
                        opening_range=or_range, market_regime=regime_eval.regime, instrument=inst,
                        quote=quote, config=config.signal, is_entry_window=True
                    )
                    
                    if sig.decision in (Decision.PROPOSED_LONG, Decision.PROPOSED_SHORT):
                        history_start = max(0, s_idx - 60)
                        stock_history = data[sym]['candles'][history_start:s_idx + 1]
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

    return {
        'trades': len(trades), 'wins': len(wins), 'losses': len(losses),
        'win_rate': round(win_rate, 2), 'profit_factor': round(profit_factor, 2),
        'gross_pnl': round(gross_pnl, 2), 'gross_win': round(gross_win, 2), 'gross_loss': round(gross_loss, 2),
        'charges': round(tot_charges, 2), 'net_pnl': net_pnl, 'profit_pct': round((net_pnl / starting_capital) * 100.0, 2),
        'avg_win': round(avg_win, 2), 'avg_loss': round(avg_loss, 2),
        'friction_winning': round(friction_ratio_winning, 2), 'friction_net': round(friction_ratio_net_gross, 2),
        'trade_list': trades,
    }

if __name__ == '__main__':
    print("Testing 3-Month Performance (July 29 - Oct 21):")
    r3 = simulate(three_month_dates, target_r=2.0)
    print(f"Trades: {r3['trades']} | Win Rate: {r3['win_rate']}% | PF: {r3['profit_factor']}")
    print(f"Gross: Rs. {r3['gross_pnl']} | Charges: Rs. {r3['charges']} | Net: Rs. {r3['net_pnl']} ({r3['profit_pct']}%)")
    print(f"Avg Win: Rs. {r3['avg_win']} | Friction / Winning: {r3['friction_winning']}%")

    print("\nTesting First Half Performance (Apr 27 - Jul 28):")
    r1 = simulate(first_half_dates, target_r=2.0)
    print(f"Trades: {r1['trades']} | Win Rate: {r1['win_rate']}% | PF: {r1['profit_factor']}")
    print(f"Gross: Rs. {r1['gross_pnl']} | Charges: Rs. {r1['charges']} | Net: Rs. {r1['net_pnl']} ({r1['profit_pct']}%)")

    print("\nTesting Full 6-Month Combined Performance:")
    r6 = simulate(all_dates, target_r=2.0)
    print(f"Trades: {r6['trades']} | Win Rate: {r6['win_rate']}% | PF: {r6['profit_factor']}")
    print(f"Gross: Rs. {r6['gross_pnl']} | Charges: Rs. {r6['charges']} | Net: Rs. {r6['net_pnl']} ({r6['profit_pct']}%)")
    print(f"Avg Win: Rs. {r6['avg_win']} | Friction / Winning: {r6['friction_winning']}%")
