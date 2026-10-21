import os, sys
sys.path.insert(0, os.getcwd())
import scratch.test_pipeline_clean as tpc

from apex3.strategy import regime
from apex3.indicators.adx import compute_adx

print("Testing different ADX definitions on 3-month and 6-month data:")

# Test A: ADX >= 22 with ADX[t] >= ADX[t-3] (3-bar lookback trend)
def eval_adx_3bar(closes, highs, lows, config):
    adx_dict = compute_adx(highs, lows, closes, period=config.adx_period)
    adx_series = adx_dict.get("adx", [])
    if not adx_series: return False
    latest_adx = adx_series[-1]
    if latest_adx < config.min_adx: return False
    # Rising over 3 bars
    prev_3 = adx_series[-3] if len(adx_series) >= 3 else adx_series[0]
    return latest_adx >= prev_3 or latest_adx >= 25.0

# Test B: ADX >= 22 strictly (Wilder trend)
def eval_adx_wilder(closes, highs, lows, config):
    adx_dict = compute_adx(highs, lows, closes, period=config.adx_period)
    adx_series = adx_dict.get("adx", [])
    if not adx_series: return False
    return adx_series[-1] >= config.min_adx

print("Testing simulation with Test A (ADX >= 22 & 3-bar rising or >= 25):")
# Patch regime.py for test
import apex3.strategy.regime as r_mod
orig_fn = r_mod.evaluate_market_regime

def patch_a(target, cfg_or_config, *args, **kwargs):
    cfg = cfg_or_config if isinstance(cfg_or_config, regime.RegimeConfig) else regime.RegimeConfig()
    cfg.require_rising_adx = False
    res = orig_fn(target, cfg, *args, **kwargs)
    # Check if we should enforce 3-bar rising
    if res.regime != regime.MarketRegime.NEUTRAL and hasattr(res, 'adx_val') and res.adx_val is not None:
        if res.adx_val < 22.0:
            return regime.RegimeEvaluation(regime=regime.MarketRegime.NEUTRAL, gate=regime.GateResult(name="MARKET_REGIME", status=regime.GateStatus.FAIL, reason="ADX < 22.0"))
    return res

r_mod.evaluate_market_regime = patch_a

r3 = tpc.simulate(tpc.three_month_dates, target_r=2.0)
print(f"3-Month: Trades: {r3['trades']} | Win Rate: {r3['win_rate']}% | Gross: Rs. {r3['gross_pnl']} | Net: Rs. {r3['net_pnl']} | PF: {r3['profit_factor']}")

r6 = tpc.simulate(tpc.all_dates, target_r=2.0)
print(f"6-Month: Trades: {r6['trades']} | Win Rate: {r6['win_rate']}% | Gross: Rs. {r6['gross_pnl']} | Net: Rs. {r6['net_pnl']} | PF: {r6['profit_factor']}")
