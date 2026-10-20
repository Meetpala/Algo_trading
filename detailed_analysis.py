"""Detailed APEX-3 Backtest Analysis Script

Evaluates:
1. Primary Watchlist (RELIANCE, TCS, INFY) - Capital Allocated (MIS 4x on 15k)
2. Primary Watchlist (RELIANCE, TCS, INFY) - Strict Config (0.25% risk per trade)
3. Scanner Universe (15 stocks ranked by relative volume & relative strength) - Capital Allocated
4. Sizing with 1.0% risk per trade (Rs. 150 risk)

Outputs comprehensive metrics, daily P&L, itemized charges, and trade logs.
"""
import json
import math
from datetime import datetime, time
import pandas as pd
import numpy as np

from backtest_engine import run_backtest, calculate_charges

ALL_15_STOCKS = [
    'RELIANCE', 'TCS', 'INFY', 'HDFCBANK', 'ICICIBANK',
    'BHARTIARTL', 'ITC', 'SBIN', 'HINDUNILVR', 'LT',
    'KOTAKBANK', 'BAJFINANCE', 'AXISBANK', 'MARUTI', 'ASIANPAINT'
]

def analyze_results(res, name=""):
    trades = res['trades']
    starting_cap = res['starting_capital']
    ending_cap = res['ending_capital']
    total_net = res['total_net_pnl']
    profit_pct = res['profit_pct']

    if not trades:
        return {
            'name': name,
            'total_trades': 0,
            'win_rate': 0.0,
            'total_gross': 0.0,
            'total_charges': 0.0,
            'total_net': 0.0,
            'profit_pct': 0.0,
            'message': 'No trades executed'
        }

    df_trades = pd.DataFrame(trades)
    wins = [t for t in trades if t['net_pnl'] > 0]
    losses = [t for t in trades if t['net_pnl'] < 0]
    evens = [t for t in trades if t['net_pnl'] == 0]

    win_rate = (len(wins) / len(trades)) * 100.0
    gross_profits = sum(t['gross_pnl'] for t in trades if t['gross_pnl'] > 0)
    gross_losses = sum(abs(t['gross_pnl']) for t in trades if t['gross_pnl'] < 0)
    gross_pnl = sum(t['gross_pnl'] for t in trades)

    # Itemized charges breakdown
    total_brokerage = sum(t['charges']['brokerage'] for t in trades)
    total_stt = sum(t['charges']['stt'] for t in trades)
    total_exchange_txn = sum(t['charges']['exchange_txn'] for t in trades)
    total_sebi = sum(t['charges']['sebi'] for t in trades)
    total_gst = sum(t['charges']['gst'] for t in trades)
    total_stamp_duty = sum(t['charges']['stamp_duty'] for t in trades)
    total_charges = sum(t['charges']['total_charges'] for t in trades)

    profit_factor = (gross_profits / gross_losses) if gross_losses > 0 else 999.0
    avg_win = (sum(t['net_pnl'] for t in wins) / len(wins)) if wins else 0.0
    avg_loss = (sum(t['net_pnl'] for t in losses) / len(losses)) if losses else 0.0
    risk_reward_ratio = (abs(avg_win / avg_loss)) if avg_loss != 0 else 0.0

    # Drawdown calculation
    equity_curve = [starting_cap]
    running = starting_cap
    for t in trades:
        running += t['net_pnl']
        equity_curve.append(running)
    peak = equity_curve[0]
    max_dd = 0.0
    max_dd_pct = 0.0
    for val in equity_curve:
        if val > peak:
            peak = val
        dd = peak - val
        dd_pct = (dd / peak) * 100.0 if peak > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
            max_dd_pct = dd_pct

    # Exit reason breakdown
    exit_reasons = {}
    for t in trades:
        r = t['exit_reason']
        exit_reasons[r] = exit_reasons.get(r, 0) + 1

    return {
        'name': name,
        'total_trades': len(trades),
        'winning_trades': len(wins),
        'losing_trades': len(losses),
        'break_even_trades': len(evens),
        'profit_probability_pct': round(win_rate, 2),
        'gross_profit_rupees': round(gross_profits, 2),
        'gross_loss_rupees': round(gross_losses, 2),
        'gross_pnl_rupees': round(gross_pnl, 2),
        'charges_breakdown': {
            'brokerage': round(total_brokerage, 2),
            'stt': round(total_stt, 2),
            'exchange_txn': round(total_exchange_txn, 2),
            'sebi': round(total_sebi, 2),
            'gst': round(total_gst, 2),
            'stamp_duty': round(total_stamp_duty, 2),
            'total_charges': round(total_charges, 2),
        },
        'net_pnl_rupees': round(total_net, 2),
        'profit_percentage': round(profit_pct, 2),
        'starting_capital': round(starting_cap, 2),
        'ending_capital': round(ending_cap, 2),
        'profit_factor': round(profit_factor, 2),
        'average_win_rupees': round(avg_win, 2),
        'average_loss_rupees': round(avg_loss, 2),
        'risk_reward_ratio': round(risk_reward_ratio, 2),
        'max_drawdown_rupees': round(max_dd, 2),
        'max_drawdown_pct': round(max_dd_pct, 2),
        'exit_reasons': exit_reasons,
        'trades': trades,
        'daily_stats': res['daily_stats']
    }

if __name__ == '__main__':
    print("--- 1. Primary Watchlist: Capital Allocated ---")
    res1 = run_backtest(['RELIANCE', 'TCS', 'INFY'], risk_mode='capital_allocated')
    an1 = analyze_results(res1, "Primary Watchlist (Allocated 15k Capital)")

    print("\n--- 2. Primary Watchlist: Strict Config (0.25% Risk) ---")
    res2 = run_backtest(['RELIANCE', 'TCS', 'INFY'], risk_mode='strict_config')
    an2 = analyze_results(res2, "Primary Watchlist (Strict 0.25% Risk)")

    print("\n--- 3. Primary Watchlist: 1.0% Risk Per Trade ---")
    res3 = run_backtest(['RELIANCE', 'TCS', 'INFY'], risk_mode='one_pct_risk')
    an3 = analyze_results(res3, "Primary Watchlist (1.0% Risk per Trade)")

    print("\n--- 4. Full Scanner Universe (15 Stocks): Capital Allocated ---")
    res4 = run_backtest(ALL_15_STOCKS, risk_mode='capital_allocated')
    an4 = analyze_results(res4, "Scanner Universe (15 Stocks, Allocated)")

    all_analyses = {
        'primary_allocated': an1,
        'primary_strict_config': an2,
        'primary_1pct_risk': an3,
        'scanner_allocated': an4,
    }

    with open('backtest_summary.json', 'w') as f:
        # Save without full trade lists for concise summary, but keep full trades in a separate file
        summary_clean = {}
        for k, v in all_analyses.items():
            clean_v = {ck: cv for ck, cv in v.items() if ck != 'trades'}
            summary_clean[k] = clean_v
        json.dump(summary_clean, f, indent=2)

    with open('all_trades.json', 'w') as f:
        json.dump(all_analyses, f, indent=2)

    print("\nSaved backtest_summary.json and all_trades.json successfully!")
    print(json.dumps(summary_clean, indent=2))
