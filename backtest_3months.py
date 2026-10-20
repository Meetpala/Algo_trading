"""Optimized High-Performance APEX-3 3-Month Backtest Engine.

Pre-parses all historical bars into memory for instant execution across all 57 sessions.
Evaluates:
- Option A: Fixed 2.0R Target + Structural SL (Current Program Strategy)
- Full 3-Month performance (July 24, 2026 - October 19, 2026)
- Monthly breakdown: Month 1, Month 2, Month 3 (Last 30 Days)
- Optimization Grid: 1.5R, 2.0R, 2.5R, 3.0R Targets, Hybrid Trailing
- Itemized Brokerage and Regulatory Charges Statement on Rs. 15,000 Capital
"""
import os
import math
import json
from datetime import datetime, time
import pandas as pd
import numpy as np

from apex3.config import load_config
from apex3.models import Candle, Quote, InstrumentInfo, OpeningRange, Side, Decision
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import evaluate_signal
from apex3.strategy.trail import calculate_initial_stop, calculate_target_price, evaluate_trailing_stop
from apex3.indicators.atr import compute_atr
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

def run_fast_backtest(
    data: dict,
    watchlist_symbols: list[str],
    target_r: float = 2.0,
    use_fixed_target: bool = True,
    trail_enabled: bool = False,
    trail_activation_r: float = 1.0,
    initial_atr_mult: float = 1.5,
):
    config = load_config()
    nifty_candles = data['NSEI']['candles']
    nifty_df = data['NSEI']['df']

    dates = sorted(list(set(nifty_df['Datetime'].dt.date)))
    # 57 trading sessions (July 29 to October 19)
    test_dates = dates[3:]

    starting_capital = 15000.0
    current_capital = starting_capital
    leverage = 4.0
    safety_reserve_pct = 5.0
    max_positions = 3

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

        for sym in watchlist_symbols:
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

        for step, n_idx in enumerate(sess_indices):
            c_bar = nifty_candles[n_idx]
            current_dt = c_bar.timestamp
            bar_time = current_dt.time()

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

                is_stopped = False
                exit_price = pos['current_stop']
                exit_reason = "STOP_LOSS"

                # 1. Target check
                if use_fixed_target and pos['target_price'] > 0:
                    if pos['side'] == Side.BUY and s_high >= pos['target_price']:
                        is_stopped = True
                        exit_price = pos['target_price']
                        exit_reason = f"TARGET_HIT_{target_r}R"
                    elif pos['side'] == Side.SELL and s_low <= pos['target_price']:
                        is_stopped = True
                        exit_price = pos['target_price']
                        exit_reason = f"TARGET_HIT_{target_r}R"

                # 2. Stop check
                if not is_stopped:
                    if pos['side'] == Side.BUY:
                        if s_low <= pos['current_stop']:
                            is_stopped = True
                            exit_price = min(pos['current_stop'], s_bar.open)
                            exit_reason = "TRAILING_STOP_HIT" if pos['trail_activated'] else "STRUCTURAL_STOP_HIT"
                    else:
                        if s_high >= pos['current_stop']:
                            is_stopped = True
                            exit_price = max(pos['current_stop'], s_bar.open)
                            exit_reason = "TRAILING_STOP_HIT" if pos['trail_activated'] else "STRUCTURAL_STOP_HIT"

                # 3. EOD Flatten check
                if not is_stopped and bar_time >= time(14, 55):
                    is_stopped = True
                    exit_price = s_close
                    exit_reason = "EOD_FLATTEN_1455"

                if is_stopped:
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
                        'exit_time': current_dt.strftime('%H:%M'),
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
                    if trail_enabled:
                        if pos['side'] == Side.BUY:
                            pos['best_favorable'] = max(pos['best_favorable'], s_high)
                            fav = pos['best_favorable'] - pos['entry_price']
                        else:
                            pos['best_favorable'] = min(pos['best_favorable'], s_low)
                            fav = pos['entry_price'] - pos['best_favorable']

                        cur_r = fav / pos['r_rupees'] if pos['r_rupees'] > 0 else 0.0
                        if cur_r >= trail_activation_r:
                            pos['trail_activated'] = True

                        if pos['trail_activated']:
                            rt_cost = (config.costs.brokerage_per_order * 2) / max(1, pos['quantity']) + (pos['entry_price'] * 0.0003)
                            lock_floor = pos['entry_price'] + rt_cost + config.stops.trail.profit_lock_rupees if pos['side'] == Side.BUY else pos['entry_price'] - (rt_cost + config.stops.trail.profit_lock_rupees)
                            atr_mult = config.stops.trail.atr_multiplier_tight if bar_time >= time(13, 30) else config.stops.trail.atr_multiplier_normal
                            atr_trail = pos['best_favorable'] - (atr_mult * pos['atr']) if pos['side'] == Side.BUY else pos['best_favorable'] + (atr_mult * pos['atr'])

                            if pos['side'] == Side.BUY:
                                cand = max(pos['current_stop'], lock_floor, atr_trail)
                                cand = min(cand, s_close - 0.10)
                                if cand > pos['current_stop'] + 0.10:
                                    pos['current_stop'] = round(math.floor(cand / 0.05) * 0.05, 2)
                            else:
                                cand = min(pos['current_stop'], lock_floor, atr_trail)
                                cand = max(cand, s_close + 0.10)
                                if cand < pos['current_stop'] - 0.10:
                                    pos['current_stop'] = round(math.ceil(cand / 0.05) * 0.05, 2)

            for cs in closed_syms:
                del open_positions[cs]

            # Entry checks
            if time(9, 30) <= bar_time <= time(14, 15):
                if len(open_positions) >= max_positions:
                    continue

                bench_slice = nifty_candles[max(0, n_idx - 100): n_idx + 1]
                regime_eval = evaluate_market_regime(bench_slice, config.regime)
                if regime_eval.regime == MarketRegime.NEUTRAL:
                    continue

                for sym in watchlist_symbols:
                    if sym in open_positions or len(open_positions) >= max_positions:
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

                        entry_price = s_bar.close
                        side = sig.side

                        pad = initial_atr_mult * atr_val
                        if side == Side.BUY:
                            raw_stop = s_bar.low - pad
                            initial_stop = round(math.floor(raw_stop / 0.05) * 0.05, 2)
                        else:
                            raw_stop = s_bar.high + pad
                            initial_stop = round(math.ceil(raw_stop / 0.05) * 0.05, 2)

                        r_dist = abs(entry_price - initial_stop)
                        tgt_price = calculate_target_price(side, entry_price, initial_stop, target_r=target_r, tick_size=0.05)

                        usable_margin = max(0.0, current_capital * (1.0 - safety_reserve_pct / 100.0))
                        num_alloc = max_positions - len(open_positions)
                        symbol_budget = (usable_margin * leverage) / max(1, num_alloc)
                        qty = math.floor(symbol_budget / entry_price)

                        if qty > 0:
                            open_positions[sym] = {
                                'symbol': sym,
                                'side': side,
                                'entry_price': entry_price,
                                'initial_stop': initial_stop,
                                'current_stop': initial_stop,
                                'target_price': tgt_price,
                                'quantity': qty,
                                'entry_time': current_dt,
                                'atr': atr_val,
                                'r_rupees': r_dist,
                                'best_favorable': entry_price,
                                'trail_activated': False,
                            }

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
    net_pnl = gross_pnl - tot_charges
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
        'net_pnl': round(net_pnl, 2),
        'profit_pct': round((net_pnl / starting_capital) * 100.0, 2),
        'ending_capital': round(current_capital, 2),
        'trades': trades,
        'daily_stats': daily_stats,
    }

if __name__ == '__main__':
    symbols = ['NSEI', 'RELIANCE', 'TCS', 'INFY']
    print("Pre-loading historical data into memory...")
    preloaded = load_preparsed_data(symbols)
    print("Data loaded successfully!")

    print("\n--- Running Option A Baseline: 2.0R Fixed Target + Structural SL (3 Months) ---")
    res_3m = run_fast_backtest(preloaded, ['RELIANCE', 'TCS', 'INFY'], target_r=2.0, use_fixed_target=True, trail_enabled=False)

    print(f"Total 3-Month Trades: {res_3m['total_trades']}")
    print(f"Profit Probability (Win Rate): {res_3m['win_rate']}%")
    print(f"Gross P&L: Rs. {res_3m['gross_pnl']}")
    print(f"Charges: Rs. {res_3m['total_charges']}")
    print(f"Net P&L: Rs. {res_3m['net_pnl']} ({res_3m['profit_pct']}%)")
    print(f"Ending Capital: Rs. {res_3m['ending_capital']}")

    # Monthly breakdown (Month 1: July 29 - Aug 21, Month 2: Aug 24 - Sep 18, Month 3 / Last 30 Days: Sep 21 - Oct 19)
    df_all_trades = pd.DataFrame(res_3m['trades'])
    df_all_trades['date_dt'] = pd.to_datetime(df_all_trades['date'])

    m1_trades = df_all_trades[df_all_trades['date_dt'] < pd.to_datetime('2026-08-24')]
    m2_trades = df_all_trades[(df_all_trades['date_dt'] >= pd.to_datetime('2026-08-24')) & (df_all_trades['date_dt'] < pd.to_datetime('2026-09-21'))]
    m3_trades = df_all_trades[df_all_trades['date_dt'] >= pd.to_datetime('2026-09-21')]

    def summarize_subset(sub_df, name):
        if sub_df.empty:
            return {'Period': name, 'Trades': 0, 'Win Rate': '0%', 'Gross (Rs)': 0, 'Charges (Rs)': 0, 'Net (Rs)': 0, 'ROI (%)': 0}
        w = len(sub_df[sub_df['net_pnl'] > 0])
        tot = len(sub_df)
        g = round(sub_df['gross_pnl'].sum(), 2)
        c = round(sub_df['charges'].apply(lambda x: x['total_charges']).sum(), 2)
        n = round(g - c, 2)
        return {
            'Period': name,
            'Trades': tot,
            'Win Rate': f"{round(w / tot * 100, 2)}%",
            'Gross (Rs)': g,
            'Charges (Rs)': c,
            'Net (Rs)': n,
            'ROI (%)': f"{round((n / 15000.0) * 100, 2)}%"
        }

    monthly_table = [
        summarize_subset(m1_trades, 'Month 1 (29 Jul - 21 Aug)'),
        summarize_subset(m2_trades, 'Month 2 (24 Aug - 18 Sep)'),
        summarize_subset(m3_trades, 'Month 3 / Last 30 Days (21 Sep - 19 Oct)'),
        summarize_subset(df_all_trades, 'Full 3 Months Total'),
    ]
    print("\nMONTHLY BREAKDOWN TABLE:")
    print(pd.DataFrame(monthly_table).to_string(index=False))

    # Optimization Matrix across 8 Configurations
    print("\n--- Running Optimization Grid across 8 Variations (3 Months) ---")
    configs = [
        ('1. Option A (Fixed 2.0R Target + Structural SL)', 2.0, True, False, 1.0, 1.5),
        ('2. Fixed 1.5R Target + Structural SL', 1.5, True, False, 1.0, 1.5),
        ('3. Fixed 2.5R Target + Structural SL', 2.5, True, False, 1.0, 1.5),
        ('4. Fixed 3.0R Target + Structural SL', 3.0, True, False, 1.0, 1.5),
        ('5. Hybrid: 2.5R Target + Trail after 1.5R', 2.5, True, True, 1.5, 1.5),
        ('6. Hybrid: 2.0R Target + Trail after 1.2R', 2.0, True, True, 1.2, 1.5),
        ('7. Old Baseline: +1.0R Trail (No Fixed Target)', 0.0, False, True, 1.0, 1.5),
        ('8. Tight Stop: 1.0x ATR Stop + 2.0R Target', 2.0, True, False, 1.0, 1.0),
    ]

    opt_rows = []
    for label, tgt_r, use_tgt, trail_on, trail_act, atr_m in configs:
        r = run_fast_backtest(preloaded, ['RELIANCE', 'TCS', 'INFY'], target_r=tgt_r, use_fixed_target=use_tgt, trail_enabled=trail_on, trail_activation_r=trail_act, initial_atr_mult=atr_m)
        opt_rows.append({
            'Strategy Variation': label,
            'Trades': r['total_trades'],
            'Win Rate': f"{r['win_rate']}%",
            'Gross P&L (Rs)': r['gross_pnl'],
            'Profit Factor': r['profit_factor'],
            'Charges (Rs)': r['total_charges'],
            'Net P&L (Rs)': r['net_pnl'],
            '3-Month Net ROI': f"{r['profit_pct']}%",
        })

    print("\nOPTIMIZATION GRID RESULTS (3 MONTHS):")
    print(pd.DataFrame(opt_rows).to_string(index=False))

    # Save to JSON
    with open('three_month_results.json', 'w') as f:
        json.dump({
            'base_3m': {k: v for k, v in res_3m.items() if k != 'trades'},
            'monthly_table': monthly_table,
            'opt_grid': opt_rows,
            'trades_sample': res_3m['trades'][:20],
        }, f, indent=2)
