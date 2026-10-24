# APEX-3 Production Trading System — Model Context Bundle

This bundle contains the complete, production-ready codebase, mathematical specifications, quantitative invariants, and audit benchmarks for the **APEX-3 Algo Trading Engine**.

## 1. System Overview & Core Invariants
* **Operating Mode**: Intraday Trend-Following on NSE equities via Zerodha MIS.
* **Equity Base**: Exactly ₹15,000.00 INR with a 5.0% idle safety cash reserve (₹14,250 usable equity).
* **Leverage & Slots**: 4.0x leverage (₹57,000 buying power), divided into exactly 2 concurrent slots (₹28,500/slot).
* **Risk Budgeting**: 0.80% risk cap per trade (₹120.00 max loss), 1.60% hard daily stop (-₹240.00 = 2 losses).
* **Friction Invariant**: Total statutory charges across all trades must remain strictly `< 26.0%` of positive gross profits. Verified at **21.88% (6M)** and **21.98% (3M)**.
* **Structural Stop & Target**: Structural 1.5x ATR swing low/high anchor (`0.55% <= R% <= 0.90%`), fixed 2.0R limit target, and a one-time profit-preserving step (+1.60R -> +0.60R, and 14:15 IST +1.15R -> +0.35R).

## 2. Directory & File Navigation
* **`apex3/config.py` & `apex3/config.yaml`**: The single source of truth for all parameters, radar tiers, schedules, and thresholds.
* **`apex3/strategy/`**:
  * `engine.py`: The 12-step deterministic pipeline (`evaluate_step()`).
  * `signal.py`: 8-gate candidate signal evaluation with institutional volume (>= 2.0x core, >= 2.5x probation) and CLV (>= 0.70 core, >= 0.75 probation).
  * `regime.py`: NIFTY 50 dual-EMA + ADX + Typical-Price TWAP benchmark filter.
  * `sizer.py`: Exact position sizing with zero slippage allowance (`0.0%`) to match backtest share allocations.
  * `trail.py`: Target 2.0R calculation, structural stop placement, and profit-preserving fee-lock logic.
  * `risk.py`: Portfolio risk aggregation and daily loss limit enforcement.
  * `schedule.py`: IST session schedule state machine.
* **`apex3/reconciliation/`**:
  * `reconciler.py` & `state_machine.py`: Broker order status verification, crash recovery, and position phase tracking (`OPEN_PROTECTED`, `CLOSED`, etc.).
* **`apex3/broker/`**:
  * `hybrid_adapter.py`: Production dual-broker engine combining Angel One SmartAPI live WebSocket tick data with Zerodha Kite Connect order execution.
  * `paper.py`: Deterministic simulation broker.
* **`backtest_3months.py` & `backtest_6months.py`**:
  * Conservative next-bar execution replay with 4 bps spread and itemized round-trip Zerodha + NSE statutory charges.
* **`.agent/skills/apex3-quant-rules/SKILL.md`**:
  * Mandatory quantitative invariants and permanent mathematical rules.
* **`WALKTHROUGH_AUDIT_REPORT.md`**:
  * Full 3M & 6M portfolio performance tables, Bull vs Bear attribution, and 32-stock ranked screener audit.
* **`three_month_conservative_audit.json` & `six_month_conservative_audit.json`**:
  * Full JSON audit logs with trade-by-trade metrics.

## 3. Verified Production Benchmarks
* **3-Month Backtest (57 sessions)**: 12 Trades | 66.67% Win Rate | 8.56 Profit Factor | ₹+611.37 Net P&L (+4.08% ROI) | 21.98% Friction.
* **6-Month Backtest (122 sessions)**: 15 Trades | 60.00% Win Rate | 3.67 Profit Factor | ₹+566.29 Net P&L (+3.78% ROI) | 21.88% Friction.
* **Test Suite**: 52 passed unit tests in `apex3/tests/` (100% pass rate).
