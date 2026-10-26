"""Conservative APEX-3 1-Year Historical Audit Engine.

Evaluates performance across the full 1-year historical window (~246 trading sessions)
under true institutional execution constraints:
- Completed 5-minute bar signals fill on the next aligned bar
- Fixed 2.0R targets
- Structural swing stop-loss
- Fee-protected breakeven lock at +1.60R (locking +0.60R)
- Strict ₹15,000 base capital sizing with max ₹120 loss/trade risk cap
- Hard daily loss limit (-₹240)
- Itemized Zerodha and statutory transaction charges
"""
from __future__ import annotations

import os
import math
import json
from datetime import datetime, date
import numpy as np
import pandas as pd

from apex3.config import load_config
from apex3.strategy.engine import build_active_radar
from backtest_3months import (
    load_preparsed_data,
    run_fast_backtest,
    friction_ratio_pct,
)


def compute_drawdown_series(daily_stats: list[dict], starting_capital: float = 15000.0):
    """Compute peak-to-trough equity curve drawdown statistics."""
    equity = starting_capital
    peak = starting_capital
    max_dd_inr = 0.0
    max_dd_pct = 0.0
    curve = []

    for d in daily_stats:
        equity = d.get("closing_capital", equity + d.get("day_pnl", 0.0))
        curve.append((d["date"], equity))
        if equity > peak:
            peak = equity
        dd_inr = peak - equity
        dd_pct = (dd_inr / peak) * 100.0 if peak > 0 else 0.0
        if dd_inr > max_dd_inr:
            max_dd_inr = dd_inr
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    return {
        "max_drawdown_inr": round(max_dd_inr, 2),
        "max_drawdown_pct": round(max_dd_pct, 2),
        "peak_equity": round(peak, 2),
        "final_equity": round(equity, 2),
        "equity_curve": curve,
    }


def compute_sharpe_ratio(daily_stats: list[dict], risk_free_rate_pct: float = 6.5) -> float:
    """Calculate annualized Sharpe ratio from daily realized returns."""
    daily_pnls = [d.get("day_pnl", 0.0) for d in daily_stats]
    if not daily_pnls or len(daily_pnls) < 10:
        return 0.0

    daily_returns = [pnl / 15000.0 for pnl in daily_pnls]
    mean_ret = np.mean(daily_returns)
    std_ret = np.std(daily_returns, ddof=1)
    if std_ret <= 0.0:
        return 0.0

    rf_daily = (risk_free_rate_pct / 100.0) / 252.0
    excess_daily = mean_ret - rf_daily
    annualized_sharpe = (excess_daily / std_ret) * math.sqrt(252)
    return round(float(annualized_sharpe), 2)


def main():
    config = load_config()
    watchlist = build_active_radar(config.raw_dict)
    all_symbols = ['NSEI'] + watchlist

    print("=" * 70)
    print("   APEX-3 FULL 1-YEAR CONSERVATIVE BACKTEST REPLAY AUDIT         ")
    print("=" * 70)
    print(f"Watchlist ({len(watchlist)} symbols): {', '.join(watchlist)}")
    print(f"Pre-loading 1-year historical data for {len(all_symbols)} instruments...")
    preloaded = load_preparsed_data(all_symbols)
    print("Pre-parsed data loaded successfully into memory!\n")

    # Get all available trading sessions for the benchmark
    nsei_dates = sorted(set(preloaded['NSEI']['df']['Datetime'].dt.date))
    print(f"Available historical benchmark trading sessions: {len(nsei_dates)}")
    print(f"Date Range: {nsei_dates[0]} to {nsei_dates[-1]}")

    print("\nExecuting APEX-3 conservative next-bar execution replay across all sessions...")
    res_1y = run_fast_backtest(
        data=preloaded,
        watchlist_symbols=watchlist,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=2,
        session_dates=nsei_dates,
    )

    dd_stats = compute_drawdown_series(res_1y['daily_stats'], starting_capital=15000.0)
    sharpe = compute_sharpe_ratio(res_1y['daily_stats'], risk_free_rate_pct=6.5)

    starting_capital = 15000.0
    ending_capital = res_1y['ending_capital']
    total_trades = res_1y['total_trades']
    win_trades = res_1y['winning_trades']
    loss_trades = res_1y['losing_trades']
    win_rate = res_1y['win_rate']
    gross_pnl = res_1y['gross_pnl']
    tot_charges = res_1y['total_charges']
    net_pnl = res_1y['net_pnl']
    profit_pct = res_1y['profit_pct']
    profit_factor = res_1y['profit_factor']
    friction_pct = res_1y['friction_ratio_pct']

    trades = res_1y['trades']
    df_trades = pd.DataFrame(trades)
    if not df_trades.empty:
        df_trades['date_dt'] = pd.to_datetime(df_trades['date'])

    avg_win = round(float(np.mean([t['net_pnl'] for t in trades if t['net_pnl'] > 0])), 2) if win_trades > 0 else 0.0
    avg_loss = round(float(np.mean([abs(t['net_pnl']) for t in trades if t['net_pnl'] <= 0])), 2) if loss_trades > 0 else 0.0
    expectancy = round(net_pnl / total_trades, 2) if total_trades > 0 else 0.0

    print("\n" + "=" * 70)
    print("   APEX-3 1-YEAR CONSERVATIVE AUDIT REPORT (5-MIN NEXT-BAR FILLS) ")
    print("=" * 70)
    print(f"Evaluation Period:             {nsei_dates[0]} to {nsei_dates[-1]} ({len(nsei_dates)} sessions)")
    print(f"Starting Base Capital:         Rs. {starting_capital:,.2f}")
    print(f"Ending Portfolio Equity:       Rs. {ending_capital:,.2f}")
    print(f"Net Realized P&L:              Rs. {net_pnl:+,.2f} ({profit_pct:+.2f}%)")
    print(f"Total Completed Trades:        {total_trades}")
    print(f"Winning Trades / Losing:       {win_trades} Wins | {loss_trades} Losses")
    print(f"Win Rate (Probability):        {win_rate:.2f}%")
    print(f"Profit Factor:                 {profit_factor:.2f}")
    print(f"Average Win:                   Rs. +{avg_win:.2f}")
    print(f"Average Loss:                  Rs. -{avg_loss:.2f}")
    print(f"Net Expectancy / Trade:        Rs. {expectancy:+.2f}")
    print(f"Gross P&L:                     Rs. {gross_pnl:+,.2f}")
    print(f"Zerodha & Statutory Charges:   Rs. {tot_charges:,.2f}")
    print("  - Charges Breakdown:         " + " | ".join(
        f"{k}: Rs. {v:.2f}" for k, v in res_1y['charge_breakdown'].items()
    ))
    print(f"Statutory Friction Ratio:      {friction_pct:.2f}% of gross winning profits (Limit: < 26.0%)")
    print(f"Max Portfolio Drawdown:        Rs. {dd_stats['max_drawdown_inr']:.2f} ({dd_stats['max_drawdown_pct']:.2f}%)")
    print(f"Annualized Sharpe Ratio:       {sharpe}")
    print("=" * 70 + "\n")

    def summarize_subset(sub_df, name):
        if sub_df.empty:
            return {'Period': name, 'Trades': 0, 'Win Rate': '0.0%', 'Gross (Rs)': 0.0, 'Charges (Rs)': 0.0, 'Net (Rs)': 0.0, 'ROI (%)': '0.00%'}
        w = len(sub_df[sub_df['net_pnl'] > 0])
        tot = len(sub_df)
        g = round(float(sub_df['gross_pnl'].sum()), 2)
        c = round(float(sub_df['total_charges'].sum()), 2)
        n = round(g - c, 2)
        return {
            'Period': name,
            'Trades': tot,
            'Win Rate': f"{round(w / tot * 100.0, 1)}%",
            'Gross (Rs)': f"{g:+,.2f}",
            'Charges (Rs)': f"{c:,.2f}",
            'Net (Rs)': f"{n:+,.2f}",
            'ROI (%)': f"{round((n / starting_capital) * 100.0, 2):+.2f}%"
        }

    # Month by month breakdown
    monthly_rows = []
    if not df_trades.empty:
        for period, m_trades in df_trades.groupby(df_trades['date_dt'].dt.to_period('M')):
            monthly_rows.append(summarize_subset(m_trades, str(period)))
    monthly_rows.append(summarize_subset(df_trades, f"Full 1-Year Total ({len(nsei_dates)} sess)"))

    print("MONTHLY BREAKDOWN TABLE:")
    print(pd.DataFrame(monthly_rows).to_string(index=False))
    print()

    # Quarter by quarter breakdown
    quarterly_rows = []
    if not df_trades.empty:
        for q_period, q_trades in df_trades.groupby(df_trades['date_dt'].dt.to_period('Q')):
            quarterly_rows.append(summarize_subset(q_trades, str(q_period)))
    print("QUARTERLY BREAKDOWN TABLE:")
    print(pd.DataFrame(quarterly_rows).to_string(index=False))
    print()

    # Symbol by symbol contribution
    symbol_rows = []
    if not df_trades.empty:
        for sym, s_trades in df_trades.groupby('symbol'):
            w = len(s_trades[s_trades['net_pnl'] > 0])
            tot = len(s_trades)
            g = round(float(s_trades['gross_pnl'].sum()), 2)
            c = round(float(s_trades['total_charges'].sum()), 2)
            n = round(g - c, 2)
            symbol_rows.append({
                'Symbol': sym,
                'Trades': tot,
                'Win Rate': f"{round(w / tot * 100.0, 1)}%",
                'Gross (Rs)': f"{g:+,.2f}",
                'Charges (Rs)': f"{c:,.2f}",
                'Net (Rs)': f"{n:+,.2f}",
            })
    print("SYMBOL CONTRIBUTION TABLE:")
    print(pd.DataFrame(symbol_rows).to_string(index=False))
    print()

    # Dump full JSON audit report
    out_json = "one_year_conservative_audit.json"
    with open(out_json, "w") as f:
        json.dump({
            "audit_period": f"{nsei_dates[0]} to {nsei_dates[-1]}",
            "sessions_count": len(nsei_dates),
            "starting_capital": starting_capital,
            "ending_capital": ending_capital,
            "net_pnl": net_pnl,
            "profit_pct": profit_pct,
            "total_trades": total_trades,
            "win_rate": win_rate,
            "profit_factor": profit_factor,
            "statutory_friction_pct": friction_pct,
            "max_drawdown_inr": dd_stats["max_drawdown_inr"],
            "max_drawdown_pct": dd_stats["max_drawdown_pct"],
            "sharpe_ratio": sharpe,
            "charges_breakdown": res_1y["charge_breakdown"],
            "monthly_summary": monthly_rows,
            "quarterly_summary": quarterly_rows,
            "symbol_summary": symbol_rows,
            "trades": trades,
        }, f, indent=2)
    print(f"[SAVED] Complete audit results written to {out_json}")


if __name__ == "__main__":
    main()
