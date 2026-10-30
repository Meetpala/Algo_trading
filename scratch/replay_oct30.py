import os
import sys
import time
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

load_dotenv()

from apex3.config import load_config
from apex3.broker.angel_data_provider import AngelDataProvider
from apex3.strategy.regime import evaluate_market_regime, MarketRegime
from apex3.strategy.signal import evaluate_candidate_signal, StockCandidateBar
from apex3.strategy.trail import compute_initial_stop_and_target
from apex3.strategy.sizer import compute_position_size
from apex3.indicators.atr import compute_atr
from apex3.indicators.vwap import compute_session_vwap
from apex3.models import Candle, Quote, OpeningRange, Side

IST = ZoneInfo("Asia/Kolkata")
cfg = load_config()

p = AngelDataProvider(
    api_key=os.getenv("ANGEL_API_KEY"),
    client_code=os.getenv("ANGEL_CLIENT_CODE"),
    pin=os.getenv("ANGEL_PIN"),
    totp_secret=os.getenv("ANGEL_TOTP_SECRET"),
)
p.authenticate()

target_date = date(2026, 10, 30)
symbols = [
    "HINDALCO", "JSWSTEEL", "RELIANCE", "MARUTI", "TCS",
    "TRENT", "BHARTIARTL", "JIOFIN", "TATASTEEL", "AXISBANK", "TATAPOWER"
]

print("Fetching NIFTY 50...")
nifty_candles = p.get_historical("NIFTY 50", "5minute")
oct30_nifty = [c for c in nifty_candles if c.timestamp.date() == target_date]
print(f"Oct 30 NIFTY 50 candles: {len(oct30_nifty)}")

stock_data = {}
for s in symbols:
    print(f"Fetching {s}...")
    candles = p.get_historical(s, "5minute")
    s_oct30 = [c for c in candles if c.timestamp.date() == target_date]
    stock_data[s] = {"all": candles, "oct30": s_oct30}
    time.sleep(0.35)

print("\nSimulating Oct 30 trading session...")

# Check regime throughout the day
regimes = []
for c in oct30_nifty:
    idx = nifty_candles.index(c)
    sub = nifty_candles[:idx + 1]
    res = evaluate_market_regime(sub, cfg.regime)
    regimes.append((c.timestamp.strftime("%H:%M"), res.regime, res.adx_val, res.ema_slope_val, res.gate.reason))

# Check Opening Range (09:15 - 09:30) for each stock
signals = []
for s in symbols:
    candles = stock_data[s]["all"]
    s_oct30 = stock_data[s]["oct30"]
    if len(s_oct30) < 3:
        print(f"Insufficient candles for {s}: {len(s_oct30)}")
        continue
    
    # OR is first 3 bars: 09:15, 09:20, 09:25
    or_bars = s_oct30[:3]
    or_high = max(b.high for b in or_bars)
    or_low = min(b.low for b in or_bars)
    or_vwap = sum((b.open + b.high + b.low + b.close)/4.0 * b.volume for b in or_bars) / max(1, sum(b.volume for b in or_bars))
    opening_range = OpeningRange(high=or_high, low=or_low, vwap=or_vwap, is_ready=True)

    # Check bars from 09:30 to 12:30
    for bar in s_oct30[3:]:
        bar_time = bar.timestamp.time()
        if bar_time > dtime(12, 30):
            break
        
        idx = candles.index(bar)
        history = candles[:idx + 1]
        
        # Nifty regime at this bar
        n_bar = next((c for c in oct30_nifty if c.timestamp == bar.timestamp), None)
        if n_bar:
            n_idx = nifty_candles.index(n_bar)
            mkt_regime = evaluate_market_regime(nifty_candles[:n_idx + 1], cfg.regime).regime
        else:
            mkt_regime = MarketRegime.NEUTRAL
        
        atr = compute_atr(history, 14)
        day_bars_so_far = [c for c in s_oct30 if c.timestamp <= bar.timestamp]
        vwap = compute_session_vwap(day_bars_so_far)
        
        # Check Long breakout
        if bar.close > or_high:
            q = Quote(symbol=s, last_price=bar.close, bid=bar.close - 0.05, ask=bar.close + 0.05, volume=bar.volume, timestamp=bar.timestamp)
            sig = evaluate_candidate_signal(
                candidate=StockCandidateBar(symbol=s, bar=bar, history=history, opening_range=opening_range, quote=q, atr=atr, session_vwap=vwap),
                market_regime=mkt_regime,
                config=cfg.signal,
                costs_config=cfg.costs,
                capital_base=15000.0,
            )
            signals.append((bar.timestamp.strftime("%H:%M"), s, "LONG", sig.passed, sig.reason, sig.candidate_score, mkt_regime.value))
        elif bar.close < or_low:
            q = Quote(symbol=s, last_price=bar.close, bid=bar.close - 0.05, ask=bar.close + 0.05, volume=bar.volume, timestamp=bar.timestamp)
            sig = evaluate_candidate_signal(
                candidate=StockCandidateBar(symbol=s, bar=bar, history=history, opening_range=opening_range, quote=q, atr=atr, session_vwap=vwap),
                market_regime=mkt_regime,
                config=cfg.signal,
                costs_config=cfg.costs,
                capital_base=15000.0,
            )
            signals.append((bar.timestamp.strftime("%H:%M"), s, "SHORT", sig.passed, sig.reason, sig.candidate_score, mkt_regime.value))

print("\n--- NIFTY 50 Regime on Oct 30 ---")
for t_str, reg, adx, slope, reason in regimes:
    if t_str.endswith(":00") or t_str.endswith(":30") or t_str in ("09:35", "12:30"):
        print(f"[{t_str}] Regime: {reg.value:<8} | ADX: {adx:.1f if adx else 0} | Slope: {slope:+.2f if slope else 0} | Reason: {reason}")

print("\n--- Signals Evaluated on Oct 30 (09:30 - 12:30) ---")
if not signals:
    print("Zero breakouts occurred beyond Opening Range on Oct 30.")
else:
    for t_str, s, side, passed, reason, score, reg in signals:
        status = "PASSED" if passed else f"FAILED: {reason}"
        print(f"[{t_str}] {s:<10} {side:<5} (Regime: {reg}) -> {status}")
