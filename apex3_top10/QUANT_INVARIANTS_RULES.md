---
name: apex3-quant-rules
description: >-
  Permanent quantitative trading rules, capital sizing limits, Zerodha statutory friction constraints,
  structural stop-loss and 2.0R target protocols, and mandatory backtest verification for the APEX-3 algo engine.
  Activate whenever modifying apex3/ codebase or backtest_3months.py.
---

# APEX-3 Quantitative Invariants & Production Trading Rules

This skill documents and enforces the four permanent quantitative invariants of the **APEX-3 Intraday Trend Following System**. Whenever proposing, generating, modifying, or testing code in `@apex3/` or `@backtest_3months.py`, you **MUST** strictly adhere to every invariant below.

---

## 1. Capital Base & Exposure Constraint
* **Base Account Equity**: Exactly **₹15,000.00 INR**.
* **Safety Reserve Buffer**: **5.0%** cash reserve ($₹750.00$) kept completely idle to prevent margin breach penalties and RMS square-off fees.
  $$\text{Usable Equity} = ₹15,000.00 \times (1 - 0.05) = \mathbf{₹14,250.00}$$
* **Intraday Leverage (Zerodha MIS)**: Maximum **$4.0\times$** leverage.
  $$\text{Total 4x MIS Buying Power} = ₹14,250.00 \times 4.0 = \mathbf{₹57,000.00}$$
* **Position Slots & Budgeting**:
  * **Max Concurrent Positions**: **2 slots** (down from 3 slots).
  * **Per-Slot Leveraged Budget**: $\frac{₹57,000.00}{2} = \mathbf{₹28,500.00 \text{ per trade}}$.
  * **Risk-per-Trade Cap**: **0.80%** of account equity ($\mathbf{₹120.00 \text{ max risk per trade}}$).
  * **Hard Daily Loss Limit**: **1.60%** of account equity ($\mathbf{-₹240.00 \text{ hard daily stop}}$). If realized daily net loss hits $-₹240.00$ (equivalent to 2 consecutive maximum losses), all new entries are halted for the remainder of the trading day.
  * **Second-Slot Unlock Gate**: Governed directly by available equity margin and capacity (`second_slot_requires_slot1_profit_r: 0.0`). As long as position capacity (< 2 slots) and usable margin allow, Slot 2 trades independently with its dedicated ₹28,500 slot budget and ₹120.00 risk cap, taking all structural stop-loss and gate validations without requiring Slot 1 profit.
  * **New Entries Per Bar**: Maximum **1 new entry** on any single 5-minute candle (`max_new_entries_per_bar: 1`), deploying capital only into the #1 highest-conviction institutional breakout across the radar.

---

## 2. Friction Rule: Exact Zerodha & Statutory Fee Accounting
Every backtest, simulation, or live sizing logic modification **must** compute exact round-trip Zerodha + NSE statutory charges:

* **Brokerage**: $0.03\%$ of turnover or ₹20.00 per executed order, whichever is lower (applied on both Buy and Sell legs).
* **STT (Securities Transaction Tax)**: $0.025\%$ on the **Sell** turnover only.
* **NSE Exchange Transaction Turnover Charge**: $0.00318\%$ on total turnover (both legs).
* **SEBI Turnover Charge**: $0.00010\%$ (₹10 per Crore) on total turnover.
* **Stamp Duty**: $0.003\%$ on **Buy** turnover only.
* **GST**: $18.0\%$ on $(\text{Brokerage} + \text{NSE Turnover Charge} + \text{SEBI Fee})$.

### Total Statutory Cost Benchmark
On a standard ₹28,500.00 leveraged trade, total round-trip charges evaluate to **$\approx 0.1065\%$ to $0.1075\%$** ($\approx ₹30.35$ to $₹30.74$).

### Friction Thresholds
* **Gross Profit Ratio**: Total charges across all trades in any backtest must remain **$< 26.0\%$ of gross profit**.
* **Per-Trade Sizing Gate**: A trade is automatically **rejected** at order sizing if estimated round-trip charges consume **$> 18.0\%$** of the expected $2.0R$ gross profit ($2.0 \times R \times Q$).
* **Spread Gate (Gate 6)**: Bid-Ask Spread must be $\le 4.0\text{ bps}$ ($0.04\%$) to guarantee zero-slippage limit execution.

---

## 3. Stop & Target Rule: Structural Anchor, Fixed 2.0R & Fee Lock
Continuous trailing stop-loss is **strictly disabled** (`trail_enabled: false`) to avoid prematurely choking institutional momentum runners. Instead, trades use structural anchor levels with a one-time fee-plus-breakeven protection step:

* **Structural Stop Anchor**:
  * **LONG**: $\text{Initial Stop} = \text{floor}_{0.05}(\text{Swing Low}_5 - 1.5 \times \text{ATR}_{14})$.
  * **SHORT**: $\text{Initial Stop} = \text{ceil}_{0.05}(\text{Swing High}_5 + 1.5 \times \text{ATR}_{14})$.
  * The $1.5\times \text{ATR}$ structural buffer is mandatory (empirically superior to $1.0\times \text{ATR}$).
* **Gate 9 Stop-Width Sweet Spot**:
  * **Gate 9A (Minimum Volatility)**: $\frac{\text{ATR}_{14}}{\text{Price}} \ge 0.0022$ ($0.22\%$) to filter out sluggish/defensive stocks.
  * **Gate 9B (Stop Width Bounds)**: Risk distance $R\%$ must satisfy:
    $$0.55\% \le R\% \le 0.90\% \quad \left(\text{where } R\% = \frac{|\text{Entry} - \text{Initial Stop}|}{\text{Entry}}\right)$$
    * $R\% < 0.55\%$ is rejected (stops too narrow where Zerodha charges consume $> 40\%$ of the payout).
    * $R\% > 0.90\%$ is rejected (stops too wide where a 2.0R target requires $> 1.80\%$ intraday move, unachievable before 14:55).
  * **Gate 9C (ADR Exhaustion Filter)**: Reject breakout if the stock has already consumed $> 60\%$ of its 14-day Average Daily Range ($\text{ADR Consumed} > 0.60$, with 2.5% minimum baseline).
* **Target Multiple**: Fixed **$2.0R$** Limit Target order placed simultaneously with entry.
* **Profit-Preserving Fee Lock Step**:
  * Step stop to **preserve profit** without continuous trailing:
    1. Triggers when favorable excursion reaches **$\ge +1.60R$**, locking **$+0.60R$** gross profit ($\approx +₹65$ to $+₹75$, covering all Zerodha + statutory charges plus a profit buffer).
    2. Session time reaches **$14:15 \text{ IST}$** and current unrealized profit is $\ge \mathbf{+1.15R}$, locking **$+0.35R$** gross profit.
  * Once the fee lock is applied, the stop is never loosened or continuously trailed.
* **EOD Liquidation**: Strict square-off at **14:55 IST** before Zerodha's 15:15 RMS auto-square-off penalty window.

---

---

## 4. Live Execution Safety & Broker Reconciliation Invariants
The live execution pipeline enforces strict zero-leakage broker safeguards:

* **Partial Fill Protection**:
  * On any partial fill, the unfilled remainder is **immediately cancelled and confirmed** with the broker before placing protection.
  * The protective stop-loss order is placed for **strictly the filled quantity**.
  * The position remains in `OPEN_UNPROTECTED` until the broker confirms the stop status as `TRIGGER_PENDING`. If unconfirmed within timeout, the position is immediately liquidated via market order.
  * The engine accepts **only `TRIGGER_PENDING`** as valid resting protection (rejecting `OPEN` which represents an already-triggered unfilled limit order, and `COMPLETE` which indicates the stop has already been executed).
* **Managed `EXITING` State & Target Exit Idempotence**:
  * At the $2.0R$ target, the resting stop is cancelled and verified with the broker **prior** to submitting the take-profit market exit.
  * The order ID is tracked in `pos.exit_order_id` with `pos.phase = PositionPhase.EXITING`.
  * Subsequent 3-second evaluation cycles poll the existing exit order and reconcile broker fills. Submitting duplicate exit orders while an exit is pending or in-flight is strictly forbidden.
* **Tracked One-Shot EOD & Emergency Flatten**:
  * The 14:55 IST flatten sequence cancels resting orders, submits market exits for reconciled positions, and polls broker position quantities up to a 10s deadline until the broker confirms net open quantity is $0$.
  * Once verified flat, subsequent cycles return `Decision.WAIT` (`FLATTEN_COMPLETED`) without re-submitting orders or looping.
* **Feed Freshness & Zero Synthetic Spreads**:
  * Quotes must supply valid source feed timestamps. Feeds older than $15.0\text{s}$ or missing depth (`bid <= 0 or ask <= 0 or ask < bid`) fail closed immediately.
  * LTP substitution for missing depth or synthetic zero-spread fabrication is strictly prohibited.
  * Fabricated $\pm 10\%$ circuit limit fallbacks are eliminated; limits must come directly from verified exchange instrument metadata.
* **Restart Recovery & State Persistence**:
  * On engine restart, open positions are reconciled by matching broker orders against strategy session tags (`self.session_tag`), reconnecting resting `TRIGGER_PENDING` stops without triggering false `UNKNOWN_MANUAL_POSITIONS` halts.
  * Broker positions with zero quantity immediately clear corresponding managed positions.
* **Hard Daily Stop Liquidation**:
  * If daily net realized loss breaches the hard limit ($-₹240.00$), the engine not only halts new entries but immediately executes `reconciler.execute_flatten_sequence()` to liquidate any remaining open positions, guaranteeing a hard loss ceiling.
* **Decoupled Risk Management**:
  * Compliance gates (live trading enabled, static IP, SEBI acknowledgements) gate **only new trade entry proposals**. Managing existing open positions, trailing stops, fee-locks, and EOD flatten operations remain active regardless of entry gate states.
* **Official 2026 NSE Holiday Calendar & Special Sessions**:
  * Fully synchronized with official NSE Circular FAOP71777 (15 official weekday holidays).
  * Special sessions (e.g., Muhurat Trading on 2026-11-08) transition to `SessionState.SPECIAL_SESSION` and block new MIS intraday entries (`SPECIAL_SESSION_NO_MIS`).

---

## 5. Mandatory Verification Protocol & Benchmarks
Before declaring any changes complete on `@apex3/` or `@backtest_3months.py`:

1. **Terminal Command Execution**:
   ```powershell
   python backtest_3months.py
   ```
2. **Verified 57-Session Conservative 3-Month Benchmark**:
   * **Starting Capital**: ₹15,000.00 (4.0x leverage = ₹57,000 buying power, 2 slots)
   * **Total Completed Trades**: 15 (11 Wins / 4 Losses)
   * **Profit Probability (Win Rate)**: **73.33%**
   * **Profit Factor**: **9.67**
   * **Gross P&L**: ₹+933.30
   * **Zerodha & Statutory Charges**: ₹254.08
   * **Net Realized P&L**: **₹+679.22 (+4.53% ROI)**
   * **Statutory Friction / Winning Gross**: **24.41%** (strictly satisfies `< 26.0%` invariant)
   * **Monthly Consistency**: All 3 individual monthly periods positive:
     * August 2026: 5 trades, 60.0% win rate, ₹+17.81 net
     * September 2026: 7 trades, 71.4% win rate, ₹+543.00 net
     * October 2026: 3 trades, 100.0% win rate, ₹+118.41 net
3. **Unit Test Verification**:
   ```powershell
   pytest apex3/tests -q
   ```
   * All 72 unit tests (including `test_live_blockers.py` and `test_radar_conviction.py`) must pass with **0 failures and 0 errors**.
