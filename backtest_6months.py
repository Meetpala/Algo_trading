"""Conservative six-month APEX-3 strategy and stock-universe audit.

The report compares the current Elite 10, its proposed exclusions and cutoff,
and the wider set of local historical symbols. All variants share the same
next-bar execution model, exact costs, and risk rules.
"""
from __future__ import annotations

import json
import math
import os
import sys
from datetime import time
from pathlib import Path

import pandas as pd

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

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


def _summary(result: dict) -> dict:
    """Keep the comparable headline metrics from one replay."""
    summary = {
        key: result[key]
        for key in (
            "total_trades",
            "winning_trades",
            "losing_trades",
            "win_rate",
            "profit_factor",
            "gross_pnl",
            "gross_profit",
            "gross_loss",
            "total_charges",
            "charge_breakdown",
            "friction_ratio_pct",
            "net_pnl",
            "profit_pct",
            "ending_capital",
        )
    }
    if not math.isfinite(summary["friction_ratio_pct"]):
        summary["friction_ratio_pct"] = None
    return summary


def _month_table(trades: list[dict]) -> list[dict]:
    if not trades:
        return []
    frame = pd.DataFrame(trades)
    frame["month"] = pd.to_datetime(frame["date"]).dt.to_period("M").astype(str)
    rows = []
    for month, group in frame.groupby("month", sort=True):
        gross_positive = group.loc[group["gross_pnl"] > 0, "gross_pnl"].sum()
        rows.append(
            {
                "month": month,
                "trades": int(len(group)),
                "win_rate_pct": round((group["net_pnl"] > 0).mean() * 100, 2),
                "gross_pnl": round(group["gross_pnl"].sum(), 2),
                "charges": round(group["total_charges"].sum(), 2),
                "friction_pct_of_gross_profit": (
                    round(group["total_charges"].sum() / gross_positive * 100, 2)
                    if gross_positive > 0 else None
                ),
                "net_pnl": round(group["net_pnl"].sum(), 2),
            }
        )
    return rows


def _symbol_table(trades: list[dict]) -> list[dict]:
    if not trades:
        return []
    frame = pd.DataFrame(trades)
    rows = []
    for symbol, group in frame.groupby("symbol", sort=True):
        rows.append(
            {
                "symbol": symbol,
                "trades": int(len(group)),
                "win_rate_pct": round((group["net_pnl"] > 0).mean() * 100, 2),
                "gross_pnl": round(group["gross_pnl"].sum(), 2),
                "charges": round(group["total_charges"].sum(), 2),
                "net_pnl": round(group["net_pnl"].sum(), 2),
            }
        )
    return sorted(rows, key=lambda row: row["net_pnl"], reverse=True)


def run_6month_audit() -> dict | None:
    print("=" * 82)
    print("APEX-3 CONSERVATIVE SIX-MONTH STRATEGY AUDIT")
    print("Next-bar fills | 4 bps assumed spread | 1.5 ATR structural stop | fixed 2R")
    print("=" * 82)

    available_symbols = sorted(
        path.stem for path in Path("scratch_data").glob("*.csv")
        if path.stem != "NSEI"
    )
    symbols_needed = ["NSEI"] + available_symbols
    for symbol in symbols_needed:
        path = os.path.join("scratch_data", f"{symbol}.csv")
        if not os.path.exists(path):
            print(f"[ERROR] Historical data file missing: {path}")
            return None

    print("Loading historical candles...")
    data = load_preparsed_data(symbols_needed)
    dates = sorted(set(data["NSEI"]["df"]["Datetime"].dt.date))
    print(f"Data span: {dates[0]} to {dates[-1]} ({len(dates)} trading sessions)")

    config = load_config()
    active_radar = build_active_radar(config.raw_dict)
    no_sbin_maruti = [s for s in ELITE_10 if s not in {"SBIN", "MARUTI"}]
    variants = {
        "Elite 10 baseline": (ELITE_10, None),
        "Exclude SBIN + MARUTI": (no_sbin_maruti, None),
        "Noon entry cutoff": (ELITE_10, time(12, 0)),
        "Exclude pair + noon cutoff": (no_sbin_maruti, time(12, 0)),
        "Active 3-Tier Radar (Config)": (active_radar, time(12, 0)),
        "All available historical symbols": (available_symbols, None),
    }

    full_results = {}
    for name, (symbols, cutoff) in variants.items():
        print(f"Running: {name}...")
        full_results[name] = run_fast_backtest(
            data=data,
            watchlist_symbols=symbols,
            target_r=2.0,
            use_fixed_target=True,
            trail_enabled=False,
            max_slots=2,
            entry_cutoff_time=cutoff,
            session_dates=dates,
        )

    comparison_rows = []
    for name, result in full_results.items():
        comparison_rows.append(
            {
                "Variant": name,
                "Trades": result["total_trades"],
                "Win rate": f"{result['win_rate']:.2f}%",
                "Profit factor": f"{result['profit_factor']:.2f}",
                "Gross P&L": f"Rs. {result['gross_pnl']:.2f}",
                "Charges": f"Rs. {result['total_charges']:.2f}",
                "Friction / gross profit": (
                    f"{result['friction_ratio_pct']:.2f}%"
                    if math.isfinite(result["friction_ratio_pct"]) else "∞"
                ),
                "Net P&L": f"Rs. {result['net_pnl']:.2f}",
            }
        )
    print("\nFULL-PERIOD COMPARISON (all figures include charges):")
    print(pd.DataFrame(comparison_rows).to_string(index=False))

    # A chronological stability check. It is exploratory because the stock
    # hypotheses were prompted by the supplied full-period attribution.
    midpoint = len(dates) // 2
    windows = {
        "First half": dates[:midpoint],
        "Second half": dates[midpoint:],
    }
    window_results = {}
    for strategy_name in (
        "Elite 10 baseline",
        "Exclude pair + noon cutoff",
        "Active 3-Tier Radar (Config)",
        "All available historical symbols",
    ):
        symbols, cutoff = variants[strategy_name]
        for window_name, window_dates in windows.items():
            print(f"Running date split: {strategy_name} / {window_name}...")
            result = run_fast_backtest(
                data=data,
                watchlist_symbols=symbols,
                target_r=2.0,
                use_fixed_target=True,
                trail_enabled=False,
                max_slots=2,
                entry_cutoff_time=cutoff,
                session_dates=window_dates,
            )
            window_results[f"{strategy_name} / {window_name}"] = _summary(result)

    print("\nDATE-ORDERED STABILITY CHECK (exploratory, not an independent holdout):")
    split_rows = []
    for name, metrics in window_results.items():
        split_rows.append(
            {
                "Window": name,
                "Trades": metrics["total_trades"],
                "Win rate": f"{metrics['win_rate']:.2f}%",
                "Profit factor": f"{metrics['profit_factor']:.2f}",
                "Charges": f"Rs. {metrics['total_charges']:.2f}",
                "Net P&L": f"Rs. {metrics['net_pnl']:.2f}",
            }
        )
    print(pd.DataFrame(split_rows).to_string(index=False))

    baseline = full_results["Elite 10 baseline"]
    print("\nBASELINE ITEMIZED CHARGES:")
    for label, amount in baseline["charge_breakdown"].items():
        print(f"  {label}: Rs. {amount:.2f}")
    print(f"  Total: Rs. {baseline['total_charges']:.2f}")
    print("\nBASELINE MONTHLY PERFORMANCE:")
    print(pd.DataFrame(_month_table(baseline["trades"])).to_string(index=False))
    print("\nBASELINE SYMBOL ATTRIBUTION:")
    print(pd.DataFrame(_symbol_table(baseline["trades"])).to_string(index=False))

    payload = {
        "audit": {
            "data_start": str(dates[0]),
            "data_end": str(dates[-1]),
            "sessions": len(dates),
            "available_historical_symbols": available_symbols,
            "starting_capital": 15000.0,
            "execution_spread_bps": 4.0,
            "entry_cutoffs": {"default": "13:00", "noon_variant": "12:00"},
            "notes": [
                "All variants use next-bar open fills with adverse spread and exact round-trip charges.",
                "Friction ratio is charges divided by positive gross profits.",
                "The date split is exploratory, not an independent holdout, because the stock hypotheses came from the supplied period attribution.",
                "The expanded-universe run uses every locally available stock CSV and is subject to survivorship and data-availability bias.",
                "No live configuration or stock radar was changed.",
            ],
        },
        "variants": {
            name: {
                "summary": _summary(result),
                "monthly": _month_table(result["trades"]),
                "by_symbol": _symbol_table(result["trades"]),
                "trades": result["trades"],
            }
            for name, result in full_results.items()
        },
        "date_ordered_stability_check": window_results,
    }
    output_path = "six_month_conservative_audit.json"
    with open(output_path, "w", encoding="utf-8") as output_file:
        json.dump(payload, output_file, indent=2, allow_nan=False)
    active_res = full_results["Active 3-Tier Radar (Config)"]
    assert active_res["net_pnl"] > 336.66, f"Active radar net P&L Rs. {active_res['net_pnl']} not > baseline +Rs. 336.66"
    assert active_res["friction_ratio_pct"] < 26.0, f"Active radar friction {active_res['friction_ratio_pct']}% >= 26.0%"
    print(f"\n[PASS] 6-Month Active Radar Net P&L (Rs. {active_res['net_pnl']:.2f} > Rs. 336.66) and friction ({active_res['friction_ratio_pct']:.2f}% < 26.0%) verified")

    print(f"\nDetailed audit written to {output_path}")
    return payload


if __name__ == "__main__":
    run_6month_audit()
