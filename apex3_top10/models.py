"""APEX-3 core data models.

All models are immutable dataclasses with type hints. Used across
the strategy engine, broker adapters, and journal.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum, auto
from typing import Optional


class Side(Enum):
    BUY = "BUY"
    SELL = "SELL"


class Product(Enum):
    MIS = "MIS"  # Only product used by APEX-3


class OrderType(Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"
    SL = "SL"          # Stop-loss limit
    SL_M = "SL-M"      # Stop-loss market


class OrderVariety(Enum):
    REGULAR = "regular"


class OrderStatus(Enum):
    """Broker-reported order status. Order acceptance != execution."""
    PENDING = "PENDING"           # Not yet sent to broker
    OPEN = "OPEN"                 # Accepted by broker, on exchange
    TRIGGER_PENDING = "TRIGGER_PENDING"  # SL order waiting for trigger
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    COMPLETE = "COMPLETE"         # Fully filled
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    ERROR = "ERROR"               # API/network error
    UNKNOWN = "UNKNOWN"           # Unrecognized broker status


class Decision(Enum):
    WAIT = "WAIT"
    PROPOSED_LONG = "PROPOSED_LONG"
    PROPOSED_SHORT = "PROPOSED_SHORT"
    MANAGE = "MANAGE"
    EXIT = "EXIT"
    HALT = "HALT"


class GateStatus(Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class Candle:
    """One OHLCV candle bar."""
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: int
    is_complete: bool = True  # False for the current forming bar


@dataclass(frozen=True)
class Quote:
    """Live quote snapshot."""
    symbol: str
    exchange: str
    last_price: float
    bid: float
    ask: float
    volume: int
    timestamp: datetime
    is_fresh: bool = True  # False if older than stale threshold


@dataclass(frozen=True)
class OpeningRange:
    """First 15-minute (09:15-09:30 IST) range."""
    symbol: str
    high: float
    low: float
    is_complete: bool = False
    candle_count: int = 0


@dataclass
class OrderRequest:
    """Request to place an order. Not yet sent to broker."""
    symbol: str = ""
    exchange: str = "NSE"
    side: Side = Side.BUY
    quantity: int = 0
    product: Product = Product.MIS
    order_type: OrderType = OrderType.LIMIT
    price: float = 0.0
    trigger_price: float = 0.0
    tag: str = ""  # APEX-3 session tag, max 20 chars
    strategy_id: str = ""


def make_broker_tag(session_tag: str, symbol: str = "", suffix: str = "") -> str:
    """Generate a Zerodha Kite Connect compliant order tag (strictly <= 20 chars).

    Structure:
    - session_tag prefix (up to 5 chars, e.g. 'APEX3')
    - symbol (up to 6 chars, e.g. 'SUNPHA')
    - suffix (up to 6 chars, e.g. 'E0935', 'SL', 'TGT', 'EMG', 'FLT')
    Joined by '_' and truncated to at most 20 characters total.
    Ensures tag.startswith(session_tag_prefix) always matches.
    """
    prefix = (session_tag or "APEX3")[:5]
    sym_short = (symbol or "")[:6]
    sfx = (suffix or "")[:6]
    parts = [p for p in (prefix, sym_short, sfx) if p]
    tag = "_".join(parts)
    return tag[:20]


@dataclass
class OrderState:
    """Tracked state of an order through its lifecycle.
    
    WARNING: order acceptance by the broker does NOT prove exchange execution.
    Always reconcile via order history and trades.
    """
    order_id: str = ""
    request: OrderRequest = field(default_factory=OrderRequest)
    status: OrderStatus = OrderStatus.PENDING
    filled_quantity: int = 0
    pending_quantity: int = 0
    average_price: float = 0.0
    modification_count: int = 0  # Track against 25-mod Kite limit
    last_updated: Optional[datetime] = None
    status_message: str = ""
    is_strategy_owned: bool = True


@dataclass
class PositionState:
    """Current position state for a symbol.
    
    Tracks actual broker-confirmed quantities and prices,
    not just order requests.
    """
    symbol: str = ""
    side: Side = Side.BUY
    quantity: int = 0          # Actual open quantity
    average_entry: float = 0.0 # Actual average fill price
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    product: Product = Product.MIS
    # Protective stop tracking
    protective_order_id: str = ""
    protective_stop_price: float = 0.0
    initial_stop_price: float = 0.0
    is_protected: bool = False
    # Trail and target tracking
    best_favorable_price: float = 0.0  # High-water (long) or low-water (short)
    r_value: float = 0.0               # abs(entry - initial_stop)
    current_r_multiple: float = 0.0    # Current favorable excursion in R
    trail_activated: bool = False
    fee_lock_applied: bool = False
    target_price: float = 0.0          # Take-profit price (e.g. 2.0R target)
    target_order_id: str = ""
    # Strategy metadata
    strategy_tag: str = ""
    entry_time: Optional[datetime] = None


@dataclass(frozen=True)
class MarginInfo:
    """Broker-reported margin information.
    
    WARNING: Do not double-count available cash, collateral,
    or utilized margin. Use broker-reported values directly.
    """
    equity_available: float = 0.0     # Available margin for new orders
    equity_used: float = 0.0          # Currently utilized margin
    collateral: float = 0.0           # Collateral (not unrestricted cash)
    net_value: float = 0.0            # Net account value
    timestamp: Optional[datetime] = None


@dataclass(frozen=True)
class OrderMarginEstimate:
    """Estimated margin for a specific proposed order.
    
    From Kite's order margin calculator. Must be fetched
    for the exact symbol, product, side, type, price, and quantity.
    """
    required_margin: float = 0.0
    charges: float = 0.0              # Estimated brokerage + taxes
    total: float = 0.0
    leverage_available: float = 0.0   # Actual leverage for this order


@dataclass(frozen=True)
class GateResult:
    """Result of a single gate check in the decision pipeline."""
    name: str
    status: GateStatus
    reason: str = ""
    data: dict = field(default_factory=dict)


@dataclass
class StrategyDecision:
    """Output of one evaluation cycle of the strategy engine."""
    decision: Decision = Decision.WAIT
    symbol: str = ""
    side: Optional[Side] = None
    timestamp_ist: Optional[datetime] = None
    reason_codes: list[str] = field(default_factory=list)
    explanation: str = ""
    gates: list[GateResult] = field(default_factory=list)
    # Entry proposal fields (when applicable)
    quantity: int = 0
    entry_limit: float = 0.0
    initial_stop: float = 0.0
    risk_rupees: float = 0.0
    estimated_margin: float = 0.0
    max_notional: float = 0.0
    # Management fields (when applicable)
    open_r: float = 0.0
    best_favorable_price: float = 0.0
    current_stop: float = 0.0
    proposed_stop: float = 0.0
    trail_reason: str = ""
    # Action
    broker_action: str = "NONE"


@dataclass(frozen=True)
class InstrumentInfo:
    """Exchange instrument metadata."""
    symbol: str
    exchange: str = "NSE"
    tick_size: float = 0.05   # NSE equity default
    lot_size: int = 1
    exchange_token: str = ""
    instrument_token: str = ""
    upper_circuit: float = 0.0
    lower_circuit: float = 0.0
    mis_available: bool = True
