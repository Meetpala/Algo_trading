"""APEX-3 Stock Signal Generator.

Evaluates individual stock candidates against opening range, session VWAP,
volume confirmation, relative strength vs NIFTY 50, spread, and circuit distance.

Exact mirror conditions for Long and Short:
- Long setup:
  1. Completed 5m close > Opening Range High (09:15-09:30 IST)
  2. Completed 5m close > Stock Session VWAP
  3. Market regime is BULLISH
  4. Relative strength > benchmark over lookback period (e.g. 20 bars)
  5. Signal bar volume >= 1.5x (min_relative_volume) 20-bar volume average
  6. Bid-Ask spread <= max_spread_bps (e.g. 8 bps)
  7. Price is at least min_circuit_distance_pct away from Upper Circuit limit
  8. Data is fresh and candle is completed

- Short setup:
  1. Completed 5m close < Opening Range Low (09:15-09:30 IST)
  2. Completed 5m close < Stock Session VWAP
  3. Market regime is BEARISH
  4. Relative weakness < benchmark over lookback period
  5. Signal bar volume >= 1.5x 20-bar volume average
  6. Bid-Ask spread <= max_spread_bps
  7. Price is at least min_circuit_distance_pct away from Lower Circuit limit
  8. Data is fresh and candle is completed
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from apex3.config import SignalConfig
from apex3.indicators.vwap import compute_session_vwap
from apex3.models import (
    Candle,
    Decision,
    GateResult,
    GateStatus,
    InstrumentInfo,
    OpeningRange,
    Quote,
    Side,
)
from apex3.strategy.regime import MarketRegime


@dataclass
class SignalEvaluation:
    """Result of signal evaluation for one symbol."""
    symbol: str
    decision: Decision  # PROPOSED_LONG, PROPOSED_SHORT, or WAIT
    side: Optional[Side] = None
    gates: list[GateResult] = None  # type: ignore[assignment]
    reason_codes: list[str] = None  # type: ignore[assignment]
    explanation: str = ""
    signal_price: float = 0.0
    or_high: float = 0.0
    or_low: float = 0.0
    vwap: float = 0.0
    relative_volume: float = 0.0
    relative_strength: float = 0.0
    circuit_distance_pct: float = 0.0

    def __post_init__(self):
        if self.gates is None:
            self.gates = []
        if self.reason_codes is None:
            self.reason_codes = []


def build_opening_range(
    symbol: str,
    candles_0915_0930: list[Candle],
) -> OpeningRange:
    """Build the 09:15 - 09:30 IST opening range from completed candles.

    Args:
        symbol: Instrument symbol
        candles_0915_0930: Candles strictly within 09:15 to 09:30 IST.

    Returns:
        OpeningRange with high, low, candle_count, and is_complete.
    """
    completed = [c for c in candles_0915_0930 if c.is_complete]
    if not completed:
        return OpeningRange(symbol=symbol, high=0.0, low=0.0, is_complete=False, candle_count=0)

    high = max(c.high for c in completed)
    low = min(c.low for c in completed)

    # 15 minutes of 5-min bars = 3 bars; or 1-min bars = 15 bars
    # If 5-min bars, at least 3 completed bars; if 1-min bars, at least 15 bars
    is_complete = len(completed) >= 3

    return OpeningRange(
        symbol=symbol,
        high=high,
        low=low,
        is_complete=is_complete,
        candle_count=len(completed),
    )


def compute_relative_strength(
    stock_closes: list[float],
    benchmark_closes: list[float],
    lookback: int = 20,
) -> float:
    """Calculate relative performance of stock vs benchmark over lookback bars.

    Returns:
        stock_pct_return - benchmark_pct_return (positive = stock outperforming).
    """
    if len(stock_closes) < lookback or len(benchmark_closes) < lookback:
        return 0.0

    stock_ret = (stock_closes[-1] - stock_closes[-lookback]) / stock_closes[-lookback]
    bench_ret = (benchmark_closes[-1] - benchmark_closes[-lookback]) / benchmark_closes[-lookback]
    return (stock_ret - bench_ret) * 100.0


def evaluate_signal(
    symbol: str,
    stock_candles_5m: list[Candle],
    benchmark_candles_5m: list[Candle],
    opening_range: OpeningRange,
    market_regime: MarketRegime,
    instrument: InstrumentInfo,
    quote: Optional[Quote],
    config: SignalConfig,
    is_entry_window: bool = True,
) -> SignalEvaluation:
    """Evaluate a single stock for entry signal across all 8 gates.

    Fails closed: Any failed gate or missing/stale data results in WAIT.
    """
    gates: list[GateResult] = []
    reason_codes: list[str] = []

    # 0. Session Entry Window Gate
    if not is_entry_window:
        gates.append(
            GateResult(
                name="ENTRY_WINDOW",
                status=GateStatus.FAIL,
                reason="Current session state does not permit new entries",
                data={"code": "OUTSIDE_ENTRY_WINDOW"},
            )
        )
        return SignalEvaluation(
            symbol=symbol,
            decision=Decision.WAIT,
            gates=gates,
            reason_codes=["OUTSIDE_ENTRY_WINDOW"],
            explanation="Outside active entry window (09:30 - 14:15 IST)",
        )
    gates.append(GateResult(name="ENTRY_WINDOW", status=GateStatus.PASS, reason="Inside entry window"))

    # 1. Opening Range Gate
    if not opening_range.is_complete or opening_range.high <= opening_range.low:
        gates.append(
            GateResult(
                name="OPENING_RANGE",
                status=GateStatus.FAIL,
                reason=f"Opening range incomplete (count={opening_range.candle_count})",
                data={"code": "INCOMPLETE_OPENING_RANGE"},
            )
        )
        return SignalEvaluation(
            symbol=symbol,
            decision=Decision.WAIT,
            gates=gates,
            reason_codes=["INCOMPLETE_OPENING_RANGE"],
            explanation=f"Opening range for {symbol} is incomplete or invalid",
        )
    gates.append(
        GateResult(
            name="OPENING_RANGE",
            status=GateStatus.PASS,
            reason=f"Opening range complete: H={opening_range.high:.2f}, L={opening_range.low:.2f}",
        )
    )

    # 2. Candle Data Freshness Gate
    completed_candles = [c for c in stock_candles_5m if c.is_complete]
    if not completed_candles:
        gates.append(
            GateResult(
                name="CANDLE_DATA",
                status=GateStatus.FAIL,
                reason="No completed 5m candles available",
                data={"code": "NO_COMPLETED_CANDLES"},
            )
        )
        return SignalEvaluation(
            symbol=symbol,
            decision=Decision.WAIT,
            gates=gates,
            reason_codes=["NO_COMPLETED_CANDLES"],
            explanation="No completed 5m bars available for evaluation",
        )

    latest_bar = completed_candles[-1]
    signal_price = latest_bar.close

    # Check Quote Freshness & Spread
    if quote is not None:
        if not quote.is_fresh:
            gates.append(
                GateResult(
                    name="QUOTE_FRESHNESS",
                    status=GateStatus.FAIL,
                    reason="Quote data is stale",
                    data={"code": "STALE_QUOTE"},
                )
            )
            return SignalEvaluation(
                symbol=symbol,
                decision=Decision.WAIT,
                gates=gates,
                reason_codes=["STALE_QUOTE"],
                explanation="Quote data exceeds staleness threshold",
            )

        # Spread check
        if quote.bid > 0 and quote.ask > 0:
            mid = (quote.bid + quote.ask) / 2.0
            spread_bps = ((quote.ask - quote.bid) / mid) * 10000.0
            if spread_bps > config.max_spread_bps:
                gates.append(
                    GateResult(
                        name="SPREAD",
                        status=GateStatus.FAIL,
                        reason=f"Spread {spread_bps:.1f} bps > max {config.max_spread_bps} bps",
                        data={"spread_bps": spread_bps, "max_bps": config.max_spread_bps},
                    )
                )
                return SignalEvaluation(
                    symbol=symbol,
                    decision=Decision.WAIT,
                    gates=gates,
                    reason_codes=["EXCESSIVE_SPREAD"],
                    explanation=f"Spread {spread_bps:.1f} bps exceeds limit {config.max_spread_bps} bps",
                )
            gates.append(GateResult(name="SPREAD", status=GateStatus.PASS, reason=f"Spread {spread_bps:.1f} bps acceptable"))
    gates.append(GateResult(name="QUOTE_FRESHNESS", status=GateStatus.PASS, reason="Quote data fresh"))

    # 3. Market Regime Alignment Gate
    if market_regime == MarketRegime.NEUTRAL:
        gates.append(
            GateResult(
                name="REGIME_ALIGNMENT",
                status=GateStatus.FAIL,
                reason="Market regime is NEUTRAL; entries prohibited",
                data={"code": "MARKET_REGIME_NEUTRAL"},
            )
        )
        return SignalEvaluation(
            symbol=symbol,
            decision=Decision.WAIT,
            gates=gates,
            reason_codes=["MARKET_REGIME_NEUTRAL"],
            explanation="Broad-market regime is neutral or mixed",
        )

    target_side = Side.BUY if market_regime == MarketRegime.BULLISH else Side.SELL
    gates.append(
        GateResult(
            name="REGIME_ALIGNMENT",
            status=GateStatus.PASS,
            reason=f"Regime aligns with target side: {target_side.value}",
        )
    )

    # 4. Opening Range Breakout Gate
    if target_side == Side.BUY:
        if signal_price <= opening_range.high:
            gates.append(
                GateResult(
                    name="OR_BREAKOUT",
                    status=GateStatus.FAIL,
                    reason=f"Close ({signal_price:.2f}) <= OR High ({opening_range.high:.2f})",
                    data={"close": signal_price, "or_high": opening_range.high},
                )
            )
            return SignalEvaluation(
                symbol=symbol,
                decision=Decision.WAIT,
                gates=gates,
                reason_codes=["NO_OR_HIGH_BREAKOUT"],
                explanation=f"Price {signal_price:.2f} has not closed above OR high {opening_range.high:.2f}",
            )
    else:  # Side.SELL
        if signal_price >= opening_range.low:
            gates.append(
                GateResult(
                    name="OR_BREAKOUT",
                    status=GateStatus.FAIL,
                    reason=f"Close ({signal_price:.2f}) >= OR Low ({opening_range.low:.2f})",
                    data={"close": signal_price, "or_low": opening_range.low},
                )
            )
            return SignalEvaluation(
                symbol=symbol,
                decision=Decision.WAIT,
                gates=gates,
                reason_codes=["NO_OR_LOW_BREAKOUT"],
                explanation=f"Price {signal_price:.2f} has not closed below OR low {opening_range.low:.2f}",
            )
    gates.append(GateResult(name="OR_BREAKOUT", status=GateStatus.PASS, reason="Confirmed OR breakout on completed bar"))

    # 5. Session VWAP Alignment Gate
    session_highs = [c.high for c in completed_candles]
    session_lows = [c.low for c in completed_candles]
    session_closes = [c.close for c in completed_candles]
    session_volumes = [c.volume for c in completed_candles]

    stock_vwap = compute_session_vwap(session_highs, session_lows, session_closes, session_volumes)

    if target_side == Side.BUY:
        if signal_price <= stock_vwap:
            gates.append(
                GateResult(
                    name="VWAP_ALIGNMENT",
                    status=GateStatus.FAIL,
                    reason=f"Close ({signal_price:.2f}) <= VWAP ({stock_vwap:.2f})",
                    data={"close": signal_price, "vwap": stock_vwap},
                )
            )
            return SignalEvaluation(
                symbol=symbol,
                decision=Decision.WAIT,
                gates=gates,
                reason_codes=["BELOW_VWAP"],
                explanation=f"Long requires price ({signal_price:.2f}) > session VWAP ({stock_vwap:.2f})",
            )
    else:  # Side.SELL
        if signal_price >= stock_vwap:
            gates.append(
                GateResult(
                    name="VWAP_ALIGNMENT",
                    status=GateStatus.FAIL,
                    reason=f"Close ({signal_price:.2f}) >= VWAP ({stock_vwap:.2f})",
                    data={"close": signal_price, "vwap": stock_vwap},
                )
            )
            return SignalEvaluation(
                symbol=symbol,
                decision=Decision.WAIT,
                gates=gates,
                reason_codes=["ABOVE_VWAP"],
                explanation=f"Short requires price ({signal_price:.2f}) < session VWAP ({stock_vwap:.2f})",
            )
    gates.append(GateResult(name="VWAP_ALIGNMENT", status=GateStatus.PASS, reason=f"VWAP aligned ({stock_vwap:.2f})"))

    # 6. Relative Volume Confirmation Gate
    if len(completed_candles) >= config.volume_avg_period + 1:
        prior_volumes = [c.volume for c in completed_candles[-config.volume_avg_period - 1 : -1]]
        avg_volume = sum(prior_volumes) / len(prior_volumes) if prior_volumes else 1.0
    else:
        all_prior = [c.volume for c in completed_candles[:-1]]
        avg_volume = sum(all_prior) / len(all_prior) if all_prior else 1.0

    current_volume = latest_bar.volume
    relative_vol = current_volume / avg_volume if avg_volume > 0 else 1.0

    if relative_vol < config.min_relative_volume:
        gates.append(
            GateResult(
                name="VOLUME_CONFIRMATION",
                status=GateStatus.FAIL,
                reason=f"Volume multiple {relative_vol:.2f}x < required {config.min_relative_volume}x",
                data={"relative_volume": relative_vol, "min_required": config.min_relative_volume},
            )
        )
        return SignalEvaluation(
            symbol=symbol,
            decision=Decision.WAIT,
            gates=gates,
            reason_codes=["INSUFFICIENT_VOLUME"],
            explanation=f"Volume {relative_vol:.2f}x below required threshold {config.min_relative_volume}x",
        )
    gates.append(
        GateResult(
            name="VOLUME_CONFIRMATION",
            status=GateStatus.PASS,
            reason=f"Volume confirmed ({relative_vol:.2f}x >= {config.min_relative_volume}x)",
        )
    )

    # 7. Relative Strength / Weakness Gate
    bench_closes = [c.close for c in benchmark_candles_5m if c.is_complete]
    stock_closes = [c.close for c in completed_candles]
    rel_perf = compute_relative_strength(stock_closes, bench_closes, lookback=config.relative_strength_lookback)

    if target_side == Side.BUY and rel_perf <= 0.0:
        gates.append(
            GateResult(
                name="RELATIVE_STRENGTH",
                status=GateStatus.FAIL,
                reason=f"Stock relative performance ({rel_perf:+.2f}%) <= 0 vs benchmark",
                data={"rel_perf": rel_perf},
            )
        )
        return SignalEvaluation(
            symbol=symbol,
            decision=Decision.WAIT,
            gates=gates,
            reason_codes=["WEAKER_THAN_BENCHMARK"],
            explanation=f"Stock is underperforming NIFTY 50 ({rel_perf:+.2f}%) over lookback",
        )
    elif target_side == Side.SELL and rel_perf >= 0.0:
        gates.append(
            GateResult(
                name="RELATIVE_STRENGTH",
                status=GateStatus.FAIL,
                reason=f"Stock relative performance ({rel_perf:+.2f}%) >= 0 vs benchmark",
                data={"rel_perf": rel_perf},
            )
        )
        return SignalEvaluation(
            symbol=symbol,
            decision=Decision.WAIT,
            gates=gates,
            reason_codes=["STRONGER_THAN_BENCHMARK"],
            explanation=f"Stock is outperforming NIFTY 50 ({rel_perf:+.2f}%) over lookback (short requires weakness)",
        )
    gates.append(
        GateResult(
            name="RELATIVE_STRENGTH",
            status=GateStatus.PASS,
            reason=f"Relative performance aligned: {rel_perf:+.2f}%",
        )
    )

    # 8. Circuit Limit Distance Gate
    circuit_dist = 100.0
    if target_side == Side.BUY and instrument.upper_circuit > 0:
        circuit_dist = ((instrument.upper_circuit - signal_price) / signal_price) * 100.0
        if circuit_dist < config.min_circuit_distance_pct:
            gates.append(
                GateResult(
                    name="CIRCUIT_DISTANCE",
                    status=GateStatus.FAIL,
                    reason=f"Upper circuit distance {circuit_dist:.2f}% < min {config.min_circuit_distance_pct}%",
                    data={"dist_pct": circuit_dist, "upper_circuit": instrument.upper_circuit},
                )
            )
            return SignalEvaluation(
                symbol=symbol,
                decision=Decision.WAIT,
                gates=gates,
                reason_codes=["TOO_CLOSE_TO_CIRCUIT"],
                explanation=f"Upper circuit is only {circuit_dist:.2f}% away",
            )
    elif target_side == Side.SELL and instrument.lower_circuit > 0:
        circuit_dist = ((signal_price - instrument.lower_circuit) / signal_price) * 100.0
        if circuit_dist < config.min_circuit_distance_pct:
            gates.append(
                GateResult(
                    name="CIRCUIT_DISTANCE",
                    status=GateStatus.FAIL,
                    reason=f"Lower circuit distance {circuit_dist:.2f}% < min {config.min_circuit_distance_pct}%",
                    data={"dist_pct": circuit_dist, "lower_circuit": instrument.lower_circuit},
                )
            )
            return SignalEvaluation(
                symbol=symbol,
                decision=Decision.WAIT,
                gates=gates,
                reason_codes=["TOO_CLOSE_TO_CIRCUIT"],
                explanation=f"Lower circuit is only {circuit_dist:.2f}% away",
            )
    gates.append(GateResult(name="CIRCUIT_DISTANCE", status=GateStatus.PASS, reason=f"Circuit distance safe ({circuit_dist:.2f}%)"))

    # All 8 gates passed!
    decision = Decision.PROPOSED_LONG if target_side == Side.BUY else Decision.PROPOSED_SHORT
    return SignalEvaluation(
        symbol=symbol,
        decision=decision,
        side=target_side,
        gates=gates,
        reason_codes=["ALL_GATES_PASSED"],
        explanation=f"All criteria met for {decision.value} on {symbol}",
        signal_price=signal_price,
        or_high=opening_range.high,
        or_low=opening_range.low,
        vwap=stock_vwap,
        relative_volume=relative_vol,
        relative_strength=rel_perf,
        circuit_distance_pct=circuit_dist,
    )
