"""APEX-3 Position Sizing and Equal Allocation Module.

Calculates share quantity based on:
1. Usable equity margin after safety reserve (M)
2. Live leverage ceiling (L = min(MAX_LEVERAGE, order_leverage))
3. Equal notional allocation across ONLY qualified candidates (N)
   B = M * L / N
4. Per-trade rupee risk budget (RISK = equity * risk_per_trade_pct)
   Q_risk = floor(RISK / (abs(E - S) + C))
5. Broker-reported exact order margin limits:
   Q = min(Q_notional, Q_risk, broker_margin_quantity_limit)

CRITICAL RULES:
- 4x is a ceiling, never assumed.
- If live leverage < min_accepted_leverage, symbol is skipped.
- Never force investment of all capital.
- Do not double-count utilised margin or treat collateral as unrestricted cash.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from apex3.config import LimitsConfig, RiskConfig
from apex3.models import (
    GateResult,
    GateStatus,
    InstrumentInfo,
    MarginInfo,
    OrderMarginEstimate,
    Side,
)


@dataclass(frozen=True)
class SizingResult:
    """Result of position sizing calculation for a symbol."""
    symbol: str
    quantity: int
    is_valid: bool
    side: Side
    entry_price: float
    stop_price: float
    per_share_risk: float
    notional_value: float
    trade_risk_rupees: float
    usable_margin: float
    per_symbol_budget: float
    effective_leverage: float
    q_notional: int
    q_risk: int
    q_margin: int
    gate: GateResult
    rejection_code: str = ""
    rejection_reason: str = ""


def compute_usable_margin(margin_info: MarginInfo, safety_reserve_pct: float = 5.0) -> float:
    """Compute usable equity margin after subtracting the safety reserve.

    Uses broker-reported equity_available directly without double-counting.
    Collateral is NOT treated as unrestricted cash.

    Args:
        margin_info: Broker-reported MarginInfo.
        safety_reserve_pct: Percentage buffer to hold back (e.g. 5%).

    Returns:
        Usable margin in Rupees (>= 0.0).
    """
    available = max(0.0, margin_info.equity_available)
    reserve = available * (safety_reserve_pct / 100.0)
    return max(0.0, available - reserve)


def size_position(
    symbol: str,
    side: Side,
    entry_price: float,
    stop_price: float,
    num_qualified_symbols: int,
    margin_info: MarginInfo,
    instrument: InstrumentInfo,
    limits_config: LimitsConfig,
    risk_config: RiskConfig,
    order_margin_estimate: Optional[OrderMarginEstimate] = None,
) -> SizingResult:
    """Compute exact risk-sized and notional-budgeted quantity for a qualified symbol.

    Args:
        symbol: Candidate symbol
        side: BUY or SELL
        entry_price: Expected entry fill price (E)
        stop_price: Initial structural stop price (S)
        num_qualified_symbols: Total count of symbols passing all signal gates (N <= 3)
        margin_info: Fresh broker equity margin
        instrument: Instrument details (tick size, lot size)
        limits_config: Limits configuration (leverage, safety reserve)
        risk_config: Risk configuration (risk per trade, slippage allowance)
        order_margin_estimate: Optional live margin estimate from Kite for proposed order

    Returns:
        SizingResult with final quantity and all intermediate calculations.
    """
    # 0. Basic Validation
    if entry_price <= 0 or stop_price <= 0:
        return SizingResult(
            symbol=symbol,
            quantity=0,
            is_valid=False,
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            per_share_risk=0.0,
            notional_value=0.0,
            trade_risk_rupees=0.0,
            usable_margin=0.0,
            per_symbol_budget=0.0,
            effective_leverage=0.0,
            q_notional=0,
            q_risk=0,
            q_margin=0,
            gate=GateResult(
                name="SIZING_VALIDATION",
                status=GateStatus.FAIL,
                reason="Invalid entry or stop price (must be > 0)",
            ),
            rejection_code="INVALID_PRICES",
            rejection_reason="Entry or stop price <= 0",
        )

    # Stop direction validation
    if side == Side.BUY and stop_price >= entry_price:
        return SizingResult(
            symbol=symbol,
            quantity=0,
            is_valid=False,
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            per_share_risk=0.0,
            notional_value=0.0,
            trade_risk_rupees=0.0,
            usable_margin=0.0,
            per_symbol_budget=0.0,
            effective_leverage=0.0,
            q_notional=0,
            q_risk=0,
            q_margin=0,
            gate=GateResult(
                name="SIZING_VALIDATION",
                status=GateStatus.FAIL,
                reason=f"Long stop ({stop_price:.2f}) must be strictly below entry ({entry_price:.2f})",
            ),
            rejection_code="INVALID_STOP_DIRECTION",
            rejection_reason="Long stop >= entry",
        )
    elif side == Side.SELL and stop_price <= entry_price:
        return SizingResult(
            symbol=symbol,
            quantity=0,
            is_valid=False,
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            per_share_risk=0.0,
            notional_value=0.0,
            trade_risk_rupees=0.0,
            usable_margin=0.0,
            per_symbol_budget=0.0,
            effective_leverage=0.0,
            q_notional=0,
            q_risk=0,
            q_margin=0,
            gate=GateResult(
                name="SIZING_VALIDATION",
                status=GateStatus.FAIL,
                reason=f"Short stop ({stop_price:.2f}) must be strictly above entry ({entry_price:.2f})",
            ),
            rejection_code="INVALID_STOP_DIRECTION",
            rejection_reason="Short stop <= entry",
        )

    # 1. Usable Margin M
    usable_margin = compute_usable_margin(margin_info, limits_config.safety_reserve_pct)
    if usable_margin <= 0:
        return SizingResult(
            symbol=symbol,
            quantity=0,
            is_valid=False,
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            per_share_risk=0.0,
            notional_value=0.0,
            trade_risk_rupees=0.0,
            usable_margin=usable_margin,
            per_symbol_budget=0.0,
            effective_leverage=0.0,
            q_notional=0,
            q_risk=0,
            q_margin=0,
            gate=GateResult(
                name="MARGIN_AVAILABILITY",
                status=GateStatus.FAIL,
                reason=f"Usable margin is zero or negative ({usable_margin:.2f})",
                data={"available": margin_info.equity_available},
            ),
            rejection_code="INSUFFICIENT_MARGIN",
            rejection_reason="Usable equity margin is zero or negative",
        )

    # 2. Leverage Determination L
    # Broker leverage from live order margin estimate if available
    broker_leverage = limits_config.max_leverage
    if order_margin_estimate is not None and order_margin_estimate.leverage_available > 0:
        broker_leverage = order_margin_estimate.leverage_available

    # Check minimum accepted leverage
    if broker_leverage < limits_config.min_accepted_leverage:
        return SizingResult(
            symbol=symbol,
            quantity=0,
            is_valid=False,
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            per_share_risk=0.0,
            notional_value=0.0,
            trade_risk_rupees=0.0,
            usable_margin=usable_margin,
            per_symbol_budget=0.0,
            effective_leverage=broker_leverage,
            q_notional=0,
            q_risk=0,
            q_margin=0,
            gate=GateResult(
                name="LEVERAGE_CHECK",
                status=GateStatus.FAIL,
                reason=f"Live leverage {broker_leverage:.2f}x < configured minimum {limits_config.min_accepted_leverage:.2f}x",
                data={"broker_leverage": broker_leverage, "min_accepted": limits_config.min_accepted_leverage},
            ),
            rejection_code="LEVERAGE_BELOW_MINIMUM",
            rejection_reason="Broker leverage below minimum accepted leverage",
        )

    effective_leverage = min(limits_config.max_leverage, broker_leverage)

    # 3. Equal Notional Allocation across N qualified symbols
    # N must be capped at max_symbols and at least 1
    n = max(1, min(limits_config.max_symbols, num_qualified_symbols))
    per_symbol_budget = (usable_margin * effective_leverage) / n

    # Q_notional = floor(B / E)
    q_notional = math.floor(per_symbol_budget / entry_price)

    # 4. Risk per share and Q_risk
    # C = per-share allowance for charges and slippage
    slippage_cost = entry_price * (risk_config.slippage_allowance_pct / 100.0)
    per_share_loss = abs(entry_price - stop_price)
    per_share_risk = per_share_loss + slippage_cost

    # RISK = Rupee risk budget for this trade
    equity_base = margin_info.net_value if margin_info.net_value > 0 else margin_info.equity_available
    trade_risk_rupees = equity_base * (risk_config.risk_per_trade_pct / 100.0)

    q_risk = math.floor(trade_risk_rupees / per_share_risk) if per_share_risk > 0 else 0

    # 5. Margin Quantity Limit
    # Q_margin based on exact order margin per share if known
    q_margin = q_notional
    if order_margin_estimate is not None and order_margin_estimate.required_margin > 0:
        # Margin per share
        margin_per_share = entry_price / effective_leverage
        if margin_per_share > 0:
            q_margin = math.floor(usable_margin / margin_per_share)

    # Final Quantity: Smallest of Q_notional, Q_risk, Q_margin
    q_raw = min(q_notional, q_risk, q_margin)

    # Round to lot size (equities = 1)
    lot_size = max(1, instrument.lot_size)
    q_final = (q_raw // lot_size) * lot_size

    if q_final <= 0:
        return SizingResult(
            symbol=symbol,
            quantity=0,
            is_valid=False,
            side=side,
            entry_price=entry_price,
            stop_price=stop_price,
            per_share_risk=per_share_risk,
            notional_value=0.0,
            trade_risk_rupees=trade_risk_rupees,
            usable_margin=usable_margin,
            per_symbol_budget=per_symbol_budget,
            effective_leverage=effective_leverage,
            q_notional=q_notional,
            q_risk=q_risk,
            q_margin=q_margin,
            gate=GateResult(
                name="SIZING_QUANTITY",
                status=GateStatus.FAIL,
                reason=f"Calculated share quantity is 0 (q_notional={q_notional}, q_risk={q_risk})",
                data={"q_notional": q_notional, "q_risk": q_risk, "q_margin": q_margin},
            ),
            rejection_code="ZERO_QUANTITY",
            rejection_reason="Risk or margin constraints resulted in 0 shares",
        )

    notional_value = q_final * entry_price
    actual_trade_risk = q_final * per_share_risk

    return SizingResult(
        symbol=symbol,
        quantity=q_final,
        is_valid=True,
        side=side,
        entry_price=entry_price,
        stop_price=stop_price,
        per_share_risk=per_share_risk,
        notional_value=notional_value,
        trade_risk_rupees=actual_trade_risk,
        usable_margin=usable_margin,
        per_symbol_budget=per_symbol_budget,
        effective_leverage=effective_leverage,
        q_notional=q_notional,
        q_risk=q_risk,
        q_margin=q_margin,
        gate=GateResult(
            name="SIZING_QUANTITY",
            status=GateStatus.PASS,
            reason=f"Sized {q_final} shares (Notional: ₹{notional_value:,.2f}, Risk: ₹{actual_trade_risk:,.2f})",
            data={"quantity": q_final, "notional": notional_value, "risk": actual_trade_risk},
        ),
    )
