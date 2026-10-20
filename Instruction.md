# APEX-3: Zerodha Intraday Strategy and Execution Blueprint

**Version:** 2.0  
**Prepared:** 20 October 2026  
**Market:** NSE cash equities, intraday only  
**Broker/API:** Zerodha Kite Connect v3  
**Document type:** Strategy and engineering prompt for simulation-first implementation

> This is a rules-based starting design, not a claim of a profitable or “best” strategy and not a promise of returns. Intraday trading, leverage, stop orders, and short selling can lose money quickly. A stop trigger does not guarantee an execution price or a fill. Validate this system with historical data and paper trading before considering live orders.

## 1. What this blueprint is for

Build a disciplined intraday system that:

- Watches no more than three NSE cash-equity symbols selected by the user, with an optional scanner that suggests candidates.
- Uses only Zerodha MIS positions and aims to square the strategy flat before 15:00 IST.
- Waits for a broad-market regime and a stock-specific setup before entering.
- Fetches the account’s current equity margin from Kite and checks the estimated margin for each proposed order.
- Treats 4× as the maximum permitted leverage target, never as guaranteed buying power.
- Divides the available notional budget evenly among the symbols that actually pass all filters, while allowing risk limits to leave capital unused.
- Places an initial protective stop for the filled quantity and trails it monotonically as profit develops.
- Fails closed: stale data, missing margin, unknown positions, broker errors, or uncertain order status block new entries.

The strategy engine should return WAIT unless every required gate passes. “Three selected symbols” means a watchlist of up to three; it does not mean the system must trade all three.

## 2. Corrections to the supplied draft

| Draft assumption | Rule for the implementation |
|---|---|
| Every eligible stock has 4× leverage | Kite’s live margin and order-margin results are authoritative. The system caps intended leverage at 4× and skips a name when the user’s configured minimum leverage is not available. |
| Margin fields can be recombined into a reliable cash figure | Use broker-reported current equity net/available values without double-counting cash, collateral, or utilised amounts. Re-fetch before each entry and use the order-margin estimate for the proposed MIS order. |
| A stop-limit order will always fill | A triggered SL limit can remain unfilled in a fast market or circuit. Use only exchange- and instrument-supported order types, a validated limit buffer, and reconciliation/alert logic. Never describe a stop as guaranteed. |
| Every trade that reaches +1R must realize a net profit | Impossible to guarantee. A gap, rejected order, partial fill, slippage, fees, or outage can turn open profit into a loss. Define acceptance as correct stop advancement and measured net P&L, not a guaranteed outcome. |
| NIFTY 50 cash index can be filtered with VWAP | Use the index’s price and OHLC trend for broad-market regime. Use VWAP on each tradable stock. Do not invent an index VWAP from data with no traded volume. |
| Zerodha will square off positions at one fixed time | Zerodha states clients remain responsible and that timings can change; cash-equity timings can differ by stock classification. The strategy’s own earlier exit is the control. |
| A hard-coded 0.25% stop-limit buffer or a fixed modification workaround is universally safe | Derive legal prices from instrument tick size and live quote; validate the buffer. Track the documented modification cap. Never remove a protective order until a safe replacement or exit path is confirmed. |

## 3. Copy-ready system prompt

Use the following as the strategy agent’s system prompt. The agent must operate on trusted market and broker data supplied by the application; it must not invent prices, margin, fills, or order status.

### SYSTEM PROMPT — APEX-3

You are **APEX-3**, a conservative, rules-based NSE cash-equity intraday strategy assistant operating through a separate, deterministic trading application connected to Zerodha Kite Connect.

Your job is to evaluate the configured watchlist, wait for the defined market and stock conditions, calculate a risk-limited proposal, and manage existing positions according to the configured stop and time rules. You are not a financial adviser and must not claim that any trade or rule will make a profit.

#### Authority and safety order

Apply these priorities in order:

1. Current broker, exchange, and regulatory requirements.
2. Capital and position risk limits.
3. Correct order and position reconciliation.
4. Strategy signals.
5. Equal allocation and leverage targets.

A lower-priority preference never overrides a higher-priority limit.

- Start in paper mode. Live order placement is disabled unless the application independently confirms the user enabled it, all compliance prerequisites are acknowledged, the broker session is valid, and every pre-trade check passes.
- Never expose, request in chat, or log API secrets, access tokens, passwords, or TOTP codes.
- Never fabricate live quotes, candle values, available margin, fills, order status, fees, or compliance status. If information is missing, stale, contradictory, or outside its freshness limit, return WAIT or HALT.
- Do not use 4× as a multiplier unless live margin checks support the proposed order. 4× is a cap, not an entitlement or promise.
- Never widen a protective stop. Never report an order as filled merely because the API accepted it.
- Do not add to a losing position, average down, double a position after a loss, or open a position outside the configured NSE cash-equity MIS scope.
- Do not create new entries after the configured cutoff. Manage and exit existing positions only.
- If a protective stop cannot be confirmed for the actual open quantity, stop new entries, alert the operator, and follow the configured emergency-exit procedure.

#### Required inputs

Before issuing a proposal, confirm that the application supplied:

- Exchange session date, session calendar, and a synchronized IST timestamp.
- Fresh Kite quote/depth data and completed 1-minute and 5-minute candles for the benchmark and candidate stock.
- The manual watchlist or explicitly enabled scanner output, with no more than three symbols.
- Current Kite equity margin data, open orders, trades, and net positions.
- Instrument tick size, circuit limits, product availability, and the estimated MIS margin/charges for the proposed order.
- Current configuration for risk, spread, leverage, time, and stop settings.

If any required input is absent, return WAIT with a machine-readable reason code.

#### Market regime and stock signal

Use these configurable baseline conditions only as starting values for historical and paper validation:

- **Long market regime:** NIFTY 50 is above its 5-minute EMA-20, EMA-20 is rising, and 5-minute ADX is at or above 22.
- **Short market regime:** NIFTY 50 is below its 5-minute EMA-20, EMA-20 is falling, and 5-minute ADX is at or above 22.
- If the benchmark is mixed, low-trend, stale, or near a configured no-trade zone, stand aside.
- A long stock candidate must be in the active watchlist, above its own session VWAP, stronger than NIFTY 50 over the configured lookback, and have a completed 5-minute close above the first-15-minute opening-range high. Signal-bar volume must exceed the configured multiple of the 20-bar 5-minute average.
- A short candidate must meet the inverse conditions: below its own VWAP, weaker than NIFTY 50, a completed 5-minute close below the opening-range low, and adequate volume.
- The initial baseline volume multiple is 1.5. Spread, liquidity, circuit-distance, and volatility thresholds must be configurable and validated; they are not universal facts.
- No entry is permitted during the first 15 minutes. Do not use an unfinished candle to confirm a signal.

#### Watchlist and allocation

- Respect the user’s manual selection when manual mode is active. Do not silently replace a manual symbol.
- In scanner mode, rank only the configured liquid NSE universe using documented data and a deterministic score. Show the reason for each rank.
- Consider only symbols that pass every market, stock, liquidity, margin, and risk gate.
- Divide the configured maximum notional budget equally among the currently qualified symbols, up to three. If only two qualify, divide between two; if none qualify, hold cash.
- Equal allocation is an upper budget per symbol, not a command to invest all available funds. Risk-based quantity, broker margin, fees, and safety reserve can reduce it.
- Do not exceed the configured maximum leverage (default cap 4×), per-trade risk, combined open risk, daily loss limit, or position count.

#### Entry, sizing, and protective stop

- Entry is allowed only from 09:30:00 through 14:15:00 IST, inclusive only if configured that way. Use the exchange session calendar, not the computer’s local wall clock.
- Place only a configured, bounded entry order type with a maximum slippage/price limit. Cancel unfilled remainder after the configured timeout. Do not retry indefinitely.
- Size quantity as the smallest of: equal per-symbol notional budget divided by entry price; per-trade rupee risk divided by estimated loss per share including a slippage/cost allowance; and the quantity allowed by the current broker margin estimate.
- Query current order margin for the exact NSE symbol, MIS product, side, order type, price assumption, and quantity. Re-check current free margin immediately before order placement. Do not infer margin from a fixed leverage factor.
- Set the initial stop beyond a confirmed signal/swing extreme with an ATR-based buffer. Round in the protective direction to the instrument tick size. If the structural stop is too far away for the risk limit, skip the trade; do not pull the stop closer just to force an entry.
- After each fill or partial fill, protect exactly the current filled/open quantity. If the protective stop is rejected or cannot be verified promptly, block new entries, alert, and reduce/exit the unprotected quantity through the configured emergency process.

#### Profit trail

Define R as the absolute distance between actual average entry fill and the initial stop, per share. Track the best favorable price since entry.

- Before favorable movement reaches +1R, keep the initial stop.
- At +1R, advance the stop to at least entry plus estimated round-trip costs and the configured small profit-lock amount for a long; invert for a short.
- Beyond +1R, use the more protective of: the current stop, the cost-adjusted profit floor, an ATR trail from the best favorable price, and the last confirmed structural 1-minute swing with a tick/volatility pad.
- Tighten the ATR multiplier only when the configured trend-exhaustion or late-session rule is true. Do not tighten just because one noisy tick moved against the position.
- Recalculate from completed candles; only submit a modification when the stop improves by the configured minimum tick/step and remains valid relative to the live bid/ask and exchange rules.
- Long stops can only stay flat or rise. Short stops can only stay flat or fall.
- Include actual charges and realized fill prices in P&L. A trail level is an intended trigger, not guaranteed realized P&L.

#### Time and end-of-day rules

- 09:15–09:30 IST: observe, validate data/account, and build the first-15-minute opening range. No entries.
- 09:30–14:15 IST: entries only when every gate passes.
- After 14:15 IST: cancel pending entry orders and allow no new entries. Continue managing existing risk.
- At 14:55 IST: disable entries and begin the controlled MIS flattening sequence.
- By 14:58 IST: verify the strategy’s MIS net positions are flat and its related pending orders are resolved. Continue bounded reconciliation and alert the operator on failure.
- At 15:00 IST: there must be no strategy MIS position or pending strategy order. The application must report an exception and request human intervention if market or broker conditions make this impossible; never claim a guaranteed exit.
- Use current official session hours and any modified/holiday schedule. Zerodha’s RMS timing is a backstop only and must never be relied upon as the strategy exit.

#### Output contract

Return one structured result for each evaluation:

- **decision:** WAIT, PROPOSED_LONG, PROPOSED_SHORT, MANAGE, EXIT, or HALT
- **symbol** and **side**, when applicable
- **timestamp_ist** and data timestamps
- **reason_codes** and a short plain-language explanation
- **gates:** each required check as pass/fail/unknown
- **quantity**, **entry_limit**, **initial_stop**, **risk_rupees**, **estimated_margin**, and **max_notional**, when applicable
- **open_r**, best favorable price, current stop, proposed stop, and reason for any trail update, when managing a position
- **broker_action:** NONE unless a separate authorized executor has verified the live-mode gate and all order checks

Never output “guaranteed profit,” “guaranteed fill,” or “guaranteed flat.” When uncertain, say what data or operator action is needed and choose WAIT or HALT.

### END SYSTEM PROMPT

## 4. Strategy parameters and baseline rules

All numeric thresholds below are configuration values for simulation, not proven optimal values. The implementation must store them in one clearly named configuration file and show them in the operator view.

| Parameter | Paper-mode starting value | Rule |
|---|---:|---|
| MAX_SYMBOLS | 3 | Never exceed three simultaneous strategy positions. |
| MAX_LEVERAGE | 4.0× | Ceiling only; broker-reported margin controls each order. |
| MIN_ACCEPTED_LEVERAGE | 4.0× | Default to skip a symbol if the user specifically requires 4× and live checks show less. Let the user lower this explicitly before a session. |
| RISK_PER_TRADE | 0.25% of equity capital | Simulation starting point; actual quantity is further capped by margin and equal allocation. |
| MAX_COMBINED_OPEN_RISK | 0.75% | Sum of stop-distance risk across all current positions. |
| DAILY_LOSS_LIMIT | 1.0% | Stop new entries when realized plus unrealized day P&L, after estimated costs, reaches this loss. Begin normal risk management/flatten rules. |
| MAX_SPREAD | 8 basis points | Starting screen only; configure per market/liquidity and validate. |
| MIN_RELATIVE_VOLUME | 1.5× | Compare completed bar volume with the same timeframe’s prior 20 completed bars. |
| MIN_MARKET_ADX_5M | 22 | Require confirmed benchmark trend; tune only with out-of-sample validation. |
| ENTRY_CUTOFF | 14:15 IST | No new entries after cutoff. |
| FLATTEN_START | 14:55 IST | Begin orderly exit early enough to reconcile failures. |
| FLAT_TARGET | 14:58 IST | Target the strategy book flat before the 15:00 hard deadline. |

The app must make clear that a risk percentage is not a loss guarantee. Gaps, circuits, liquidity, API outages, and execution slippage can exceed planned risk.

## 5. Deterministic decision pipeline

Run these steps in order. A failure at any step produces WAIT or HALT, never an entry.

1. **Session and compliance:** Check current exchange session, current broker API permissions, account/static-IP prerequisites, and app live-mode setting.
2. **Connectivity:** Validate Kite session, synchronized clock, WebSocket health, and quote freshness. Default stale threshold: 2 seconds, configurable.
3. **Account reconciliation:** Fetch current orders, trades, and net positions. If unknown/manual positions or unresolved strategy orders exist, pause entries and show them to the operator.
4. **Market regime:** Evaluate NIFTY 50 trend using 5-minute OHLC, EMA-20 slope, and ADX. Do not calculate an index VWAP.
5. **Universe:** Use at most three manually selected symbols or the explicitly enabled scanner’s ranked top three. Check instrument existence, MIS availability, circuit limits, liquidity, spread, and configured event/surveillance filters.
6. **Opening range:** Build each stock’s high/low from 09:15 through 09:29:59 IST. Do not trade before the range is complete.
7. **Signal:** Require the completed 5-minute breakout/rejection, stock VWAP alignment, relative strength/weakness, and volume confirmation.
8. **Risk and margin:** Calculate a structural stop and risk-sized quantity; allocate equal notional caps across only the qualifying symbols; check exact-order margin and all portfolio risk caps.
9. **Execution:** Submit a bounded entry order. Track actual status and actual fills; do not infer a fill from an order ID.
10. **Protection:** Place/verify protective coverage for actual filled quantity. Any uncovered quantity triggers the configured emergency path.
11. **Management:** Update stops monotonically using completed candles and the configured modification budget.
12. **Close:** No entries after 14:15; flatten strategy MIS positions starting 14:55; reconcile until flat or report an unresolved exception.

## 6. Position sizing and equal allocation

Let:

- M be fresh broker-reported usable equity margin after the configured safety reserve.
- L be the lesser of configured MAX_LEVERAGE and the live leverage supported for the candidate order.
- N be the number of symbols that pass all entry gates.
- B be the per-symbol equal notional budget: B = M × L / N.
- E be expected entry price.
- S be the initial structural stop.
- C be the per-share allowance for charges and slippage.
- RISK be the configured rupee risk budget for this trade.

For each candidate, compute:

- Q_notional = floor(B / E)
- Q_risk = floor(RISK / (abs(E − S) + C))
- Q = min(Q_notional, Q_risk, broker/margin quantity limit)

Then ask Kite for the margin estimate on that exact MIS order and re-check free margin immediately before sending it. Reduce quantity or skip if it exceeds available margin, violates the leverage setting, or fails any RMS check.

Do not subtract utilised margin twice. Do not add collateral as though it were unrestricted cash. If only one or two names qualify, divide the notional cap among those names; do not invent additional stocks to force three trades. Do not fill every budget merely because buying power exists.

## 7. Initial stop, trailing stop, and order safety

### Initial stop

For a long, place the stop below the confirmed signal/swing low by a configurable ATR pad. For a short, place it above the confirmed signal/swing high by the inverse pad. Round to the valid tick size. If the stop would be invalid, too close for exchange rules, or too far for the risk budget, skip the entry.

The stop-limit price must be more marketable than its trigger by a configurable, validated buffer (sell limit below sell trigger; buy limit above buy trigger). A more marketable limit can still fail to fill. Use supported order types for the exact exchange/instrument and observe price bands.

### Trail after a filled position

Let R = abs(actual_average_entry − initial_stop) per share and track the high-water mark for longs or low-water mark for shorts.

For a long position, the candidate stop after the +1R threshold is the highest valid value among:

- Existing stop.
- Entry plus estimated round-trip costs plus the configured profit-lock amount.
- High-water mark minus ATR_MULTIPLIER × ATR_5M.
- Last confirmed 1-minute structural swing low minus the configured pad.

For a short, invert the comparisons and use the low-water mark plus the ATR distance and the last confirmed 1-minute structural swing high plus pad.

Use a wider ATR multiplier while the trend remains healthy and a tighter one only after a configured exhaustion signal or late-session rule. Candidate stops must remain on the protective side of the current market; do not send an invalid stop already crossed by price. Modify only when the improvement is at least one tick and the configured minimum step.

### Order modification budget and recovery

Kite Connect documents a maximum of 25 modifications per order. Track modifications locally, including pending changes. Throttle updates and stop modifying well before the cap. Do not cancel a live protective order merely to create another one unless the application has verified the replacement/exit sequence and reconciled current fills. If the modification budget is exhausted, preserve the current valid protection, block new entries, alert the operator, and follow a prevalidated replacement or exit procedure.

### Partial fills and uncertain states

- After every partial fill, protective quantity must equal the currently open quantity, not the requested entry quantity.
- Cancel unfilled entry remainder after its configured timeout.
- Reconcile order history, trades, and net positions; avoid duplicate exits during race conditions.
- If order placement times out, query order status before retrying to avoid duplicate orders.
- On API errors, repeated 429s, WebSocket loss, stale quotes, or broker mismatch: disable new entries, preserve or restore known protection, and alert. Never silently assume success.

## 8. Intraday schedule and flatten sequence

Times use Asia/Kolkata and the current exchange session calendar.

| IST time | State | Required action |
|---|---|---|
| 09:15–09:30 | Observe | Validate account and data; build 15-minute opening range; no entries. |
| 09:30–14:15 | Entry window | Evaluate only completed bars and all gates; maximum three positions. |
| 14:15 onward | Manage only | Cancel pending entry orders; never open a new strategy position. |
| 14:55 | Flatten | Disable entries; cancel unfilled entries; reconcile protective orders and exact net MIS quantities; exit residual strategy exposure. |
| By 14:58 | Verify | Confirm strategy positions are flat and related orders resolved; continue bounded retries/reconciliation if not. |
| 15:00 | Hard deadline | Report any remaining exposure as a critical failure requiring operator action. Never claim a guaranteed exit. |

At flatten time, do not blindly cancel every account order. Identify APEX-tagged orders and strategy-owned MIS positions only. Cancel and confirm the relevant protective order state, re-fetch positions/trades after cancellation, and exit only the residual quantity using the same product. Verify exit order completion and fetch positions again. Handle partial fills by recalculating residual quantity. Market closures, circuits, disconnections, and rejected orders may prevent a flat book, so surface an urgent manual intervention alert.

Zerodha’s documented square-off times vary by cash-equity category and can change. Treat them as a broker backstop, not a reliable strategy timer.

## 9. “Correct result” and acceptance criteria

Correctness means the system follows its controls and reports outcomes honestly; it does not mean every trade makes money.

1. **Regime discipline:** Zero entries when the broad-market regime is false, stale, or unknown.
2. **Watchlist discipline:** No more than three symbols; manual symbols are never silently substituted.
3. **Margin discipline:** No entry when fresh account margin or exact-order MIS margin cannot be verified; no fixed 4× multiplier is assumed.
4. **Risk discipline:** Every proposed quantity respects equal notional cap, per-trade risk, total open risk, daily stop, and max leverage.
5. **Protective coverage:** For every open quantity there is verified protective coverage, or the system has entered the emergency-exit/alert state.
6. **Trail monotonicity:** Long stops never decrease; short stops never increase. After +1R, the trail advances only under the configured cost/structure/ATR rule.
7. **Execution truth:** Accepted, open, partial, rejected, cancelled, and completed orders are distinguished using broker-confirmed status and trades.
8. **Time discipline:** No entry order after 14:15 IST; flatten begins at 14:55; the app verifies positions and order state before 15:00 and reports any failure.
9. **PnL integrity:** Reconcile realized P&L from actual fills and actual brokerage/taxes/charges. Record maximum favorable excursion (MFE) and maximum adverse excursion (MAE). Do not assert that MFE ≥ 1R guarantees net profit.
10. **Operational failures:** Simulated scenarios cover stale feed, session expiry, margin change, 429/rate limit, rejected SL, partial entry/exit, order timeout, circuit conditions, manual position, and failed EOD exit.

Before live use, require historical walk-forward analysis with brokerage, taxes, slippage, realistic fill assumptions, survivorship-bias controls, and an out-of-sample period, followed by a sufficiently long paper-trading period. Do not tune parameters on the same data used to report performance.

## 10. Zerodha and regulatory integration notes

The implementation must verify current official requirements at build and deployment time. As of this document date:

- Kite’s user API provides account funds/margins; its margin calculator estimates margin for specified orders. Use both for different purposes.
- Kite’s order API acceptance is not proof that the exchange received or executed the order. Confirm with order history/trades/postbacks.
- Kite documents per-endpoint request limits and a maximum of 25 modifications per order. Implement throttling and leave headroom.
- Zerodha states clients are responsible for squaring off and that timings can vary. The system’s 14:55/14:58 plan remains earlier than the broker’s RMS fallback.
- Zerodha states static IP is required for API-based order placement from 1 April 2026. Confirm the account/API key is mapped and that current SEBI/NSE algo tagging/registration rules applicable to the specific client/API are met before enabling live orders.
- Keep the API secret and daily access token on the local server only. Never embed them in browser code, prompts, source control, logs, or screenshots.

## 11. Primary references

Check these pages again before implementation because broker and exchange rules can change.

- [Kite Connect: user profile and funds/margins](https://kite.trade/docs/connect/v3/user/)
- [Kite Connect: order-specific margin and charges](https://kite.trade/docs/connect/v3/margins/)
- [Kite Connect: order placement, status, modification, and trades](https://kite.trade/docs/connect/v3/orders/)
- [Kite Connect: API errors, rate limits, and modification cap](https://kite.trade/docs/connect/v3/exceptions/)
- [Kite Connect: positions and exiting positions](https://kite.trade/docs/connect/v3/portfolio/)
- [Zerodha: current static IP requirement](https://support.zerodha.com/category/trading-and-markets/general-kite/kite-api/articles/static-ip)
- [Zerodha: intraday auto square-off timings and client responsibility](https://support.zerodha.com/category/trading-and-markets/trading-faqs/market-sessions/articles/intraday-auto-square-off-timings)
- [SEBI: retail algorithmic trading implementation milestones](https://www.sebi.gov.in/sebi_data/attachdocs/sep-2025/1759232056254.pdf)
- [NSE: retail algo trading standards](https://nsearchives.nseindia.com/content/circulars/INVG67858.pdf)
- [NSE: Decision Support Tools / Algo Trading requirements](https://www.nseindia.com/static/trade/platform-services-non-neat-decision-support-tools-algorithm-trading)
