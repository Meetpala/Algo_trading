# APEX-3: Zerodha Intraday Trading System (NSE Cash Equity)

> [!WARNING]
> **NO GUARANTEE OF PROFIT OR FILL**: Intraday equity trading with leverage and stop orders carries substantial financial risk. Stop-loss limit (SL) orders can fail to fill during rapid price movements, circuit limits, or exchange outages. Broker square-off timings can vary and are a backstop only. This system makes **no claim or guarantee of profitable edge, fill execution, or exit pricing**.

---

## 1. System Overview & Architecture

APEX-3 is a conservative, deterministic Python intraday trading system designed for NSE cash equities using **MIS (Margin Intraday Squareoff)** via the Zerodha Kite Connect v3 API.

The system is **simulation-first**: it boots in **Paper Trading Mode** by default and cannot submit live orders unless an explicit 6-stage compliance and safety gate is satisfied.

```
                    ┌────────────────────────────────────────────────────────┐
                    │               Operator Dashboard (Flask)               │
                    │               Bound strictly to 127.0.0.1              │
                    └───────────────────────────┬────────────────────────────┘
                                                │
                                    ┌───────────▼───────────┐
                                    │    Strategy Engine    │
                                    │ (12-Step Determinism) │
                                    └───────────┬───────────┘
                         ┌──────────────────────┼──────────────────────┐
                         ▼                      ▼                      ▼
                  ┌──────────────┐       ┌──────────────┐       ┌──────────────┐
                  │Market Regime │       │Signal Gen &  │       │Position Size │
                  │  (NIFTY 50)  │       │ 8 Entry Gates│       │& Equal Budget│
                  └──────────────┘       └──────────────┘       └──────────────┘
                         │                      │                      │
                         └──────────────────────┼──────────────────────┘
                                                │
                                    ┌───────────▼───────────┐
                                    │    Broker Interface   │
                                    └───────────┬───────────┘
                                                │
                         ┌──────────────────────┴──────────────────────┐
                         ▼                                             ▼
                 ┌───────────────┐                             ┌───────────────┐
                 │  PaperBroker  │                             │  KiteAdapter  │
                 │ (In-memory,   │                             │(Live-gated,   │
                 │ deterministic)│                             │  token rate)  │
                 └───────────────┘                             └───────────────┘
```

---

## 2. Verified Broker & Regulatory Facts (As of 20 October 2026)

All parameters adhere to official documentation verified on **20 October 2026**:

| Subject | Current Official Rule | Source Link |
|---|---|---|
| **Static IP Mandate** | **Mandatory since 1 April 2026** for API order placement/modification. | [Zerodha Static IP Policy](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/static-ip) |
| **SEBI Algo Rules** | SEBI circular `SEBI/HO/MIRSD/MIRSD-PoD/P/CIR/2025/0000013`: Under 10 OPS retail scripts do not require separate exchange empanelment if authenticated via OAuth/2FA and static IP. | [SEBI Retail Algo Framework](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf) |
| **Modification Cap** | **Max 25 modifications per order** per session. APEX-3 enforces headroom (stops modifying after 22). | [Kite Connect Exceptions](https://kite.trade/docs/connect/v3/exceptions/) |
| **Order Rate Limits** | 10 Orders/sec per client account; 200 orders/minute. | [Kite Connect Limits](https://kite.trade/docs/connect/v3/exceptions/) |
| **Quote Rate Limits** | 1 Request/sec for REST quote endpoint. | [Kite Connect Limits](https://kite.trade/docs/connect/v3/exceptions/) |
| **Square-Off Timing** | Zerodha RMS auto square-off begins ~15:15–15:20 IST. APEX-3 begins flattening at **14:55 IST** and verifies flat by **14:58 IST**. | [Zerodha Market Sessions](https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/intraday-auto-square-off-timings) |

---

## 3. Quickstart & Installation (Windows-Friendly)

### Prerequisites
- Python 3.11+
- Windows PowerShell or Command Prompt

### Step 1: Virtual Environment Setup
```powershell
# Clone or navigate to the repository directory
cd "c:\Users\itsme\OneDrive\Desktop\Algo Trading"

# Create virtual environment
python -m venv .venv

# Activate virtual environment (Windows PowerShell)
.venv\Scripts\Activate.ps1

# Install dependencies
.venv\Scripts\pip install -r requirements.txt
```

### Step 2: Configuration & Secrets
Copy `.env.example` to `.env` (never commit `.env`):
```powershell
Copy-Item .env.example .env
```
Leave `LIVE_TRADING_ENABLED=false` for Paper Trading mode.

### Step 3: Run Paper Trading Dashboard
```powershell
.venv\Scripts\python -m apex3.main
```
Open your browser at `http://127.0.0.1:5000` to monitor:
- Active Watchlist (select up to 3 NSE stocks)
- Broad-market regime (NIFTY 50 EMA-20 slope + ADX)
- Real-time reason codes for `WAIT` / `HALT`
- Broker-reported margin and risk budgets
- Protective stops and monotonic trailing progress

### Step 4: Run Simulation Cycle
```powershell
.venv\Scripts\python -m apex3.main --simulate
```

### Step 5: Run the Costed Walk-Forward Comparison
```powershell
.venv\Scripts\python analyze_trailing_sl.py
```
This writes `strategy_oos_report.json` with the 1.5R, 2R, and 50% partial-at-1R/2R-runner results. It uses the full 15-symbol candidate pool, the configured 0.25% risk sizing, and the latest 20 common sessions as the holdout. The report keeps the earlier sessions separate and records the fill, stop-priority, risk-limit, and charge assumptions.

### Step 6: Run Automated Test Suite
```powershell
.venv\Scripts\python -m pytest -v
```

---

## 4. Strategy Rules & Deterministic Pipeline

### Time Windows (IST = Asia/Kolkata)
- **09:15 – 09:30**: Observe only. Constructs the 15-minute Opening Range High and Low. **Zero entries permitted**.
- **09:30 – 14:15**: Active Entry Window. Evaluates only completed 5-minute candles.
- **14:15 – 14:55**: Manage Only. Pending entry orders are cancelled. Existing risk is managed.
- **14:55 – 14:58**: Controlled Flattening Sequence. Closes all strategy MIS positions.
- **14:58 – 15:00**: Verification. Confirms flat book. Reports emergency alert if any position remains open.

### Broad-Market Regime (NIFTY 50)
- **Long Regime**: NIFTY 50 close > 5m EMA-20, EMA-20 slope > 0, 5m ADX ≥ 22.
- **Short Regime**: NIFTY 50 close < 5m EMA-20, EMA-20 slope < 0, 5m ADX ≥ 22.
- **Neutral/Mixed**: ADX < 22, flat slope, or conflicting price. **System returns WAIT**.
- *Note: NIFTY 50 uses OHLC trend and ADX only. No fake cash-index VWAP is computed.*

### Stock Signal (Exact Mirror Conditions for Long / Short)
A candidate stock must satisfy all 8 gates simultaneously on a completed 5-minute bar:
1. Completed close beyond Opening Range (Close > OR High for Long; Close < OR Low for Short).
2. Session VWAP alignment (Close > VWAP for Long; Close < VWAP for Short).
3. Benchmark relative strength / weakness (> 0% for Long; < 0% for Short).
4. Relative volume ≥ 1.5× of the prior 20-bar 5m average.
5. Bid-Ask spread ≤ 8 basis points.
6. Safe circuit limit distance (≥ 2.0% away from Upper/Lower Circuit).
7. Quote data fresh (≤ 2.0 seconds stale threshold).
8. Market regime aligned (BULLISH for Long, BEARISH for Short).

### Position Sizing & Equal Allocation
- Usable Margin ($M$): Fresh broker-reported equity after deducting the 5% safety reserve.
- Leverage ($L$): The minimum of 4.0× ceiling and broker order margin estimate. If broker offers < 4.0×, the symbol is skipped.
- Qualified Symbols ($N$): Number of symbols passing all gates (up to 3).
- Equal Notional Budget: $B = (M \times L) / N$.
- Share Quantity: $\min(Q_{\text{notional}}, Q_{\text{risk}}, Q_{\text{margin}})$.
- Rupee Risk Budget: 0.25% of equity per trade. Combined open risk capped at 0.75%.

### Protective Stop & Monotonic Trailing
1. **Initial Stop**: Set beyond the signal candle extreme + ATR pad (rounded to 0.05 tick).
2. **Immediate Protection**: Placed immediately upon order fill for the exact filled quantity.
3. **Threshold (+1R)**: Stop stays at initial level until favorable movement reaches $+1.0R$.
4. **Profit Lock**: At $+1.0R$, stop advances to cover entry + round-trip brokerage/taxes + configured profit lock.
5. **Beyond $+1.0R$**: Trails monotonically using the highest valid of:
   - Existing stop level (enforcing monotonicity: **Long stop never moves down, Short stop never moves up**)
   - Cost-adjusted profit floor
   - ATR trail from High-Water Mark / Low-Water Mark (normal 2.5×, or tightened 1.5× during exhaustion/late session)
   - Confirmed 1-minute structural swing point
6. **Modification Budget**: Tracks Kite's 25-mod limit; ceases trailing if modification count exceeds 22.

---

## 5. Live-Mode Safety Gate Requirements

To enable live trading, the user must explicitly satisfy all 6 gates:
1. Change `mode: "live"` in `config.yaml`.
2. Set `LIVE_TRADING_ENABLED=true` in `.env`.
3. Configure and verify your static IPv4 address with Zerodha developer portal (`STATIC_IP_CONFIGURED=true`).
4. Complete regulatory self-declaration for SEBI/NSE algo compliance (`SEBI_COMPLIANCE_ACKNOWLEDGED=true`).
5. Populate valid `KITE_API_KEY` and daily `KITE_ACCESS_TOKEN`.
6. Confirm live session authorization on startup; audit confirms 0 foreign/manual positions.

---

## 6. Audit & SQLite Trade Journal

All decisions, gate evaluations, orders, trade fills, stop updates, and daily summaries are stored in a local SQLite database (`apex3_journal.db`):
- `events`: Every pipeline evaluation, gate pass/fail, and state transition with IST timestamps.
- `orders`: Order state machine tracking (status, filled qty, avg price, mod count).
- `fills`: Confirmed exchange fills with trade IDs.
- `stop_changes`: Monotonic trail history with R-multiples and reason text.
- `warnings`: Actionable reason codes (e.g. `STALE_BENCHMARK_DATA`, `UNKNOWN_MANUAL_POSITIONS`).

---

## 7. Performance Validation Methodology

Before considering live deployment, the user must conduct historical and forward validation:
1. **Walk-Forward Analysis**: Split historical 1-minute and 5-minute NSE equity data into in-sample train (60%) and out-of-sample test (40%) splits.
2. **Friction Inclusion**: Apply estimated Zerodha NSE equity intraday charges (0.03% or ₹20 per executed order, whichever is lower; STT 0.025% on sells; NSE transaction charges 0.00307%; SEBI fee; GST 18%; stamp duty 0.003% on buys) plus the configured 0.10% sizing allowance.
3. **Survivorship & Look-Ahead Controls**: Use point-in-time constituent lists; never evaluate uncompleted candles.
4. **Paper Trading Period**: Execute a minimum 30-day paper trading validation run to verify feed stability, order reconciliation, and stop adherence.
