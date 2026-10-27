# APEX-3 Top Core Engine Files

This folder contains the self-contained, audited core files of the **APEX-3 Quantitative Intraday Trading Engine**.

## File Manifest & Descriptions

| File Name | Component | Core Functionality |
|---|---|---|
| [`engine.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/engine.py) | **Execution Strategy Engine** | Master state machine, 11-step evaluation loop, order dispatch, partial-fill protection, target/gap exits, and emergency square-offs. |
| [`signal.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/signal.py) | **Signal Generation & Gates** | Multi-gate filtering (Opening Range breakout, volume confirmation >= 1.5x, relative strength, VWAP alignment, safe circuit limits). |
| [`reconciler.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/reconciler.py) | **Broker Reconciliation** | Startup position audit, tag matching, unconfirmed order recovery, and tracked one-shot EOD flatten sequence. |
| [`models.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/models.py) | **Domain Data Models** | Core data structures: `OrderRequest`, `OrderState`, `PositionState`, `Quote`, `Candle`, `InstrumentInfo`, and `Decision` enums. |
| [`config.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/config.py) | **Configuration & Risk Bounds** | Pydantic/dataclass config definitions, environment variable validation, and risk parameters. |
| [`sizer.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/sizer.py) | **Position Sizing Engine** | Strict ₹15,000 capital base sizing, ₹120 max loss per trade cap, 4.0x MIS leverage, and 2-slot concurrency enforcement. |
| [`trail.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/trail.py) | **Stops & Profit Protection** | Structural swing stops ($1.5 \times \text{ATR}_{14}$), fixed 2.0R limit targets, and +1.60R fee-lock to lock +0.60R profits. |
| [`regime.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/regime.py) | **Market Regime Classifier** | NIFTY 50 benchmark trend classification (Bullish, Bearish, Sideways/Chop) to enforce directional alignment. |
| [`backtest_3months.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/backtest_3months.py) | **3-Month Audit Replay** | Audited conservative next-bar replay verifying statutory friction (< 26%), 73.3% win rate, and profit factor 8.00. |
| [`backtest_1year.py`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/backtest_1year.py) | **Full 1-Year Audit Replay** | 247-session replay across 2025-2026, generating month-by-month, quarterly, and stock-by-stock attribution. |
| [`QUANT_INVARIANTS_RULES.md`](file:///c:/Users/itsme/OneDrive/Desktop/Algo%20Trading/top_files/QUANT_INVARIANTS_RULES.md) | **Permanent Quant Invariants** | Official specification of all 4 quantitative invariants, capital sizing limits, and Zerodha fee friction constraints. |

---

## How to Run Verifications

### 1. Run 3-Month Conservative Audit Replay
```powershell
python backtest_3months.py
```

### 2. Run 1-Year Full Historical Replay
```powershell
python backtest_1year.py
```

### 3. Run Pytest Test Suite
```powershell
pytest apex3/tests -v
```
