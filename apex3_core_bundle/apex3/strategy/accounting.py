"""Session P&L accounting from broker-confirmed executions."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from apex3.config import CostsConfig
from apex3.models import OrderState, PositionState, Side

IST = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True)
class SessionPnL:
    """Gross realized/unrealized P&L and estimated charges for one session."""

    realized_pnl: float
    unrealized_pnl: float
    estimated_charges: float
    trade_count: int

    @property
    def net_pnl(self) -> float:
        return self.realized_pnl + self.unrealized_pnl - self.estimated_charges


def _as_ist_datetime(value: Any, session_date: date, fallback_index: int) -> datetime:
    """Parse a broker timestamp; session-scoped fills without one use a stable IST fallback."""
    if isinstance(value, datetime):
        parsed = value
    elif value:
        raw = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(f"Unusable broker fill timestamp: {value!r}") from exc
    else:
        parsed = datetime.combine(session_date, time.min, tzinfo=IST).replace(microsecond=fallback_index % 1_000_000)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=IST)
    return parsed.astimezone(IST)


def _side(value: Any) -> Side:
    if isinstance(value, Side):
        return value
    try:
        return Side(str(value).upper())
    except ValueError as exc:
        raise ValueError(f"Unusable broker fill side: {value!r}") from exc


def _is_strategy_order(order: OrderState, session_tag: str) -> bool:
    request = order.request
    tag = (request.tag or "").strip()
    strategy_id = (request.strategy_id or "").strip()
    return strategy_id == session_tag or tag == session_tag or tag.startswith(f"{session_tag}_")


def _calculate_realized_pnl(fills: list[dict[str, Any]]) -> float:
    """Calculate weighted-average realized P&L, including partial closes and reversals."""
    positions: dict[str, tuple[int, float]] = {}
    realized = 0.0

    for fill in fills:
        symbol = str(fill["symbol"])
        side = _side(fill["side"])
        quantity = int(fill["quantity"])
        price = float(fill["price"])
        if quantity <= 0 or price <= 0:
            raise ValueError(f"Invalid quantity or price in fill for {symbol}")

        signed_fill = quantity if side == Side.BUY else -quantity
        current_quantity, average_price = positions.get(symbol, (0, 0.0))

        if current_quantity == 0 or (current_quantity > 0) == (signed_fill > 0):
            new_quantity = current_quantity + signed_fill
            total_quantity = abs(current_quantity) + quantity
            new_average = (
                (abs(current_quantity) * average_price + quantity * price) / total_quantity
                if total_quantity
                else 0.0
            )
            positions[symbol] = (new_quantity, new_average)
            continue

        closed_quantity = min(abs(current_quantity), quantity)
        realized += (
            (price - average_price) * closed_quantity
            if current_quantity > 0
            else (average_price - price) * closed_quantity
        )
        new_quantity = current_quantity + signed_fill
        if new_quantity == 0:
            positions[symbol] = (0, 0.0)
        elif (new_quantity > 0) != (current_quantity > 0):
            positions[symbol] = (new_quantity, price)
        else:
            positions[symbol] = (new_quantity, average_price)

    return round(realized, 2)


def _calculate_estimated_charges(
    fills: list[dict[str, Any]], costs: CostsConfig
) -> float:
    """Estimate intraday charges by executed order, including partial executions."""
    order_turnover: dict[str, float] = defaultdict(float)
    buy_turnover = 0.0
    sell_turnover = 0.0

    for fill in fills:
        order_id = str(fill["order_id"])
        side = _side(fill["side"])
        turnover = float(fill["price"]) * int(fill["quantity"])
        order_turnover[order_id] += turnover
        if side == Side.BUY:
            buy_turnover += turnover
        else:
            sell_turnover += turnover

    brokerage = sum(
        min(costs.brokerage_per_order, turnover * 0.0003)
        for order_id, turnover in order_turnover.items()
    )
    total_turnover = buy_turnover + sell_turnover
    stt = sell_turnover * costs.stt_pct / 100.0
    exchange_txn = total_turnover * costs.exchange_txn_pct / 100.0
    sebi = total_turnover * costs.sebi_turnover_pct / 100.0
    gst = (brokerage + exchange_txn + sebi) * costs.gst_pct / 100.0
    stamp_duty = buy_turnover * costs.stamp_duty_pct / 100.0
    return round(brokerage + stt + exchange_txn + sebi + gst + stamp_duty, 2)


def _estimate_closing_charge(position: PositionState, costs: CostsConfig) -> float:
    """Include the expected fee for closing an open MIS position."""
    side = Side.SELL if position.side == Side.BUY else Side.BUY
    turnover = position.current_price * position.quantity
    brokerage = min(costs.brokerage_per_order, turnover * 0.0003)
    stt = turnover * costs.stt_pct / 100.0 if side == Side.SELL else 0.0
    exchange_txn = turnover * costs.exchange_txn_pct / 100.0
    sebi = turnover * costs.sebi_turnover_pct / 100.0
    gst = (brokerage + exchange_txn + sebi) * costs.gst_pct / 100.0
    stamp_duty = turnover * costs.stamp_duty_pct / 100.0 if side == Side.BUY else 0.0
    return brokerage + stt + exchange_txn + sebi + gst + stamp_duty


def build_session_pnl(
    trades: list[dict[str, Any]],
    orders: list[OrderState],
    open_positions: list[PositionState],
    session_tag: str,
    session_date: date,
    costs: CostsConfig,
) -> SessionPnL:
    """Build a strategy-only session P&L snapshot from current broker data.

    `trades` must be the broker's executions for the current session. Each
    execution is matched to the order tag before inclusion so manual activity
    cannot be counted as strategy P&L.
    """
    strategy_order_ids = {
        str(order.order_id)
        for order in orders
        if order.order_id and _is_strategy_order(order, session_tag)
    }

    fills: list[dict[str, Any]] = []
    all_order_ids = {str(order.order_id) for order in orders if order.order_id}
    orders_by_id = {str(order.order_id): order for order in orders if order.order_id}
    seen_trade_ids: set[str] = set()
    for index, trade in enumerate(trades):
        trade_id = str(trade.get("trade_id", ""))
        if trade_id and trade_id in seen_trade_ids:
            continue
        if trade_id:
            seen_trade_ids.add(trade_id)

        order_id = str(trade.get("order_id", ""))
        if not order_id or order_id not in all_order_ids:
            raise ValueError(f"Broker execution {trade_id or '(unknown id)'} has no matching order")
        if order_id not in strategy_order_ids:
            continue

        timestamp = _as_ist_datetime(
            trade.get("timestamp") or trade.get("exchange_timestamp") or trade.get("fill_timestamp"),
            session_date,
            index,
        )
        if timestamp.date() != session_date:
            continue

        order = orders_by_id[order_id]
        request_side = order.request.side
        fill_side = _side(trade.get("side", request_side.value))
        fills.append({
            "timestamp": timestamp,
            "order_id": order_id,
            "trade_id": trade_id,
            "symbol": str(trade.get("symbol") or order.request.symbol),
            "side": fill_side,
            "quantity": int(trade.get("quantity", 0)),
            "price": float(trade.get("price", 0.0)),
        })

    fills.sort(key=lambda item: (item["timestamp"], item["order_id"], item["trade_id"]))
    unrealized = 0.0
    expected_exit_charges = 0.0
    for position in open_positions:
        if position.quantity <= 0:
            continue
        if position.current_price <= 0 or position.average_entry <= 0:
            raise ValueError(f"Missing current mark for open position {position.symbol}")
        sign = 1.0 if position.side == Side.BUY else -1.0
        unrealized += (position.current_price - position.average_entry) * position.quantity * sign
        expected_exit_charges += _estimate_closing_charge(position, costs)

    return SessionPnL(
        realized_pnl=_calculate_realized_pnl(fills),
        unrealized_pnl=round(unrealized, 2),
        estimated_charges=round(
            _calculate_estimated_charges(fills, costs) + expected_exit_charges, 2
        ),
        trade_count=len(fills),
    )
