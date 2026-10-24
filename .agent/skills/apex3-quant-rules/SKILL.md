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
    $$0.45\% \le R\% \le 0.85\% \quad \left(\text{where } R\% = \frac{|\text{Entry} - \text{Initial Stop}|}{\text{Entry}}\right)$$
    * $R\% < 0.45\%$ is rejected (stops too narrow where Zerodha charges consume $> 40\%$ of the payout).
    * $R\% > 0.85\%$ is rejected (stops too wide where a 2.0R target requires $> 1.70\%$ intraday move, unachievable before 14:55).
  * **Gate 9C (ADR Exhaustion Filter)**: Reject breakout if the stock has already consumed $> 60\%$ of its 14-day Average Daily Range ($\text{ADR Consumed} > 0.60$).
* **Target Multiple**: Fixed **$2.0R$** Limit Target order placed simultaneously with entry.
* **One-Time Fee-Protected Breakeven Step**:
  * Move stop to **$\text{Entry} \pm 0.15\%$** (which covers the 0.1065% round-trip Zerodha statutory fees plus 1 tick buffer) **only** if:
    1. Unrealized profit reaches **$+1.65R$** ($82.5\%$ of the distance to target), OR
    2. Session time reaches **$13:45 \text{ IST}$** and unrealized profit is $\ge \mathbf{+0.75R}$.
  * Once the fee lock is applied, the stop is never loosened or continuously trailed.
* **EOD Liquidation**: Strict square-off at **14:55 IST** before Zerodha's 15:15 RMS auto-square-off penalty window.

---

## 4. Mandatory Verification Protocol
Before completing any task, commiting code, or declaring an edit complete on `@apex3/` or `@backtest_3months.py`:

1. **Terminal Command Execution**:
   ```powershell
   python backtest_3months.py
   ```
2. **Benchmark Verification Criteria**:
   * **Net P&L**: Must be **$> +₹1,083.53$** on the baseline ₹15,000 capital.
   * **Win Rate**: Must be **$\ge 58.0\%$**.
   * **Profit Factor**: Must be **$\ge 2.90$**.
   * **Total Charges**: Must be itemized and confirm statutory accuracy.
3. **Unit Test Verification**:
   ```powershell
   pytest apex3/tests -q
   ```
   * All unit tests (including `test_radar_conviction.py`) must pass with **0 failures and 0 errors**.
