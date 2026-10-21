# APEX-3 Radar, Risk, and Free Broker Support Implementation Plan

> **For agentic workers:** Use `superpowers:using-git-worktrees` before implementation, then use `superpowers:executing-plans` task by task. Keep production mode paper unless broker-specific API onboarding and compliance settings are confirmed. Steps use checkbox syntax for tracking.

**Goal:** Implement the requested APEX-3 radar, risk, signal, regime, fee-lock, and broker upgrades, then produce a reproducible, costed three-month comparison without presenting historical results as a profit guarantee.

**Architecture:** Consolidate the currently split candidate-signal path into the engine that actually places orders. Load all strategy and broker fee settings through the typed configuration, use one date-aware charge calculator for sizing and backtests, and make the backtest replay the same entry/risk/exit rules as the engine. Keep provider adapters behind the existing `BrokerInterface` and retain paper mode as the default.

**Tech Stack:** Python, pytest, pandas, PyYAML, existing APEX-3 broker and strategy interfaces, local 5-minute CSV data.

**Spec:** User-provided request in `C:\Users\itsme\.codex\attachments\3c82f0e0-5ec7-4000-843b-c183397fa989\Pasted text.txt`.

## Global Constraints

- Starting equity is ₹15,000; reserve 5%; treat 4.0x as an exposure ceiling and use broker-reported margin where available.
- Maximum 2 concurrent positions; risk per trade 0.80%; hard daily loss limit 1.60% of starting equity; Slot 2 requires the existing position to be at least +0.50R; at most 1 new entry per completed 5-minute bar.
- Structural stop uses the 5-bar swing low/high and 1.5x ATR(14); target remains fixed at 2.0R; continuous trailing remains disabled.
- Apply one one-time stop step to entry ±0.15% at +1.65R, or at/after 13:45 IST when open profit is at least +0.75R.
- Entry window is 09:30–13:00 IST; flatten at 14:55 and verify flat by 14:58.
- Enforce all entry gates: session window, completed opening range, stock close on the correct side of session VWAP, NIFTY regime alignment, 20-bar relative strength versus NIFTY, volume surge ≥2.0x, spread ≤4 bps, circuit distance ≥2.0%, minimum 5-minute ATR/close 0.22%, stop width 0.45%–0.85%, and ADR14 consumption ≤60%; missing required data fails closed.
- Enforce the 18% fee-to-expected-2R-profit sizing guard for the selected broker tariff.
- Treat the APEX-3 verification thresholds as acceptance gates: net P&L >₹1,083.53, net win rate ≥58.0%, profit factor ≥2.90, itemized statutory fees, and total charges <26% of gross profit. The supplied baseline’s 51.18% charges/gross ratio already fails that last gate; report it as a baseline failure rather than changing its accounting.
- Keep recurring API subscription cost at ₹0; never assume 4x margin when a broker reports less.
- Optimize on training data only; keep a chronological holdout untouched and publish the full test-period result separately.
- Do not label a result production-validated unless the user’s benchmark gates and the project’s mandatory backtest/test verification both pass.

## Audit Snapshot

- The active config has some requested values, but its radar list is 17 core + 5 extended + 2 custom rather than the requested 15 + 10 + user-editable custom list.
- `apex3/config.py` still has old dataclass defaults and does not load all newer gate/regime/fee-lock settings into typed fields.
- `signal.py` contains the new `StockCandidateBar`/`evaluate_candidate_signal()` path, and `engine.py` contains `select_best_entries_for_bar()`, but the live `StrategyEngine.evaluate_step()` still follows the older `evaluate_signal()` path. The live path therefore does not consistently use the new ranker, slot-2 gate, or fee lock.
- The current candle-list regime path does not apply the configured VWAP alignment and rising-ADX requirements. The `BenchmarkSnapshot` path has those checks but is not the engine path.
- The newer charge-aware `calculate_position_size()` is separate from the `size_position()` function used by the live engine and is hard-coded to Zerodha.
- `full_backtest_report.json` matches the supplied baseline: 48 trades, 58.33% net win rate, PF 2.97, ₹2,219.50 gross, ₹1,135.97 charges, ₹1,083.53 net, 7.22% ROI, and ₹765.10 max drawdown. `three_month_results.json` differs slightly (₹2,220.20 gross and ₹1,084.23 net), so the baseline must be reproduced and the source of truth recorded.
- During planning, `scripts/run_elite20_backtest.py` appeared as an untracked workspace file and `radar_conviction_backtest_results.json` changed. Preserve both. The script labels a 22-symbol list as “Elite 20” and partitions months by thirds of trade dates; the modified report now shows 2 trades and -₹128.48 net. Treat that output as an unverified candidate run and reconcile it before using it as a baseline or overwriting it.
- `strategy_oos_report.json` reports no variant positive after estimated costs in both the three-month window and its holdout. This is prior evidence, not a result for the requested upgrades.
- Local data covers the requested period for the other listed symbols, but there is no `scratch_data/TATAMOTORS.csv`; a complete requested-universe backtest needs that series or must mark the universe incomplete.
- Current public fee pages show Zerodha NSE equity transaction charges of 0.00307%, while APEX-3’s cost calculator/quant rules contain 0.00318%. The implementation must reconcile this with date-effective official tariffs and disclose the selected rate in every report. Do not silently combine or overwrite the two assumptions.

## Review Focus

- An incomplete or stale bar, quote, benchmark, or ADR value must suppress the candidate rather than bypass a mandatory gate; test each missing-data path.
- Multiple calls during one 5-minute candle must not place duplicate entries; test repeated engine evaluation for the same completed-bar timestamp.
- Stop and target touched within the same historical bar must use stop-first fills; test the ambiguous-bar case.
- Fee calculations must remain correct for long/short trades, each broker cap, effective-date changes, and partial/exit fills; test per-leg and total reconciliation.
- Broker acceptance, rejected orders, expired sessions, and margin lower than 4x must never be treated as successful fills or assumed capital; test adapter contract behavior with fakes.

## Implementation Tasks

### Task 1: Reconcile and lock the historical baseline

**Files:**
- Modify: `backtest_3months.py`
- Modify: `walk_forward_backtest.py`
- Review/preserve: `scripts/run_elite20_backtest.py`
- Review/preserve: `radar_conviction_backtest_results.json`
- Create: `apex3/tests/test_backtest_3months.py`
- Read-only inputs: `scratch_data/*.csv`, `full_backtest_report.json`, `three_month_results.json`, `strategy_oos_report.json`

**Interfaces:** The backtest exposes a deterministic replay function that accepts preloaded symbol candles, a config, and a cost profile, and returns trades plus reconciled aggregate/monthly metrics. It must be callable without executing the script’s file-writing `__main__` block.

- [ ] Write `test_backtest_uses_only_completed_bars_and_exact_timestamps`; assert a symbol with no candle at the benchmark timestamp is skipped and a later candle is never shifted backward.
- [ ] Write `test_same_bar_stop_and_target_uses_stop_first`; assert the stop exit wins when both stop and target lie inside one OHLC bar.
- [ ] Write `test_backtest_baseline_metrics_reconcile_to_trade_ledger`; assert gross P&L minus itemized charges equals net P&L and monthly totals equal the full-period total.
- [ ] Run `pytest apex3/tests/test_backtest_3months.py -q` and confirm each new test fails for the expected missing behavior.
- [ ] Refactor the replay entry point so the requested baseline configuration is explicit and separate from the optimized configuration; preserve the current baseline report as an immutable comparison input.
- [ ] Validate the calendar/session count and every requested symbol’s coverage. Add or obtain `TATAMOTORS` 5-minute data before claiming full-radar coverage; otherwise mark that run incomplete in the report.
- [ ] Run the targeted tests and confirm the baseline ledger reconciles to the supplied 48-trade headline metrics or document the exact code/data reason for any difference.

### Task 2: Make typed configuration match the requested radar and strategy settings

**Files:**
- Modify: `apex3/config.yaml`
- Modify: `apex3/config.py`
- Modify: `apex3/strategy/engine.py` (`build_active_radar`)
- Modify: `apex3/tests/test_radar_conviction.py`
- Create or extend: `apex3/tests/test_config.py`

**Interfaces:** `load_config()` remains the sole YAML-to-typed-config entry point. Add typed fields for radar tiers, max new entries per bar, slot-2 profit threshold, signal Gate 9 values, regime VWAP/ADX slope flags, and one-time fee lock. Preserve `raw_dict` for compatibility with dictionary-based tests while making the engine consume typed settings.

Use the exact requested tiers: core `ICICIBANK`, `SBIN`, `AXISBANK`, `BAJFINANCE`, `KOTAKBANK`, `TATASTEEL`, `HINDALCO`, `JSWSTEEL`, `RELIANCE`, `M&M`, `TATAMOTORS`, `LT`, `ADANIPORTS`, `INFY`, `SUNPHARMA`; extended `BEL`, `HAL`, `TRENT`, `DLF`, `CHOLAFIN`, `BANKBARODA`, `PNB`, `VEDL`, `TATAPOWER`, `BAJAJ-AUTO`. Preserve custom symbols as user-managed entries and deduplicate across tiers.

- [ ] Write tests for exact core/extended symbols, extended-radar toggle, custom symbols, order-preserving duplicate removal, and percentage-unit normalization.
- [ ] Run the targeted tests and confirm the current 17/5 radar and old typed defaults fail them.
- [ ] Set the requested 15-symbol core list, 10-symbol extended list, and editable custom list; keep extended enabled by a toggle.
- [ ] Parse every requested risk, regime, signal, stop, fee-lock, schedule, and broker setting into explicit typed fields; reject invalid ranges or inconsistent units during config loading.
- [ ] Run `pytest apex3/tests/test_config.py apex3/tests/test_radar_conviction.py -q` and verify the merged radar has no duplicates and exact requested membership.

### Task 3: Use one fail-closed signal and regime path in the engine

**Files:**
- Modify: `apex3/strategy/signal.py`
- Modify: `apex3/strategy/regime.py`
- Modify: `apex3/config.py` (`SignalConfig`, `RegimeConfig`)
- Modify: `apex3/strategy/engine.py`
- Modify: `apex3/tests/test_signal.py`
- Modify: `apex3/tests/test_regime.py`
- Modify: `apex3/tests/test_radar_conviction.py`

**Interfaces:** Keep `evaluate_signal(...) -> SignalEvaluation` as the engine-facing entry point and make it return the candidate’s 5-bar structural levels, relative-strength/volume inputs, and conviction score on a pass. Route the engine through the same checks exercised by `evaluate_candidate_signal()` so there is one authoritative implementation of the 9 gates.

- [ ] Add failing tests for 4-bps spread boundary, ATR below 0.22%, both stop-width boundaries, ADR exhaustion above 60%, and fail-closed missing ADR/benchmark/session VWAP data.
- [ ] Add failing regime tests for NIFTY close/EMA/slope, session VWAP direction, ADX ≥22, and strictly rising ADX on the candle-list path used by the engine.
- [ ] Run targeted tests and verify the failures exercise missing production behavior rather than fixture errors.
- [ ] Consolidate the duplicated signal paths; compute 20-bar relative strength, 20-bar volume ratio, quote spread, session range/ADR14, 5-bar swing, and structural stop from the latest completed bars only.
- [ ] Require all nine gates, including valid ADR data, before creating a candidate; calculate the specified conviction formula and preserve the score with the signal for ranking.
- [ ] Add session VWAP and prior/current ADX validation to the engine’s actual regime path; insufficient or stale benchmark history returns `NEUTRAL`.
- [ ] Run `pytest apex3/tests/test_signal.py apex3/tests/test_regime.py apex3/tests/test_radar_conviction.py -q` and verify all gates and regime conditions.

### Task 4: Integrate ranking, slot gating, sizing, and daily risk

**Files:**
- Modify: `apex3/strategy/engine.py`
- Modify: `apex3/strategy/sizer.py`
- Modify: `apex3/strategy/risk.py`
- Modify: `apex3/strategy/accounting.py`
- Modify: `apex3/models.py` if a position needs a typed per-position unrealized-R value
- Modify: `apex3/tests/test_sizer.py`, `apex3/tests/test_risk.py`, `apex3/tests/test_radar_conviction.py`, and `apex3/tests/test_paper_mode.py`

**Interfaces:** The engine evaluates every radar symbol for one completed 5-minute timestamp, ranks passing candidates by `abs(RS20) * 100 * volume_ratio / max(spread_bps, 1.0)`, and submits at most one viable candidate. The canonical sizer accepts the selected `BrokerCostProfile`, computes risk/notional/margin quantity limits, and rejects a trade when round-trip costs exceed 18% of expected 2.0R gross profit.

- [ ] Add failing tests that the highest-scoring viable candidate wins, at most one entry is submitted per completed bar, and re-evaluating the same bar cannot duplicate an order.
- [ ] Add failing tests for Slot 2 blocked below +0.50R and allowed at +0.50R, plus daily stop behavior using realized P&L, unrealized P&L, and estimated costs.
- [ ] Add failing sizer tests proving risk cap ₹120, total 4x notional ceiling, broker-reported margin precedence, and the 18% fee guard for all fee profiles.
- [ ] Run targeted tests and verify failures reproduce the current split between unused helper functions and active engine behavior.
- [ ] Integrate radar selection into `StrategyEngine.evaluate_step()` and maintain the last submitted completed-bar timestamp for duplicate prevention.
- [ ] Compute Slot 1 unrealized R from actual entry, original stop distance, side, and latest fresh quote; do not use a missing dictionary key default as a passing value.
- [ ] Use one risk/sizing implementation for engine and backtest. Keep per-position planned risk at or below ₹120 and total exposure within usable equity × 4.0 and available broker margin.
- [ ] Run `pytest apex3/tests/test_sizer.py apex3/tests/test_risk.py apex3/tests/test_radar_conviction.py apex3/tests/test_paper_mode.py -q` and reconcile edge cases.

### Task 5: Apply the one-time fee-protected stop without enabling continuous trailing

**Files:**
- Modify: `apex3/strategy/trail.py`
- Modify: `apex3/strategy/engine.py`
- Modify: `apex3/models.py` if the managed position needs a persisted `fee_lock_applied` flag
- Modify: `apex3/tests/test_trail.py`

**Interfaces:** Add or reuse a one-time stop evaluation taking the managed position, current price, current IST time, configured offset/thresholds, and instrument tick size. The engine evaluates it while `trail_enabled` is false and confirms broker modification before updating local stop state.

- [ ] Add failing tests for +1.65R trigger, 13:45/+0.75R trigger, no trigger just below thresholds, one-time behavior, protective tick rounding, and no stop loosening.
- [ ] Run `pytest apex3/tests/test_trail.py -q` and confirm the engine-path behavior is not currently covered.
- [ ] Integrate the stop step independently of continuous trailing; keep the fixed 2.0R target and existing 1.5x ATR initial structural stop.
- [ ] Update local state only after the broker confirms modification; log the previous stop, new stop, trigger reason, and R multiple.
- [ ] Re-run the targeted trail tests and verify `trail_enabled: false` remains false in the loaded config.

### Task 6: Centralize broker costs and add free API adapters

**Files:**
- Create: `apex3/strategy/charges.py` (date-aware shared fee schedules and calculators)
- Modify: `apex3/strategy/sizer.py`
- Modify: `apex3/config.yaml`, `apex3/config.py`, and `apex3/models.py` (selected broker/profile and secrets)
- Create: `apex3/broker/fyers_adapter.py`
- Create: `apex3/broker/shoonya_adapter.py`
- Create: `apex3/broker/flattrade_adapter.py`
- Modify: `apex3/broker/hybrid_adapter.py`
- Create: `apex3/broker/yfinance_data_provider.py` (historical/replay only)
- Create: `apex3/broker/factory.py`
- Create or extend: `apex3/tests/test_charges.py` and `apex3/tests/test_broker_adapters.py`

**Interfaces:** `BrokerCostProfile` contains the broker ID, per-order brokerage rate/cap, effective-date statutory rates, and API monthly cost. `calculate_round_trip_charges(buy_price, sell_price, quantity, profile, trade_date)` returns itemized charges. Adapters implement `BrokerInterface`; credentials remain environment-only. `zerodha_personal_hybrid` routes execution/margin to Kite Personal and market data to a configured free provider (`fyers`, `shoonya`, `angel_smartapi`, or `yfinance`). Restrict yfinance to historical replay/paper use unless its live freshness and data rights are separately validated; never permit it as a live execution signal source by default.

- [ ] Add failing fee tests for Zerodha (0.03%/₹20 cap), Shoonya (0.03%/₹5 cap), FYERS Standard (0.03%/₹20 cap), and Flattrade (₹0 brokerage), including tax rounding and date-specific rate selection.
- [ ] Add adapter contract tests with fake SDK responses for quotes, historical data, margins, order lifecycle, partial fills, errors, and invalid sessions; tests must not place live orders or require secrets/network.
- [ ] Add a provider-selection test proving yfinance is rejected for live mode while remaining available to historical replay.
- [ ] Run the targeted fee/adapter tests and confirm each fails for missing shared profiles/adapters.
- [ ] Reconcile the repository’s 0.00318% NSE rate with the public 0.00307% rate card and any historical effective dates; include applicable IPFT and broker-specific stamp/transaction rules where published. Store the chosen source/effective date in report assumptions.
- [ ] Implement the shared fee model, profile selection, and adapters under the existing singular `apex3/broker/` package. Keep 4x as the maximum even if an adapter advertises higher leverage.
- [ ] Load API tokens and secrets only from environment variables; keep broker selection in YAML and preserve paper mode as default.
- [ ] Enforce the current broker onboarding, IP-whitelisting, and retail-algo requirements before live API use; fail closed if required broker-specific live settings are absent.
- [ ] Run `pytest apex3/tests/test_charges.py apex3/tests/test_broker_adapters.py apex3/tests/test_hybrid_adapter.py -q` and verify adapter contracts and fee totals.

### Task 7: Run non-overfit optimization, broker comparison, and publish verified metrics

**Files:**
- Modify: `backtest_3months.py`
- Modify: `walk_forward_backtest.py`
- Create: `apex3/tests/test_backtest_reporting.py`
- Modify: `STRATEGY_SPECIFICATION.md`
- Create: `optimized_backtest_report.json` (or the existing project report’s agreed output path)

**Interfaces:** The backtest returns strategy metrics and a separate broker-cost comparison using the same trade list, fills, and risk rules. It reports training and holdout results independently, monthly breakdowns, max drawdown, gross/net win rates, gross profit factor, total charges, charges/gross-profit ratio, and net ROI. Report generation must fail if totals do not reconcile.

- [ ] Add failing tests for metric reconciliation, monthly totals, broker fee ordering (Flattrade ≤ Shoonya ≤ Zerodha/FYERS for identical fills), API monthly costs of ₹0, and explicit incomplete-universe reporting when `TATAMOTORS` data is missing.
- [ ] Add a failing max-drawdown test using a known daily equity curve and assert the ₹ and percent values.
- [ ] Run the targeted reporting tests and confirm they fail on the current script’s limited universe and non-shared fee calculations.
- [ ] Freeze the requested core parameters before replay; reserve the latest 20 sessions as a holdout and tune any permitted sensitivity grid only on earlier sessions. Never choose thresholds by maximizing the reported holdout result.
- [ ] Run `python backtest_3months.py` and retain the emitted command output, trade ledger, input symbols/date range, fee schedule/source, and assumptions.
- [ ] Run `pytest apex3/tests -q` as required by the APEX-3 quant rules; report every failure by name and do not call the result verified if any fail.
- [ ] Compare the optimized candidate against the saved baseline and calculate all requested overall/monthly metrics plus Zerodha/FYERS, Shoonya, and Flattrade net results from identical fills.
- [ ] Update `STRATEGY_SPECIFICATION.md` only with reproducible metrics and assumptions. If the strict benchmark or untouched holdout is not passed, mark the requested changes as research/paper-only and report the shortfall rather than calling them production metrics.

## Research Sources to Recheck at Execution

- [Zerodha API tiers](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/what-are-the-charges-for-kite-apis): Personal is free for order/margin functions but includes no real-time or historical data; Connect supplies data for a monthly fee.
- [Zerodha charges](https://zerodha.com/charges): published retail intraday brokerage and current NSE equity transaction rate; verify effective dates and reconcile with APEX-3’s 0.00318% setting.
- [FYERS API fee FAQ](https://support.fyers.in/portal/en/kb/articles/does-fyers-charge-any-subscription-fees-for-trading-api) and [FYERS pricing](https://fyers.in/pricing): API/data fee claims and current Standard versus Prime brokerage.
- [Shoonya API pricing](https://shoonya.com/algo-trading) and [Shoonya charges](https://shoonya.com/pricing): API fee, per-order cap, and current statutory rates.
- [Flattrade API](https://www.flattrade.in/algotrading/) and [Flattrade charges](https://flattrade.in/charges/): API access, brokerage, statutory charges, and any scheduled rate changes.
- [SEBI retail algo circular](https://www.sebi.gov.in/legal/circulars/feb-2025/safer-participation-of-retail-investors-in-algorithmic-trading_91614.html) and [NSE algo-provider framework](https://www.nseindia.com/static/trade/empanelled-algo-providers-exchange): verify current implementation standards before enabling live adapters.
