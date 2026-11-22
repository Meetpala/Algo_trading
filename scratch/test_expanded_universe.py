import sys, os
sys.path.insert(0, os.path.abspath('.'))
import glob
import json
import pandas as pd
from backtest_3months import (
    load_config, load_preparsed_data, run_fast_backtest
)

def test_expanded():
    config = load_config()
    
    # Get all available symbols
    files = glob.glob('scratch_data/*.csv')
    available = set([os.path.basename(f).replace('.csv', '') for f in files])
    available.discard('NSEI')
    
    # Check share price of all symbols to filter out untradable >Rs. 7500 stocks
    # e.g., MRF, BOSCHLTD, etc.
    valid_symbols = []
    prices = {}
    for sym in sorted(available):
        df = pd.read_csv(f'scratch_data/{sym}.csv')
        last_close = df.iloc[-1]['Close']
        prices[sym] = last_close
        # If price > 7500, Rs. 120 / (0.55% * 7500) = 120 / 41.25 = 2.9 shares max
        # If price > 15000, max shares = 0 or 1
        if last_close <= 6500:
            valid_symbols.append(sym)
            
    print(f"Total available symbols: {len(available)}")
    print(f"Symbols with share price <= Rs. 6,500: {len(valid_symbols)}")
    print("Excluded high-price symbols:", [s for s in available if s not in valid_symbols])
    
    # Let's test 3 universes:
    # 1. Current 20 stocks
    # 2. Top 35 liquid stocks
    # 3. Full 50-stock liquid universe
    
    current_20 = [
        "TRENT", "HDFCLIFE", "ADANIENT", "HINDUNILVR", "HINDALCO",
        "JSWSTEEL", "BHARTIARTL", "KOTAKBANK", "BAJAJFINSV", "AXISBANK",
        "BAJFINANCE", "CIPLA", "ONGC", "M&M", "HEROMOTOCO",
        "RELIANCE", "NESTLEIND", "TATASTEEL", "ITC", "HDFCBANK"
    ]
    
    # Test top 35: add 15 more high-volume liquid names
    addon_15 = [
        "INFY", "TCS", "SBIN", "ICICIBANK", "LT", "SUNPHARMA",
        "TATAMOTORS", "COALINDIA", "NTPC", "POWERGRID", "TITAN",
        "VEDL", "TATAPOWER", "CHOLAFIN", "DRREDDY"
    ]
    universe_35 = list(set(current_20 + [s for s in addon_15 if s in valid_symbols]))
    universe_50 = list(valid_symbols)
    
    all_syms_to_load = list(set(['NSEI'] + universe_50))
    print(f"\nPre-loading {len(all_syms_to_load)} symbols into memory...")
    preloaded = load_preparsed_data(all_syms_to_load)
    all_dates = sorted(set(preloaded['NSEI']['df']['Datetime'].dt.date))
    print(f"Data loaded across {len(all_dates)} sessions.")
    
    results = {}
    for name, u in [("Current 20 Stocks", current_20), ("Expanded 35 Stocks", universe_35), ("Full 50+ Liquid Universe", universe_50)]:
        print(f"\nRunning full 1-year backtest for '{name}' ({len(u)} stocks)...")
        res = run_fast_backtest(
            data=preloaded,
            watchlist_symbols=u,
            target_r=2.0,
            use_fixed_target=True,
            trail_enabled=False,
            max_slots=2,
            session_dates=all_dates,
        )
        results[name] = {
            "stocks_count": len(u),
            "total_trades": res['total_trades'],
            "winning_trades": res['winning_trades'],
            "losing_trades": res['losing_trades'],
            "win_rate": res['win_rate'],
            "profit_factor": res['profit_factor'],
            "gross_profit": res['gross_profit'],
            "gross_loss": res['gross_loss'],
            "charges": res['total_charges'],
            "net_pnl": res['net_pnl'],
            "net_roi": res['profit_pct'],
            "friction_pct": res['friction_ratio_pct'],
        }
        
    print("\n" + "=" * 90)
    print("             UNIVERSE EXPANSION COMPARISON MATRIX (1-YEAR)")
    print("=" * 90)
    print(f"{'Universe Name':<25} | {'Stocks':<6} | {'Trades':<6} | {'Win Rate':<8} | {'PF':<5} | {'Gross PnL':<10} | {'Charges':<8} | {'Net PnL':<10} | {'ROI %':<6} | {'Friction':<8}")
    print("-" * 90)
    for k, v in results.items():
        print(f"{k:<25} | {v['stocks_count']:<6d} | {v['total_trades']:<6d} | {v['win_rate']:<7.1f}% | {v['profit_factor']:<5.2f} | Rs. {v['gross_profit']-v['gross_loss']:<6.1f} | Rs. {v['charges']:<5.1f} | Rs. {v['net_pnl']:<7.1f} | {v['net_roi']:<5.2f}% | {v['friction_pct']:<7.1f}%")
    print("=" * 90)

    with open('scratch/expansion_comparison.json', 'w') as f:
        json.dump(results, f, indent=2)

if __name__ == '__main__':
    test_expanded()
