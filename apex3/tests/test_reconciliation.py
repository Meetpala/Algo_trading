"""Tests for Order Reconciliation and Controlled Flatten Sequence."""
import pytest

from apex3.broker.paper import PaperBroker
from apex3.journal.sqlite_journal import Journal
from apex3.models import OrderRequest, OrderType, PositionState, Product, Side
from apex3.reconciliation.reconciler import Reconciler
from apex3.reconciliation.state_machine import PositionPhase, StateMachine


@pytest.fixture
def test_setup(tmp_path):
    db_file = str(tmp_path / "test_reconcile.db")
    journal = Journal(db_path=db_file)
    broker = PaperBroker(initial_margin=100000.0)
    sm = StateMachine(session_tag_prefix="APEX3")
    reconciler = Reconciler(broker=broker, state_machine=sm, journal=journal, session_tag="APEX3")
    return broker, sm, journal, reconciler


def test_detect_unknown_manual_positions(test_setup):
    """When an unmanaged/foreign MIS position exists on broker, audit must fail."""
    broker, sm, journal, reconciler = test_setup

    # Simulate a manual position on the broker
    broker._positions["INFY"] = PositionState(
        symbol="INFY",
        side=Side.BUY,
        quantity=50,
        average_entry=1500.0,
    )

    status = reconciler.audit_startup_state(watched_symbols=["RELIANCE", "TCS"])
    assert not status.is_clean
    assert status.has_unknown_positions
    assert "INFY" in status.unknown_symbols


def test_verify_unprotected_positions(test_setup):
    """When a position is open without a protective stop order, flag emergency."""
    broker, sm, journal, reconciler = test_setup

    pos = sm.get_or_create_position("RELIANCE")
    pos.quantity = 20
    pos.phase = PositionPhase.OPEN_UNPROTECTED
    pos.protective_order_id = ""  # No stop

    unprotected = reconciler.verify_protective_stops()
    assert "RELIANCE" in unprotected


def test_execute_flatten_sequence(test_setup):
    """Verify controlled square-off cancels pending entries and exits open positions."""
    broker, sm, journal, reconciler = test_setup

    # Setup an active position
    pos = sm.get_or_create_position("RELIANCE")
    pos.side = Side.BUY
    pos.quantity = 10
    pos.average_entry = 2500.0
    pos.phase = PositionPhase.OPEN_PROTECTED
    pos.protective_order_id = "SL_101"

    broker._positions["RELIANCE"] = PositionState(
        symbol="RELIANCE",
        side=Side.BUY,
        quantity=10,
        average_entry=2500.0,
    )

    success, msg = reconciler.execute_flatten_sequence()
    assert success
    assert pos.phase == PositionPhase.EXITING
    assert pos.exit_order_id != ""
