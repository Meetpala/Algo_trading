import sys
sys.path.insert(0, ".")
import zoneinfo
from datetime import datetime, date
from unittest.mock import MagicMock
from apex3.config import AppConfig
from apex3.models import *
from apex3.strategy.engine import StrategyEngine
from apex3.strategy.regime import MarketRegime
from apex3.strategy.signal import SignalEvaluation

IST = zoneinfo.ZoneInfo('Asia/Kolkata')
mock_broker = MagicMock()
mock_journal = MagicMock()
mock_broker.is_session_valid.return_value = True
mock_broker.get_margins.return_value = MarginInfo(equity_available=15000.0, net_value=15000.0)
mock_broker.get_orders.return_value = []
mock_broker.get_trades.return_value = []
mock_broker.get_positions.return_value = []
mock_broker.get_instrument_info.return_value = InstrumentInfo(symbol='HINDALCO', exchange='NSE', lot_size=1, tick_size=0.05)
now = datetime(2026, 3, 20, 9, 35, tzinfo=IST)
mock_broker.get_quote.return_value = Quote(symbol='HINDALCO', exchange='NSE', last_price=650.0, bid=649.95, ask=650.05, volume=10000, timestamp=now)

cfg = AppConfig()
object.__setattr__(cfg.watchlist, 'symbols', ['HINDALCO'])
object.__setattr__(cfg.watchlist, 'scanner_enabled', False)
engine = StrategyEngine(cfg, mock_broker, mock_journal)

candles = [
    Candle(timestamp=datetime(2026, 3, 20, 9, 15, tzinfo=IST), open=640.0, high=645.0, low=638.0, close=644.0, volume=10000, is_complete=True),
    Candle(timestamp=datetime(2026, 3, 20, 9, 20, tzinfo=IST), open=644.0, high=646.0, low=642.0, close=645.0, volume=12000, is_complete=True),
    Candle(timestamp=datetime(2026, 3, 20, 9, 25, tzinfo=IST), open=645.0, high=647.0, low=644.0, close=646.0, volume=11000, is_complete=True),
    Candle(timestamp=datetime(2026, 3, 20, 9, 30, tzinfo=IST), open=646.0, high=652.0, low=645.0, close=650.0, volume=35000, is_complete=True),
]
mock_broker.get_historical.return_value = candles

fake_sig = SignalEvaluation(
    symbol='HINDALCO',
    decision=Decision.PROPOSED_LONG,
    signal_price=650.0,
    side=Side.BUY,
    gates=[GateResult('TEST', GateStatus.PASS, 'OK')],
    relative_strength=0.05,
    relative_volume=3.0,
)

import apex3.strategy.engine as eng_module
eng_module.evaluate_signal = MagicMock(return_value=fake_sig)
eng_module.evaluate_market_regime = MagicMock(return_value=MagicMock(regime=MarketRegime.BULLISH, is_valid=True))

from apex3.strategy.accounting import build_session_pnl
try:
    pnl = build_session_pnl(trades=[], orders=[], open_positions=[], session_tag="APEX3", session_date=now.date(), costs=cfg.costs)
    print("PNL built:", pnl)
except Exception as e:
    import traceback
    traceback.print_exc()

decisions = engine.evaluate_step(now)
print('Decisions count:', len(decisions))
for d in decisions:
    print('Decision:', d.decision, d.reason_codes, d.explanation)
print('Place order calls:', mock_broker.place_order.call_count)
