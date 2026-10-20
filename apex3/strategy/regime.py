"""APEX-3 Market Regime Filter.

Evaluates broad-market regime using NIFTY 50 OHLC trend data (EMA-20 slope and ADX).
IMPORTANT: Do NOT calculate a fake NIFTY cash-index VWAP. Individual stocks use
their own session VWAP; the benchmark index has no traded volume on cash index.

Regime rules:
- Long market regime: NIFTY 50 close > 5m EMA-20, EMA-20 is rising, and 5m ADX >= 22.
- Short market regime: NIFTY 50 close < 5m EMA-20, EMA-20 is falling, and 5m ADX >= 22.
- Stand aside (NEUTRAL): If mixed, low-trend, stale, or missing data.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Optional

from apex3.config import RegimeConfig
from apex3.indicators.adx import compute_adx
from apex3.indicators.ema import compute_ema, ema_slope
from apex3.models import Candle, GateResult, GateStatus, Quote


class MarketRegime(Enum):
    """Broad-market trend regime."""
    BULLISH = auto()  # Qualified for long entries
    BEARISH = auto()  # Qualified for short entries
    NEUTRAL = auto()  # Stand aside / no entry


@dataclass(frozen=True)
class RegimeEvaluation:
    """Detailed evaluation result for market regime."""
    regime: MarketRegime
    gate: GateResult
    current_close: Optional[float] = None
    ema_20: Optional[float] = None
    ema_slope_val: Optional[float] = None
    adx_val: Optional[float] = None
    plus_di: Optional[float] = None
    minus_di: Optional[float] = None


def evaluate_market_regime(
    candles_5m: list[Candle],
    config: RegimeConfig,
    benchmark_quote: Optional[Quote] = None,
    stale_threshold_sec: float = 2.0,
) -> RegimeEvaluation:
    """Evaluate NIFTY 50 market regime using completed 5-minute bars.

    Args:
        candles_5m: Chronological list of completed 5m benchmark candles.
        config: RegimeConfig containing thresholds (ema_period, min_adx, etc.).
        benchmark_quote: Optional latest quote snapshot to check freshness.
        stale_threshold_sec: Max allowed age for quote in seconds.

    Returns:
        RegimeEvaluation with regime state, gate status, and indicator values.
    """
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

    # Need enough candles for EMA and ADX calculations
    # ADX requires 2 * period bars minimum for stabilized calculation
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

    # 1. EMA-20
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

    # 2. EMA slope
    slope = ema_slope(ema_series, lookback=config.ema_slope_lookback)

    # 3. ADX
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
    latest_plus_di = plus_di_series[-1] if plus_di_series else None
    latest_minus_di = minus_di_series[-1] if minus_di_series else None

    # Trend Strength check
    is_trending = latest_adx >= config.min_adx

    # Bullish condition: Close > EMA-20, EMA rising (slope > 0), ADX >= 22
    is_bullish = (latest_close > latest_ema) and (slope > 0) and is_trending

    # Bearish condition: Close < EMA-20, EMA falling (slope < 0), ADX >= 22
    is_bearish = (latest_close < latest_ema) and (slope < 0) and is_trending

    details = {
        "close": latest_close,
        "ema": round(latest_ema, 2),
        "slope": slope,
        "adx": round(latest_adx, 2),
        "min_adx": config.min_adx,
    }

    if is_bullish:
        return RegimeEvaluation(
            regime=MarketRegime.BULLISH,
            gate=GateResult(
                name="MARKET_REGIME",
                status=GateStatus.PASS,
                reason=f"Bullish regime: Close ({latest_close}) > EMA-20 ({latest_ema:.2f}), rising slope, ADX ({latest_adx:.1f}) >= {config.min_adx}",
                data=details,
            ),
            current_close=latest_close,
            ema_20=latest_ema,
            ema_slope_val=slope,
            adx_val=latest_adx,
            plus_di=latest_plus_di,
            minus_di=latest_minus_di,
        )
    elif is_bearish:
        return RegimeEvaluation(
            regime=MarketRegime.BEARISH,
            gate=GateResult(
                name="MARKET_REGIME",
                status=GateStatus.PASS,
                reason=f"Bearish regime: Close ({latest_close}) < EMA-20 ({latest_ema:.2f}), falling slope, ADX ({latest_adx:.1f}) >= {config.min_adx}",
                data=details,
            ),
            current_close=latest_close,
            ema_20=latest_ema,
            ema_slope_val=slope,
            adx_val=latest_adx,
            plus_di=latest_plus_di,
            minus_di=latest_minus_di,
        )
    else:
        reasons = []
        if not is_trending:
            reasons.append(f"ADX ({latest_adx:.1f}) < {config.min_adx} (no trend)")
        if slope == 0:
            reasons.append("EMA-20 is flat")
        if (latest_close > latest_ema and slope < 0) or (latest_close < latest_ema and slope > 0):
            reasons.append("Price position contradicts EMA slope")

        reason_str = "Neutral/Mixed regime: " + (", ".join(reasons) if reasons else "No clear direction")
        return RegimeEvaluation(
            regime=MarketRegime.NEUTRAL,
            gate=GateResult(
                name="MARKET_REGIME",
                status=GateStatus.FAIL,
                reason=reason_str,
                data=details,
            ),
            current_close=latest_close,
            ema_20=latest_ema,
            ema_slope_val=slope,
            adx_val=latest_adx,
            plus_di=latest_plus_di,
            minus_di=latest_minus_di,
        )
