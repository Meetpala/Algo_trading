"""Comprehensive 29/32-Stock Bull/Bear Audit and Portfolio Verification.

Runs across 6-Month (122 sessions) and 3-Month (57 sessions) windows:
1. Overall Portfolio Metrics (3M & 6M)
2. Bull (BUY) vs. Bear (SELL) breakdown (Trades, Win Rate, Net P&L)
3. Individual Stock Net P&L Ranking across all symbols
4. Test candidate additions to Extended Radar and verify friction < 26.0%
"""
import os
import sys
import json
import math
from datetime import time
from pathlib import Path
import pandas as pd

REPO_ROOT = r"c:\Users\itsme\OneDrive\Desktop\Algo Trading"
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
os.chdir(REPO_ROOT)

from apex3.config import load_config
from apex3.strategy.engine import build_active_radar
from backtest_3months import load_preparsed_data, run_fast_backtest

ELITE_10 = [
    "HINDALCO",
    "JSWSTEEL",
    "RELIANCE",
    "MARUTI",
    "ITC",
    "TCS",
    "SBIN",
    "TRENT",
    "KOTAKBANK",
    "SUNPHARMA",
]

def break_down_trades(trades: list[dict]) -> dict:
    if not trades:
        return {
            "total_trades": 0, "win_rate": 0.0, "net_pnl": 0.0,
            "long_trades": 0, "long_wr": 0.0, "long_net": 0.0,
            "short_trades": 0, "short_wr": 0.0, "short_net": 0.0,
        }
    
    longs = [t for t in trades if "BUY" in str(t.get("side", "")) or "LONG" in str(t.get("side", ""))]
    shorts = [t for t in trades if "SELL" in str(t.get("side", "")) or "SHORT" in str(t.get("side", ""))]
    
    long_wins = len([t for t in longs if t.get("net_pnl", 0) > 0])
    long_net = sum(t.get("net_pnl", 0) for t in longs)
    long_wr = (long_wins / len(longs) * 100) if longs else 0.0

    short_wins = len([t for t in shorts if t.get("net_pnl", 0) > 0])
    short_net = sum(t.get("net_pnl", 0) for t in shorts)
    short_wr = (short_wins / len(shorts) * 100) if shorts else 0.0

    tot_wins = len([t for t in trades if t.get("net_pnl", 0) > 0])
    tot_net = sum(t.get("net_pnl", 0) for t in trades)
    tot_wr = (tot_wins / len(trades) * 100) if trades else 0.0

    return {
        "total_trades": len(trades),
        "win_rate": round(tot_wr, 2),
        "net_pnl": round(tot_net, 2),
        "long_trades": len(longs),
        "long_wr": round(long_wr, 2),
        "long_net": round(long_net, 2),
        "short_trades": len(shorts),
        "short_wr": round(short_wr, 2),
        "short_net": round(short_net, 2),
    }

def main():
    print("=" * 82)
    print("TASK 2: 29/32-STOCK BULL/BEAR AUDIT & 3-TIER RADAR VERIFICATION")
    print("=" * 82)

    available_symbols = sorted(
        path.stem for path in Path("scratch_data").glob("*.csv")
        if path.stem != "NSEI"
    )
    print(f"Total available stock symbols in scratch_data: {len(available_symbols)}")

    symbols_needed = ["NSEI"] + available_symbols
    data = load_preparsed_data(symbols_needed)
    all_dates = sorted(set(data["NSEI"]["df"]["Datetime"].dt.date))
    dates_6m = all_dates
    dates_3m = all_dates[-57:]

    print(f"6-Month window: {len(dates_6m)} sessions ({dates_6m[0]} to {dates_6m[-1]})")
    print(f"3-Month window: {len(dates_3m)} sessions ({dates_3m[0]} to {dates_3m[-1]})")

    config = load_config()
    active_radar = build_active_radar(config.raw_dict)
    print(f"\nCurrent Active Radar in config.yaml ({len(active_radar)} symbols): {active_radar}")

    # 1. Run Active 3-Tier Radar Portfolio Backtest (6M and 3M)
    res_active_6m = run_fast_backtest(
        data=data,
        watchlist_symbols=active_radar,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=2,
        entry_cutoff_time=time(12, 30),
        session_dates=dates_6m,
    )
    res_active_3m = run_fast_backtest(
        data=data,
        watchlist_symbols=active_radar,
        target_r=2.0,
        use_fixed_target=True,
        trail_enabled=False,
        max_slots=2,
        entry_cutoff_time=time(12, 30),
        session_dates=dates_3m,
    )

    bb_active_6m = break_down_trades(res_active_6m["trades"])
    bb_active_3m = break_down_trades(res_active_3m["trades"])

    print("\n" + "=" * 82)
    print("1. OVERALL ACTIVE 3-TIER RADAR PORTFOLIO PERFORMANCE")
    print("=" * 82)
    portfolio_rows = [
        {
            "Window": "3-Month (57 Sessions)",
            "Trades": res_active_3m["total_trades"],
            "Win Rate": f"{res_active_3m['win_rate']:.2f}%",
            "Profit Factor": f"{res_active_3m['profit_factor']:.2f}",
            "Gross Wins": f"Rs. {res_active_3m['gross_profit']:.2f}",
            "Charges": f"Rs. {res_active_3m['total_charges']:.2f}",
            "Charges / Gross Wins %": f"{res_active_3m['friction_ratio_pct']:.2f}%",
            "Net Realized P&L": f"Rs. {res_active_3m['net_pnl']:+,.2f}",
        },
        {
            "Window": "6-Month (122 Sessions)",
            "Trades": res_active_6m["total_trades"],
            "Win Rate": f"{res_active_6m['win_rate']:.2f}%",
            "Profit Factor": f"{res_active_6m['profit_factor']:.2f}",
            "Gross Wins": f"Rs. {res_active_6m['gross_profit']:.2f}",
            "Charges": f"Rs. {res_active_6m['total_charges']:.2f}",
            "Charges / Gross Wins %": f"{res_active_6m['friction_ratio_pct']:.2f}%",
            "Net Realized P&L": f"Rs. {res_active_6m['net_pnl']:+,.2f}",
        },
    ]
    print(pd.DataFrame(portfolio_rows).to_string(index=False))

    print("\n" + "=" * 82)
    print("2. ACTIVE RADAR BULL (BUY) VS. BEAR (SELL) BREAKDOWN")
    print("=" * 82)
    bull_bear_rows = [
        {
            "Window": "3-Month (57 Sessions)",
            "Total Trades": bb_active_3m["total_trades"],
            "Total Net P&L": f"Rs. {bb_active_3m['net_pnl']:+,.2f}",
            "Bull Trades (BUY)": bb_active_3m["long_trades"],
            "Bull Win Rate": f"{bb_active_3m['long_wr']:.2f}%",
            "Bull Net P&L": f"Rs. {bb_active_3m['long_net']:+,.2f}",
            "Bear Trades (SELL)": bb_active_3m["short_trades"],
            "Bear Win Rate": f"{bb_active_3m['short_wr']:.2f}%",
            "Bear Net P&L": f"Rs. {bb_active_3m['short_net']:+,.2f}",
        },
        {
            "Window": "6-Month (122 Sessions)",
            "Total Trades": bb_active_6m["total_trades"],
            "Total Net P&L": f"Rs. {bb_active_6m['net_pnl']:+,.2f}",
            "Bull Trades (BUY)": bb_active_6m["long_trades"],
            "Bull Win Rate": f"{bb_active_6m['long_wr']:.2f}%",
            "Bull Net P&L": f"Rs. {bb_active_6m['long_net']:+,.2f}",
            "Bear Trades (SELL)": bb_active_6m["short_trades"],
            "Bear Win Rate": f"{bb_active_6m['short_wr']:.2f}%",
            "Bear Net P&L": f"Rs. {bb_active_6m['short_net']:+,.2f}",
        },
    ]
    print(pd.DataFrame(bull_bear_rows).to_string(index=False))

    # 3. Individual Stock Audit Across All Symbols
    print("\n" + "=" * 82)
    print("3. INDIVIDUAL STOCK STANDALONE AUDIT ACROSS ALL SYMBOLS")
    print("=" * 82)
    stock_rows = []
    for sym in available_symbols:
        is_core = sym in ELITE_10
        # 6M
        r6 = run_fast_backtest(
            data=data,
            watchlist_symbols=[sym],
            target_r=2.0,
            use_fixed_target=True,
            trail_enabled=False,
            max_slots=2,
            session_dates=dates_6m,
        )
        bb6 = break_down_trades(r6["trades"])
        # 3M
        r3 = run_fast_backtest(
            data=data,
            watchlist_symbols=[sym],
            target_r=2.0,
            use_fixed_target=True,
            trail_enabled=False,
            max_slots=2,
            session_dates=dates_3m,
        )
        bb3 = break_down_trades(r3["trades"])

        stock_rows.append({
            "symbol": sym,
            "tier": "Core (Elite 10)" if is_core else ("Extended" if sym in active_radar else "Universe"),
            "trades_6m": r6["total_trades"],
            "win_rate_6m": f"{r6['win_rate']:.1f}%",
            "gross_wins_6m": round(r6["gross_profit"], 2),
            "charges_6m": round(r6["total_charges"], 2),
            "net_pnl_6m": round(r6["net_pnl"], 2),
            "long_net_6m": bb6["long_net"],
            "short_net_6m": bb6["short_net"],
            "trades_3m": r3["total_trades"],
            "win_rate_3m": f"{r3['win_rate']:.1f}%",
            "gross_wins_3m": round(r3["gross_profit"], 2),
            "charges_3m": round(r3["total_charges"], 2),
            "net_pnl_3m": round(r3["net_pnl"], 2),
            "long_net_3m": bb3["long_net"],
            "short_net_3m": bb3["short_net"],
            "both_net_pos": (r6["net_pnl"] > 0 and r3["net_pnl"] > 0),
        })

    df_stocks = pd.DataFrame(stock_rows).sort_values(by="net_pnl_6m", ascending=False)
    print(df_stocks[[
        "symbol", "tier", "trades_6m", "win_rate_6m", "net_pnl_6m", "long_net_6m", "short_net_6m",
        "trades_3m", "win_rate_3m", "net_pnl_3m", "long_net_3m", "short_net_3m", "both_net_pos"
    ]].to_string(index=False))

    # 4. Check candidates outside Elite 10 strictly positive in BOTH windows
    print("\n" + "=" * 82)
    print("4. EVALUATION OF NON-ELITE 10 STOCKS STRICTLY POSITIVE IN BOTH 6M & 3M")
    print("=" * 82)
    candidates = [
        s for s in stock_rows
        if s["symbol"] not in ELITE_10 and s["both_net_pos"]
    ]
    print(f"Candidates found: {[c['symbol'] for c in candidates]}")
    for c in candidates:
        print(f"  {c['symbol']}: 6M Net P&L = Rs. {c['net_pnl_6m']:.2f} ({c['trades_6m']} trades) | "
              f"3M Net P&L = Rs. {c['net_pnl_3m']:.2f} ({c['trades_3m']} trades)")

    # 5. Test portfolio variations:
    # A: Elite 10 only (with noon cutoff / 12:30 cutoff)
    # B: Core 6 only (HINDALCO, JSWSTEEL, RELIANCE, MARUTI, TCS, TRENT)
    # C: Current Active Radar (Core 6 + BHARTIARTL, JIOFIN, TATASTEEL)
    # D: Core 6 + only candidates that improve Net P&L while friction < 26.0%
    print("\n" + "=" * 82)
    print("5. PORTFOLIO COMBINATION COMPARISON")
    print("=" * 82)
    core_6 = ["HINDALCO", "JSWSTEEL", "RELIANCE", "MARUTI", "TCS", "TRENT"]
    combos = {
        "Elite 10 Baseline": ELITE_10,
        "Core 6 Leaders": core_6,
        "Current Active Radar (Core 6 + BHARTIARTL, JIOFIN, TATASTEEL)": active_radar,
        "All Available Historical Symbols (32)": available_symbols,
    }
    combo_results = []
    for name, syms in combos.items():
        res6 = run_fast_backtest(
            data=data,
            watchlist_symbols=syms,
            target_r=2.0,
            use_fixed_target=True,
            trail_enabled=False,
            max_slots=2,
            entry_cutoff_time=time(12, 30),
            session_dates=dates_6m,
        )
        res3 = run_fast_backtest(
            data=data,
            watchlist_symbols=syms,
            target_r=2.0,
            use_fixed_target=True,
            trail_enabled=False,
            max_slots=2,
            entry_cutoff_time=time(12, 30),
            session_dates=dates_3m,
        )
        combo_results.append({
            "Portfolio": name,
            "6M Trades": res6["total_trades"],
            "6M Win Rate": f"{res6['win_rate']:.1f}%",
            "6M Friction %": f"{res6['friction_ratio_pct']:.2f}%",
            "6M Net P&L": f"Rs. {res6['net_pnl']:+,.2f}",
            "3M Trades": res3["total_trades"],
            "3M Win Rate": f"{res3['win_rate']:.1f}%",
            "3M Friction %": f"{res3['friction_ratio_pct']:.2f}%",
            "3M Net P&L": f"Rs. {res3['net_pnl']:+,.2f}",
        })
    print(pd.DataFrame(combo_results).to_string(index=False))

    # Save all output to JSON for documentation
    output_audit = {
        "active_portfolio_3m": portfolio_rows[0],
        "active_portfolio_6m": portfolio_rows[1],
        "bull_bear_3m": bull_bear_rows[0],
        "bull_bear_6m": bull_bear_rows[1],
        "stock_rankings": stock_rows,
        "combos": combo_results,
    }
    with open("task2_audit_results.json", "w") as f:
        json.dump(output_audit, f, indent=2)
    print("\nSaved Task 2 audit results to task2_audit_results.json")

if __name__ == "__main__":
    main()
