# Strategy Profitability and Risk Tracking Plan

**Goal:** Correct APEX-3 daily loss accounting and timestamp alignment, then compare fixed 1.5R, fixed 2R, and a 50% exit at 1R with the runner targeting 2R on a separate out-of-sample period.

**Architecture:** Add a small fill-accounting module that derives strategy-only realized P&L and estimated fees from the broker's session executions, and mark open positions from current quotes before evaluating the daily loss gate. Rework the strategy comparison around exact timezone-aware candle timestamps, strict 0.25% risk sizing, portfolio limits, conservative stop-first fills, and a chronological train/test split. Keep live defaults at 0.25% risk, 2R, no trail, and scanner disabled unless the out-of-sample evidence clears costs.

**Tech Stack:** Python, pandas, SQLite journal, existing APEX-3 broker and strategy interfaces.

**Spec:** User-approved recommendation in the current conversation.

## Global Constraints

- Per-trade planned risk remains 0.25% of equity.
- Combined open risk remains capped at 0.75%; daily loss stop remains 1.0%.
- Live target remains fixed 2R with trailing disabled during comparison.
- The 15-symbol scanner stays disabled in `apex3/config.yaml` pending positive costed out-of-sample evidence.
- Backtest capital is ₹15,000; report net P&L after estimated Zerodha equity intraday charges.
- Use a distinct chronological holdout; no parameter selection from holdout results.
- Do not run the project test suite; the requested backtest is the evaluation deliverable.

## Review Focus

- Missing or timezone-naive fill timestamps: treat broker session trades consistently in IST and fail closed if accounting data cannot be obtained.
- Partially closed or flipped positions: realize P&L only for the closed quantity and include every executed order's fees.
- Candles missing for one symbol at a timestamp: do not shift to that symbol's next row; skip that symbol's signal/mark for that interval.
- Stop and target both touched in one candle: use the conservative stop-first assumption.
- OOS selection leakage: select any preferred candidate on the earlier period and report the latest period independently.

---

### Task 1: Wire accurate daily loss accounting

**Files:** Create `apex3/strategy/accounting.py`; modify `apex3/strategy/engine.py` and `apex3/config.yaml`.

**Interfaces:** Accounting consumes broker trades, broker orders, session date, open positions, strategy tag, and cost configuration; it returns realized gross P&L and estimated charges. Engine adds marked open P&L and passes all three components to `evaluate_portfolio_risk` on every eligible cycle.

- [x] Compute weighted-average realized gross P&L from strategy-tagged fills and costs by executed order.
- [x] Mark open broker positions from quotes and evaluate the daily loss threshold before considering entries.
- [x] Keep the daily stop sticky for the session and fail closed if required broker accounting data is unavailable.
- [x] Correct NSE cash transaction charge estimate to 0.00307%.

### Task 2: Make strategy backtest timestamp-safe and comparable

**Files:** Modify `backtest_engine.py`; create `walk_forward_backtest.py`; update `analyze_trailing_sl.py` to invoke the corrected comparison.

- [x] Use timezone-aware exact candle timestamp lookup for every symbol and calculate signal availability at candle close.
- [x] Use common historical sessions, with the earlier sessions for selection and the latest 20 sessions as the separate holdout.
- [x] Simulate the configured 15-symbol universe using strict 0.25% risk, 0.75% combined risk, 1.0% daily loss limit, 4x notional ceiling, and ₹15,000 capital.
- [x] Compare 1.5R, 2R, and 50% exit at 1R with the remainder targeting 2R; apply the same fill and fee assumptions to all variants.
- [x] Save a JSON report with train and out-of-sample dates, per-variant metrics, trades, charges, and explicit assumptions.

### Task 3: Review code and generated results

- [x] Inspect changed code paths for accounting double-counts, timestamp shifts, and sizing violations.
- [x] Run only the requested walk-forward backtest and report whether any candidate was positive after charges out of sample.
- [x] Keep current deployment defaults unchanged because no candidate was positive after estimated charges on the holdout.
