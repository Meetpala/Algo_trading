import os, sys
sys.path.insert(0, os.getcwd())
import scratch.test_relaxation as tr

# Let's test different ADX conditions in evaluate_market_regime
from apex3.strategy import regime

orig_eval = regime.evaluate_market_regime

def run_with_adx_rule(adx_thresh=30.0):
    def patched_eval(target, cfg_or_config, *args, **kwargs):
        res = orig_eval(target, cfg_or_config, *args, **kwargs)
        # Check if candle series
        if hasattr(res, 'adx_val') and res.adx_val is not None:
            # If it was marked NEUTRAL only because of rising ADX
            pass
        return res
    pass
