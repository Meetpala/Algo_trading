"""APEX-3 Market Regime Filter.

Evaluates broad-market regime using NIFTY 50 OHLC trend data (EMA-20 slope, ADX, VWAP alignment).
Supports both BenchmarkSnapshot (high-speed radar scanning) and Candle series.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional, Union

from apex3.config import RegimeConfig
from apex3.indicators.adx import compute_adx
from apex3.indicators.ema import compute_ema, ema_slope
from apex3.models import Candle, GateResult, GateStatus, Quote


class MarketRegime(str, Enum):
    """Broad-market trend regime."""
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


@dataclass
class BenchmarkSnapshot:
    close: float
    ema20: float
    prev_ema20: float
    session_vwap: float
    adx14: float
    prev_adx14: float
    ema50: float = 0.0


@dataclass(frozen=True)
class RegimeEvaluation:
    """Detailed evaluation result for market regime."""
    regime: MarketRegime
    gate: GateResult
    current_close: Optional[float] = None
    ema_20: Optional[float] = None
    ema_50: Optional[float] = None
    ema_slope_val: Optional[float] = None
    adx_val: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None


def evaluate_market_regime(
    target: Union[BenchmarkSnapshot, list[Candle]],
    cfg_or_config: Union[dict, RegimeConfig],
    benchmark_quote: Optional[Quote] = None,
    stale_threshold_sec: float = 2.0,
) -> Union[MarketRegime, RegimeEvaluation]:
    """Evaluate NIFTY 50 5-minute regime.

    Supports:
      1. evaluate_market_regime(snapshot: BenchmarkSnapshot, cfg: dict) -> MarketRegime
      2. evaluate_market_regime(candles_5m: list[Candle], config: RegimeConfig) -> RegimeEvaluation
    """
    if isinstance(target, BenchmarkSnapshot):
        cfg = cfg_or_config if isinstance(cfg_or_config, dict) else {}
        regime_cfg = cfg.get("regime_filter", cfg.get("regime", {}))
        min_adx = float(regime_cfg.get("min_adx", 22.0))
        require_vwap = bool(regime_cfg.get("require_vwap_alignment", True))
        require_rising_adx = bool(regime_cfg.get("require_rising_adx", True))

        snapshot_slope = target.ema20 - target.prev_ema20
        adx_strong = target.adx14 >= min_adx
        adx_rising = (target.adx14 > target.prev_adx14) if require_rising_adx else True

        if not (adx_strong and adx_rising):
            return MarketRegime.NEUTRAL

        # Bullish Regime Check: Close > EMA20 > EMA50, EMA20 Slope > 0, VWAP aligned
        vwap_bull_ok = (target.close > target.session_vwap) if (require_vwap and target.session_vwap > 0) else True
        ema50_bull_ok = (target.ema20 > target.ema50) if target.ema50 > 0 else True
        if target.close > target.ema20 and ema50_bull_ok and snapshot_slope > 0 and vwap_bull_ok:
            return MarketRegime.BULLISH

        # Bearish Regime Check: Close < EMA20 < EMA50, EMA20 Slope < 0, VWAP aligned
        vwap_bear_ok = (target.close < target.session_vwap) if (require_vwap and target.session_vwap > 0) else True
        ema50_bear_ok = (target.ema20 < target.ema50) if target.ema50 > 0 else True
        if target.close < target.ema20 and ema50_bear_ok and snapshot_slope < 0 and vwap_bear_ok:
            return MarketRegime.BEARISH

        return MarketRegime.NEUTRAL

    # Candle series processing
    candles_5m = target
    config = cfg_or_config if isinstance(cfg_or_config, RegimeConfig) else RegimeConfig()

    # Check quote freshness if provided
    if benchmark_quote is not None:
        if not benchmark_quote.is_fresh:
            return RegimeEvaluation(
                regime=MarketRegime.NEUTRAL,
                gate=GateResult(
                    name="MARKET_REGIME",
                    status=GateStatus.FAIL,
                    reason="Benchmark quote is marked stale",
                    data={"code": "STALE_BENCHMARK_DATA"},
                ),
            )

    min_required_bars = max(config.ema_period + config.ema_slope_lookback + 2, config.adx_period * 2 + 5)
    completed_candles = [c for c in candles_5m if c.is_complete]

    if len(completed_candles) < min_required_bars:
        return RegimeEvaluation(
            regime=MarketRegime.NEUTRAL,
            gate=GateResult(
                name="MARKET_REGIME",
                status=GateStatus.FAIL,
                reason=f"Insufficient completed 5m benchmark candles ({len(completed_candles)}/{min_required_bars})",
                data={"code": "INSUFFICIENT_BENCHMARK_DATA", "count": len(completed_candles)},
            ),
        )

    closes = [c.close for c in completed_candles]
    highs = [c.high for c in completed_candles]
    lows = [c.low for c in completed_candles]

    ema_series = compute_ema(closes, period=config.ema_period)
    if not ema_series or len(ema_series) < config.ema_slope_lookback:
        return RegimeEvaluation(
            regime=MarketRegime.NEUTRAL,
            gate=GateResult(
                name="MARKET_REGIME",
                status=GateStatus.FAIL,
                reason="Could not compute EMA series",
                data={"code": "EMA_CALCULATION_ERROR"},
            ),
        )

    latest_close = closes[-1]
    latest_ema = ema_series[-1]
    slope = ema_slope(ema_series, lookback=config.ema_slope_lookback)

    # Compute EMA50 if configured and sufficient candles are available
    slow_period = getattr(config, "ema_slow_period", 50)
    latest_ema50: Optional[float] = None
    dual_ema_bull = True
    dual_ema_bear = True
    if len(closes) >= slow_period:
        ema_slow_series = compute_ema(closes, period=slow_period)
        if ema_slow_series:
            latest_ema50 = ema_slow_series[-1]
            dual_ema_bull = (latest_ema > latest_ema50)
            dual_ema_bear = (latest_ema < latest_ema50)

    adx_dict = compute_adx(highs, lows, closes, period=config.adx_period)
    adx_series = adx_dict.get("adx", [])
    plus_di_series = adx_dict.get("plus_di", [])
    minus_di_series = adx_dict.get("minus_di", [])

    if not adx_series:
        return RegimeEvaluation(
            regime=MarketRegime.NEUTRAL,
            gate=GateResult(
                name="MARKET_REGIME",
                status=GateStatus.FAIL,
                reason="Could not compute ADX series",
                data={"code": "ADX_CALCULATION_ERROR"},
            ),
        )

    latest_adx = adx_series[-1]
    prev_adx = adx_series[-2] if len(adx_series) >= 2 else latest_adx
    latest_plus_di = plus_di_series[-1] if plus_di_series else None
    latest_minus_di = minus_di_series[-1] if minus_di_series else None

    if getattr(config, "require_rising_adx", True):
        adx_rising = (latest_adx > prev_adx) if latest_adx < 28.0 else True
    else:
        adx_rising = True
    is_trending = (latest_adx >= config.min_adx) and adx_rising

    is_bullish = (latest_close > latest_ema) and dual_ema_bull and (slope > 0) and is_trending
    is_bearish = (latest_close < latest_ema) and dual_ema_bear and (slope < 0) and is_trending

    # Directional Indicator check (+DI vs -DI)
    if latest_plus_di is not None and latest_minus_di is not None:
        if is_bullish and latest_plus_di <= latest_minus_di:
            is_bullish = False
        if is_bearish and latest_minus_di <= latest_plus_di:
            is_bearish = False

    # Session VWAP / TWAP alignment
    if getattr(config, "require_vwap_alignment", True) and len(completed_candles) >= 3:
        target_date = completed_candles[-1].timestamp.date()
        today_candles = [c for c in completed_candles if c.timestamp.date() == target_date]
        if today_candles:
            tot_vol = sum(c.volume for c in today_candles)
            if tot_vol > 0:
                from apex3.indicators.vwap import compute_session_vwap
                vwap_val = compute_session_vwap(
                    [c.high for c in today_candles],
                    [c.low for c in today_candles],
                    [c.close for c in today_candles],
                    [c.volume for c in today_candles],
                )
            else:
                # Zero-volume index spot candles: compute Typical-Price Session TWAP
                vwap_val = sum((c.high + c.low + c.close) / 3.0 for c in today_candles) / len(today_candles)

            if vwap_val > 0:
                if is_bullish and latest_close <= vwap_val:
                    is_bullish = False
                if is_bearish and latest_close >= vwap_val:
                    is_bearish = False

    details = {
        "close": latest_close,
        "ema": round(latest_ema, 2),
        "ema50": round(latest_ema50, 2) if latest_ema50 is not None else None,
        "slope": slope,
        "adx": round(latest_adx, 2),
        "prev_adx": round(prev_adx, 2),
        "min_adx": config.min_adx,
    }

    if is_bullish:
        ema50_str = f" > EMA-50 ({latest_ema50:.2f})" if latest_ema50 is not None else ""
        return RegimeEvaluation(
            regime=MarketRegime.BULLISH,
            gate=GateResult(
                name="MARKET_REGIME",
                status=GateStatus.PASS,
                reason=f"Bullish regime: Close ({latest_close}) > EMA-20 ({latest_ema:.2f}){ema50_str}, rising slope, ADX ({latest_adx:.1f}) >= {config.min_adx}",
                data=details,
            ),
            current_close=latest_close,
            ema_20=latest_ema,
            ema_50=latest_ema50,
            ema_slope_val=slope,
            adx_val=latest_adx,
            plus_di=latest_plus_di,
            minus_di=latest_minus_di,
        )

    if is_bearish:
        ema50_str = f" < EMA-50 ({latest_ema50:.2f})" if latest_ema50 is not None else ""
        return RegimeEvaluation(
            regime=MarketRegime.BEARISH,
            gate=GateResult(
                name="MARKET_REGIME",
                status=GateStatus.PASS,
                reason=f"Bearish regime: Close ({latest_close}) < EMA-20 ({latest_ema:.2f}){ema50_str}, falling slope, ADX ({latest_adx:.1f}) >= {config.min_adx}",
                data=details,
            ),
            current_close=latest_close,
            ema_20=latest_ema,
            ema_50=latest_ema50,
            ema_slope_val=slope,
            adx_val=latest_adx,
            plus_di=latest_plus_di,
            minus_di=latest_minus_di,
        )

    return RegimeEvaluation(
        regime=MarketRegime.NEUTRAL,
        gate=GateResult(
            name="MARKET_REGIME",
            status=GateStatus.FAIL,
            reason="Market regime is neutral (mixed signals or weak trend)",
            data=details,
        ),
        current_close=latest_close,
        ema_20=latest_ema,
        ema_50=latest_ema50,
        ema_slope_val=slope,
        adx_val=latest_adx,
        plus_di=latest_plus_di,
        minus_di=latest_minus_di,
    )
