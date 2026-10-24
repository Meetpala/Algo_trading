"""Backtest Script for APEX-3 Upgraded Radar, Gate 9, and Conviction Ranker.

Simulates:
- 3-Tier Merged Stock Radar (Core + Extended + Custom)
- Gate 9: Stop-Width Sweet Spot (0.45% <= R% <= 0.85%) + ADR Exhaustion Gate (<= 60%)
- Cross-Sectional Conviction Ranker: (|RS| * 100 * Vol_Ratio) / max(Spread, 1.0)
- 2-Slot Allocation (Rs. 28,500 per slot) with 0.80% risk cap (Rs. 120 max risk)
- Second-Slot Profit Condition (Slot 2 only opens if Slot 1 is >= +0.50R)
- Fee-Protected Breakeven Step (Entry +/- 0.15%) at +1.65R or at 13:45 IST (if >= +0.75R)
- Exact Zerodha Intraday Statutory Charges
"""

import os
import math
import json
from datetime import datetime, time, date
import pandas as pd
import numpy as np

from apex3.config import load_config
from apex3.strategy.regime import BenchmarkSnapshot, MarketRegime, evaluate_market_regime
from apex3.strategy.signal import StockCandidateBar, TradeSignal, evaluate_candidate_signal
from apex3.strategy.trail import (
    StopTargetLevels,
    compute_initial_stop_and_target,
    evaluate_fee_protected_stop,
    floor_to_tick,
    ceil_to_tick,
)
from apex3.strategy.sizer import (
    PositionSizeResult,
    calculate_position_size,
    calculate_zerodha_intraday_charges,
)
from apex3.strategy.engine import build_active_radar, select_best_entries_for_bar


def compute_adx(df: pd.DataFrame, period: int = 14) -> tuple[pd.Series, pd.Series]:
    """Computes Wilders ATR and ADX."""
    high = df['High']
    low = df['Low']
    close = df['Close']
    
    tr1 = high - low
    tr2 = (high - close.shift(1)).abs()
    tr3 = (low - close.shift(1)).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)
    
    plus_dm_series = pd.Series(plus_dm, index=df.index)
    minus_dm_series = pd.Series(minus_dm, index=df.index)
    
    # Wilder's smoothing
    alpha = 1.0 / period
    atr = tr.ewm(alpha=alpha, adjust=False).mean()
    plus_di = 100.0 * (plus_dm_series.ewm(alpha=alpha, adjust=False).mean() / (atr + 1e-9))
    minus_di = 100.0 * (minus_dm_series.ewm(alpha=alpha, adjust=False).mean() / (atr + 1e-9))
    
    dx = 100.0 * ((plus_di - minus_di).abs() / ((plus_di + minus_di) + 1e-9))
    adx = dx.ewm(alpha=alpha, adjust=False).mean()
    return atr, adx


def compute_intraday_vwap(df: pd.DataFrame) -> pd.Series:
    """Computes session-anchored VWAP resetting at each date boundary."""
    typical_price = (df['High'] + df['Low'] + df['Close']) / 3.0
    tp_vol = typical_price * df['Volume']
    
    dates = df['Datetime'].dt.date
    cum_tp_vol = tp_vol.groupby(dates).cumsum()
    cum_vol = df['Volume'].groupby(dates).cumsum()
    
    # When volume is 0 (e.g. index data), fall back to cumulative mean of typical price
    cum_tp = typical_price.groupby(dates).cumsum()
    cum_cnt = typical_price.groupby(dates).cumcount() + 1
    mean_tp = cum_tp / cum_cnt
    
    vwap = np.where(cum_vol > 0, cum_tp_vol / (cum_vol + 1e-9), mean_tp)
    return pd.Series(vwap, index=df.index)


def prepare_benchmark_data(nifty_csv_path: str) -> pd.DataFrame:
    """Loads and computes technicals on NIFTY 50 5m bars."""
    df = pd.read_csv(nifty_csv_path)
    df['Datetime'] = pd.to_datetime(df['Datetime'])
    df = df.sort_values('Datetime').reset_index(drop=True)
    
    df['ema20'] = df['Close'].ewm(span=20, adjust=False).mean()
    df['prev_ema20'] = df['ema20'].shift(1)
    df['session_vwap'] = compute_intraday_vwap(df)
    
    atr, adx = compute_adx(df, period=14)
    df['adx14'] = adx
    df['prev_adx14'] = df['adx14'].shift(1)
    
    df['return_20'] = (df['Close'] - df['Close'].shift(20)) / (df['Close'].shift(20) + 1e-9)
    return df


def prepare_stock_data(stock_csv_path: str) -> pd.DataFrame:
    """Loads and computes technicals on individual stock 5m bars."""
    df = pd.read_csv(stock_csv_path)
    df['Datetime'] = pd.to_datetime(df['Datetime'])
    df = df.sort_values('Datetime').reset_index(drop=True)
    
    df['session_vwap'] = compute_intraday_vwap(df)
    df['return_20'] = (df['Close'] - df['Close'].shift(20)) / (df['Close'].shift(20) + 1e-9)
    df['avg_volume_20'] = df['Volume'].rolling(20).mean()
    
    atr, _ = compute_adx(df, period=14)
    df['atr14'] = atr
    
    df['swing_low_5'] = df['Low'].rolling(5).min()
    df['swing_high_5'] = df['High'].rolling(5).max()
    
    # Daily ATR / ADR over 14 daily sessions
    daily_stats = df.groupby(df['Datetime'].dt.date).agg(
        day_high=('High', 'max'),
        day_low=('Low', 'min'),
        day_close=('Close', 'last')
    ).reset_index()
    daily_stats.rename(columns={'Datetime': 'date'}, inplace=True)
    daily_stats['day_range_pct'] = (daily_stats['day_high'] - daily_stats['day_low']) / daily_stats['day_close']
    daily_stats['adr14_pct'] = daily_stats['day_range_pct'].rolling(14, min_periods=1).mean()
    
    df['date'] = df['Datetime'].dt.date
    df = df.merge(daily_stats[['date', 'adr14_pct']], on='date', how='left')
    
    # Cumulative intraday high and low so far
    df['session_high_so_far'] = df.groupby('date')['High'].cummax()
    df['session_low_so_far'] = df.groupby('date')['Low'].cummin()
    
    return df


def run_backtest():
    cfg = load_config().raw_dict
    active_radar = build_active_radar(cfg)
    print(f"Active Radar ({len(active_radar)} symbols): {active_radar}")
    
    # Check data availability
    available_stocks = {}
    for sym in active_radar:
        fpath = os.path.join('scratch_data', f'{sym}.csv')
        if os.path.exists(fpath):
            available_stocks[sym] = prepare_stock_data(fpath)
        else:
            print(f"Warning: Data file missing for {sym}, skipping.")

    # Precompute bar lookups and daily groups
    stock_bar_dicts = {sym: s_df.set_index('Datetime').to_dict(orient='index') for sym, s_df in available_stocks.items()}
    stock_daily_groups = {sym: {d: grp for d, grp in s_df.groupby('date')} for sym, s_df in available_stocks.items()}
    
    nifty_df = prepare_benchmark_data(os.path.join('scratch_data', 'NSEI.csv'))
    print(f"Loaded NIFTY 50: {len(nifty_df)} 5m bars", flush=True)
    
    all_dates = sorted(list(set(nifty_df['Datetime'].dt.date)))
    # Warm up period (first 3 days for indicators)
    test_dates = all_dates[3:]
    print(f"Total Test Dates: {len(test_dates)} sessions ({test_dates[0]} to {test_dates[-1]})", flush=True)
    
    starting_capital = float(cfg['account']['starting_capital'])
    current_capital = starting_capital
    max_slots = int(cfg['risk_management']['max_concurrent_positions'])
    
    all_trades = []
    daily_logs = []
    
    for session_date in test_dates:
        # Nifty session slice
        n_mask = (nifty_df['Datetime'].dt.date == session_date)
        n_df_session = nifty_df[n_mask]
        if len(n_df_session) < 10:
            continue
            
        # Precompute Opening Range (09:15 to 09:30) for each stock from daily group
        stock_or = {}
        for sym in available_stocks:
            s_day_df = stock_daily_groups[sym].get(session_date)
            if s_day_df is not None:
                or_df = s_day_df[s_day_df['Datetime'].dt.time < time(9, 30)]
                if len(or_df) >= 3:
                    stock_or[sym] = {
                        'or_high': float(or_df['High'].max()),
                        'or_low': float(or_df['Low'].min()),
                        'circuit_ref': float(or_df.iloc[0]['Open']),
                    }
                else:
                    stock_or[sym] = None
            else:
                stock_or[sym] = None

        open_positions = {}
        day_realized_pnl_net = 0.0
        session_trades = []
        
        for _, n_row in n_df_session.iterrows():
            cur_dt = n_row['Datetime']
            cur_time = cur_dt.time()
            time_str = cur_time.strftime("%H:%M")
            
            # 1. Update Existing Open Positions
            closed_symbols = []
            for sym, pos in open_positions.items():
                s_bar = stock_bar_dicts[sym].get(cur_dt)
                if s_bar is None:
                    continue
                
                # Check Fee-Protected Breakeven Step (1.65R profit or 13:45 IST if >= +0.75R)
                levels = pos['levels']
                evaluate_fee_protected_stop(
                    levels=levels,
                    current_price=s_bar['Close'],
                    current_time_str=time_str,
                    cfg=cfg,
                )
                
                # Check Exits: Target, Stop, EOD
                exit_price = None
                exit_reason = None
                
                if pos['side'] == "LONG":
                    # Target Hit
                    if s_bar['High'] >= levels.target_price:
                        exit_price = levels.target_price
                        exit_reason = "TARGET_HIT_2.0R"
                    # Stop Hit
                    elif s_bar['Low'] <= levels.current_stop:
                        exit_price = min(levels.current_stop, s_bar['Open'])
                        exit_reason = "FEE_LOCK_STOP" if levels.fee_lock_applied else "STRUCTURAL_STOP"
                else: # SHORT
                    # Target Hit
                    if s_bar['Low'] <= levels.target_price:
                        exit_price = levels.target_price
                        exit_reason = "TARGET_HIT_2.0R"
                    # Stop Hit
                    elif s_bar['High'] >= levels.current_stop:
                        exit_price = max(levels.current_stop, s_bar['Open'])
                        exit_reason = "FEE_LOCK_STOP" if levels.fee_lock_applied else "STRUCTURAL_STOP"
                        
                # EOD Square-Off at 14:55
                if exit_price is None and cur_time >= time(14, 55):
                    exit_price = s_bar['Close']
                    exit_reason = "EOD_SQUAREOFF_1455"
                    
                if exit_price is not None:
                    qty = pos['quantity']
                    entry_p = pos['entry_price']
                    if pos['side'] == "LONG":
                        gross_pnl = (exit_price - entry_p) * qty
                        charges = calculate_zerodha_intraday_charges(entry_p, exit_price, qty)
                    else:
                        gross_pnl = (entry_p - exit_price) * qty
                        charges = calculate_zerodha_intraday_charges(exit_price, entry_p, qty)
                    
                    net_pnl = round(gross_pnl - charges, 2)
                    day_realized_pnl_net += net_pnl
                    current_capital += net_pnl
                    
                    trade_record = {
                        'date': session_date.strftime("%Y-%m-%d"),
                        'symbol': sym,
                        'side': pos['side'],
                        'quantity': qty,
                        'entry_time': pos['entry_time'],
                        'entry_price': entry_p,
                        'exit_time': time_str,
                        'exit_price': round(exit_price, 2),
                        'exit_reason': exit_reason,
                        'initial_stop': levels.initial_stop,
                        'target_price': levels.target_price,
                        'gross_pnl': round(gross_pnl, 2),
                        'charges': charges,
                        'net_pnl': net_pnl,
                        'conviction_score': pos['conviction_score'],
                        'fee_lock_applied': levels.fee_lock_applied,
                    }
                    all_trades.append(trade_record)
                    session_trades.append(trade_record)
                    closed_symbols.append(sym)
                    
            for cs in closed_symbols:
                del open_positions[cs]
                
            # Update unrealized_r for existing open positions (needed for Slot 2 unlock)
            for sym, pos in open_positions.items():
                s_bar = stock_bar_dicts[sym].get(cur_dt)
                if s_bar is not None:
                    s_close = s_bar['Close']
                    if pos['side'] == "LONG":
                        pos['unrealized_r'] = (s_close - pos['entry_price']) / pos['levels'].risk_per_share
                    else:
                        pos['unrealized_r'] = (pos['entry_price'] - s_close) / pos['levels'].risk_per_share

            # 2. Entry Evaluation at Bar Close (Window: 09:30 to 13:00)
            if not (time(9, 30) <= cur_time <= time(13, 0)):
                continue
                
            if len(open_positions) >= max_slots:
                continue
                
            # Create BenchmarkSnapshot
            bench_snapshot = BenchmarkSnapshot(
                close=float(n_row['Close']),
                ema20=float(n_row['ema20']),
                prev_ema20=float(n_row['prev_ema20']),
                session_vwap=float(n_row['session_vwap']),
                adx14=float(n_row['adx14']),
                prev_adx14=float(n_row['prev_adx14']),
            )
            
            # Prepare Candidate Bars for all radar symbols
            candidate_bars = {}
            for sym in active_radar:
                if sym in open_positions or sym not in available_stocks:
                    continue
                or_info = stock_or.get(sym)
                if or_info is None:
                    continue
                    
                s_bar = stock_bar_dicts[sym].get(cur_dt)
                if s_bar is None:
                    continue
                
                if pd.isna(s_bar['session_vwap']) or pd.isna(s_bar['atr14']) or pd.isna(s_bar['avg_volume_20']):
                    continue
                    
                circuit_ref = or_info['circuit_ref']
                cand = StockCandidateBar(
                    symbol=sym,
                    timestamp_hhmm=time_str,
                    close=float(s_bar['Close']),
                    or_high=or_info['or_high'],
                    or_low=or_info['or_low'],
                    session_vwap=float(s_bar['session_vwap']),
                    stock_return_20=float(s_bar['return_20']) if not pd.isna(s_bar['return_20']) else 0.0,
                    nifty_return_20=float(n_row['return_20']) if not pd.isna(n_row['return_20']) else 0.0,
                    bar_volume=float(s_bar['Volume']),
                    avg_volume_20=float(s_bar['avg_volume_20']),
                    bid_ask_spread_bps=2.0,  # 2 bps for liquid large-cap radar
                    upper_circuit=circuit_ref * 1.10,
                    lower_circuit=circuit_ref * 0.90,
                    swing_low_5=float(s_bar['swing_low_5']),
                    swing_high_5=float(s_bar['swing_high_5']),
                    atr14=float(s_bar['atr14']),
                    session_high_so_far=float(s_bar['session_high_so_far']),
                    session_low_so_far=float(s_bar['session_low_so_far']),
                    adr14_pct=float(s_bar['adr14_pct']) if not pd.isna(s_bar['adr14_pct']) else 0.025,
                )
                candidate_bars[sym] = cand
                
            # Select Best Entries using Engine
            selected_entries = select_best_entries_for_bar(
                benchmark_bar=bench_snapshot,
                candidate_bars=candidate_bars,
                open_positions=open_positions,
                realized_daily_pnl_net=day_realized_pnl_net,
                cfg=cfg,
            )
            
            for sig, size_res in selected_entries:
                open_positions[sig.symbol] = {
                    'symbol': sig.symbol,
                    'side': sig.side,
                    'entry_price': sig.entry_price,
                    'entry_time': time_str,
                    'quantity': size_res.quantity,
                    'levels': sig.levels,
                    'conviction_score': sig.conviction_score,
                    'unrealized_r': 0.0,
                }
                
        daily_logs.append({
            'date': session_date.strftime("%Y-%m-%d"),
            'trades_count': len(session_trades),
            'net_pnl': round(sum(t['net_pnl'] for t in session_trades), 2),
            'capital': round(current_capital, 2),
        })
        
    # Analysis & Metrics
    total_trades = len(all_trades)
    winning_trades = [t for t in all_trades if t['net_pnl'] > 0]
    losing_trades = [t for t in all_trades if t['net_pnl'] < 0]
    breakeven_trades = [t for t in all_trades if t['net_pnl'] == 0]
    
    gross_pnl = sum(t['gross_pnl'] for t in all_trades)
    gross_profit = sum(t['gross_pnl'] for t in winning_trades)
    gross_loss = sum(abs(t['gross_pnl']) for t in losing_trades)
    total_charges = sum(t['charges'] for t in all_trades)
    net_pnl = gross_pnl - total_charges
    
    win_rate = (len(winning_trades) / total_trades * 100.0) if total_trades > 0 else 0.0
    profit_factor = (gross_profit / gross_loss) if gross_loss > 0 else 999.0
    fee_to_win_ratio = (total_charges / gross_profit * 100.0) if gross_profit > 0 else 0.0
    
    # Last 30 Days (approximately last 22 trading sessions)
    last_30d_dates = set([d.strftime("%Y-%m-%d") for d in test_dates[-22:]])
    trades_30d = [t for t in all_trades if t['date'] in last_30d_dates]
    wins_30d = [t for t in trades_30d if t['net_pnl'] > 0]
    gross_30d = sum(t['gross_pnl'] for t in trades_30d)
    charges_30d = sum(t['charges'] for t in trades_30d)
    net_30d = gross_30d - charges_30d
    win_rate_30d = (len(wins_30d) / len(trades_30d) * 100.0) if trades_30d else 0.0
    
    results = {
        'total_trades': total_trades,
        'winning_trades': len(winning_trades),
        'losing_trades': len(losing_trades),
        'breakeven_trades': len(breakeven_trades),
        'win_rate_pct': round(win_rate, 2),
        'profit_factor': round(profit_factor, 2),
        'starting_capital': starting_capital,
        'ending_capital': round(current_capital, 2),
        'gross_profit': round(gross_profit, 2),
        'gross_loss': round(gross_loss, 2),
        'gross_pnl': round(gross_pnl, 2),
        'total_charges': round(total_charges, 2),
        'net_pnl': round(net_pnl, 2),
        'return_on_capital_pct': round((net_pnl / starting_capital) * 100.0, 2),
        'fee_to_win_ratio_pct': round(fee_to_win_ratio, 2),
        'last_30_days': {
            'total_trades': len(trades_30d),
            'winning_trades': len(wins_30d),
            'win_rate_pct': round(win_rate_30d, 2),
            'gross_pnl': round(gross_30d, 2),
            'total_charges': round(charges_30d, 2),
            'net_pnl': round(net_30d, 2),
            'profit_pct': round((net_30d / starting_capital) * 100.0, 2),
        },
        'trades': all_trades,
        'daily_logs': daily_logs,
    }
    
    with open('radar_conviction_backtest_results.json', 'w') as f:
        json.dump(results, f, indent=2)
        
    print("\n" + "=" * 60)
    print("APEX-3 UPGRADED RADAR & CONVICTION BACKTEST SUMMARY")
    print("=" * 60)
    print(f"Total Trading Sessions Evaluated: {len(test_dates)}")
    print(f"Total Executed Trades: {total_trades}")
    print(f"Winning Trades: {len(winning_trades)} | Losing Trades: {len(losing_trades)}")
    print(f"Win Rate: {win_rate:.2f}%")
    print(f"Profit Factor: {profit_factor:.2f}")
    print(f"Gross P&L: Rs. {gross_pnl:,.2f}")
    print(f"Total Zerodha Charges: Rs. {total_charges:,.2f}")
    print(f"Net P&L: Rs. {net_pnl:,.2f}")
    print(f"Return on Rs. 15,000 Capital: {(net_pnl / starting_capital) * 100.0:.2f}%")
    print(f"Fee-to-Win Ratio: {fee_to_win_ratio:.2f}%")
    print("-" * 60)
    print("LAST 30 DAYS (MONTH 3):")
    print(f"Trades: {len(trades_30d)} | Win Rate: {win_rate_30d:.2f}%")
    print(f"Gross P&L: Rs. {gross_30d:,.2f} | Charges: Rs. {charges_30d:,.2f}")
    print(f"Net P&L: Rs. {net_30d:,.2f} ({(net_30d / starting_capital) * 100.0:.2f}%)")
    print("=" * 60)
    
    return results

if __name__ == "__main__":
    run_backtest()
