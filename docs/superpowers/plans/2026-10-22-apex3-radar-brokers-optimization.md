# Implementation Plan: Synchronize StrategyEngine with Audited Elite 10 Institutional Benchmark

## 1. Executive Summary & Objective
This plan locks the audited **Elite 10 Institutional Trend Leaders** into `apex3/config.yaml`, implements the **Second-Slot Unlock Gate (+0.50R)**, **Max 1 Entry Per Bar (Conviction Ranked)**, and **One-Time Fee-Protected Breakeven Step** inside `StrategyEngine.evaluate_step()`, and synchronizes `backtest_3months.py` to verify the **+₹2,044.67 Net Realized P&L (+13.63% ROI, 59.72% Win Rate)** benchmark across 57 sessions on ₹15,000 capital.

---

## 2. Proposed Changes & Action Plan

### Task 1: Lock Audited Elite Watchlist & Statutory Rate in `apex3/config.yaml`
1. **Core Watchlist**:
   Set `radar.core_watchlist` strictly to the audited Elite 10 Trend Leaders:
   ```yaml
   radar:
     core_watchlist:
       - "HINDALCO"
       - "JSWSTEEL"
       - "RELIANCE"
       - "MARUTI"
       - "ITC"
       - "TCS"
       - "SBIN"
       - "TRENT"
       - "KOTAKBANK"
       - "SUNPHARMA"
   ```
   > [!NOTE]
   > **Elite 10 vs Elite 9 Empirical Finding**:
   > Experimental testing across all 57 sessions proved that removing `SUNPHARMA` (Elite 9) drops Net P&L from **+₹2,044.67 to +₹1,645.97** and drops Win Rate from **59.72% to 55.88%** because Sun Pharma provides non-correlated healthcare diversification on days when metals/auto pause. Therefore, **Elite 10 is mathematically superior** and will be locked in.
2. **Disable Laggards**:
   Set `enable_extended_radar: false` and `custom_user_radar: []` so choppy, negative-expectancy names (`HAL`, `AXISBANK`, `BEL`, `BAJFINANCE`, `LT`, `M&M`, `HDFCBANK`) cannot enter active scanning.
3. **NSE Statutory Turnover Charge**:
   In `apex3/config.yaml` (line 149), update `costs.exchange_txn_pct` from `0.00307` to `0.00318` to match the exact audited NSE turnover fee rate specified in `SKILL.md` and `APEX3_TRADING_MASTER.md`.

---

### Task 2: Wire Slot-2 Lock, Conviction Ranking & Fee-Lock into `StrategyEngine`
1. **Data Model Updates**:
   - In `apex3/reconciliation/state_machine.py`: Add `fee_lock_applied: bool = False` to `ManagedPosition`.
   - In `apex3/models.py`: Add `fee_lock_applied: bool = False` to `PositionState`.
2. **Watchlist Resolution (`get_active_watchlist`)**:
   - In `apex3/strategy/engine.py:StrategyEngine.get_active_watchlist()`:
     Check if `self.config.raw_dict` contains a `radar` section; if so, return `build_active_radar(self.config.raw_dict)`. This ensures `config.yaml`'s radar configuration is the single source of truth across live, paper, and dashboard views.
3. **One-Time Fee-Protected Breakeven Step (Step 11, line 364)**:
   - In the `else:` branch of position management (when `trail_enabled` is `False`), evaluate:
     - `should_lock = not pos.fee_lock_applied and (open_r >= 1.65 or (now.strftime("%H:%M") >= "13:45" and open_r >= 0.75))`
     - If triggered, calculate `protected_stop = ceil_to_tick(pos.average_entry * 1.0015, tick_size)` for `Side.BUY` or `floor_to_tick(pos.average_entry * 0.9985, tick_size)` for `Side.SELL`.
     - Modify `pos.protective_order_id` on the broker, update `pos.current_stop_price = protected_stop`, set `pos.fee_lock_applied = True`, increment `pos.stop_modification_count += 1`, and log the event via `self.journal.log_stop_change`.
4. **Second-Slot Unlock Gate & 1 Entry Per Bar (Step 8, lines 493–625)**:
   - When `active_managed_count == 1`:
     Compute the active position's `unrealized_r`. If `unrealized_r < 0.50`, halt new entry evaluation immediately and return `StrategyDecision(decision=Decision.WAIT, reason_codes=["SECOND_SLOT_LOCKED_UNTIL_0.5R"])`.
   - In `qualified_candidates`:
     Compute Conviction Score:
     $$\text{Conviction} = \frac{|RS| \times 100 \times \text{Vol\_Ratio}}{\max(\text{Spread}_{\text{bps}}, 1.0)}$$
     Sort descending and slice to `max_new_entries_per_bar` (`qualified_candidates[:1]`), deploying at most 1 new entry per 5-minute bar into the highest-conviction institutional leader.

---

### Task 3: Synchronize `backtest_3months.py` & Mandatory Verification
1. **Synchronize `backtest_3months.py`**:
   - Update `backtest_3months.py` to dynamically load `build_active_radar(config.raw_dict)`.
   - Incorporate the 2-slot limit, second-slot profit unlock gate (+0.50R), 1-entry-per-bar conviction ranking, and fee-protected breakeven step.
2. **Terminal Verification Suite**:
   - Run `.venv\Scripts\python.exe backtest_3months.py` and confirm:
     - Net Realized P&L $\ge \mathbf{+₹2,044.67}$ (+13.63% ROI).
     - Win Rate $\ge \mathbf{59.72\%}$.
     - Statutory friction $< 26.0\%$ of gross profit.
   - Run `.venv\Scripts\pytest.exe apex3/tests -q` and confirm **47 of 47 unit tests pass with 0 errors**.

---

## 3. Verification Criteria & Invariants Checklist

- [ ] Account Capital Base: Exactly ₹15,000.00 with 5% safety buffer (₹14,250 usable margin, ₹57,000 max 4x MIS buying power).
- [ ] Max Concurrent Slots: Exactly 2 slots (₹28,500 budget per slot).
- [ ] Risk-per-trade cap: 0.80% (₹120 max loss).
- [ ] Hard daily loss stop: 1.60% (-₹240 max loss).
- [ ] Net Realized P&L: Must exceed +₹1,083.53 baseline and verify the +₹2,044.67 Elite 10 benchmark.
- [ ] Unit Tests: 100% pass rate.
