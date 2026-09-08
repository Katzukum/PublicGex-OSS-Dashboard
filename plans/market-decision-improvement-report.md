# PublicGex Market-Decision Improvement Report

**Date:** 2026-08-14  
**Code reviewed at:** `7ee8f2e`  
**Scope:** Product direction, decision quality, market-context coverage, trade-setup quality, empirical validation, and the technical surfaces that support them.

## Executive recommendation

PublicGex should become a **decision-support loop**, not a larger collection of market gauges:

1. **Observe:** What regime is the market in, and what changed?
2. **Frame:** What are the bullish, bearish, pin, and no-trade scenarios?
3. **Validate:** Is the setup supported by independent historical evidence and executable quotes?
4. **Act:** What is the trigger, target zone, invalidation, time horizon, and defined risk?
5. **Learn:** Did the scenario work, what was the actual fill/slippage, and in which regimes does it repeat?

The app already has most of the structural pieces: a cockpit, gamma profile and sweep, TRACE history, targets and invalidations, defined-risk strategy cards, outcome labeling, and NinjaTrader broadcasting. The highest-value work is therefore not “add more indicators.” It is to make the existing signal trustworthy, execution-aware, and aware of conditions outside the option chain.

The first three moves should be:

1. Fix the semantics and sampling of **Confidence** and **Empirical Edge**.
2. Replace theoretical setup prices with **live strategy bid/mid/ask and liquidity checks**.
3. Add a compact **volatility/event context layer** so GEX is not treated as the whole market.

## What is already strong

- The cockpit combines broad-market, selected-symbol dealer, and liquidity inputs and can deliberately return `WAIT` (`web/main.js:2149-2187`).
- Targets, invalidations, flip levels, and a focused GEX profile are already presented together (`web/index.html:99-200`).
- TRACE records how the strike map evolves through time instead of showing only the latest static profile (`appy.py:736-904`, `web/main.js:1243-1590`).
- The system persists signal events and labels 15-, 30-, and 60-minute outcomes, including target-first, invalidation-first, MFE, and MAE (`models.py:91-153`, `signal_performance.py:219-314`).
- Trade setups are defined-risk butterflies and debit spreads, not uncovered short-option suggestions (`appy.py:1127-1256`).
- Apache ECharts is already the active charting foundation, so the recommended additions can reuse existing chart primitives and interaction patterns.
- Verification is healthy: 41 Python tests and 6 JavaScript tests passed during this review; all active JavaScript entry points passed syntax checks.

## Research conclusion

The external research supports the app's central thesis, but also argues for tighter boundaries around it.

- A 2025 research paper hosted by Cboe finds a negative relationship between option market-maker net gamma and minute-level S&P 500 return variance. That supports using gamma as a **conditional volatility/regime input**, especially over short horizons. [Cboe research paper](https://cdn.cboe.com/resources/education/research_publications/gammasqueezes.pdf)
- Cboe's own market-impact analysis also reports that balanced customer activity can make net 0DTE market-maker hedging small relative to SPX liquidity. Gamma should therefore be treated as one market force, not a guaranteed directional driver. [Cboe 0DTE positioning and market-impact analysis](https://www.cboe.com/insights/posts/0-dt-es-decoded-positioning-trends-and-market-impact)
- The app's current open-interest sign convention does not observe actual dealer inventory. The project's thesis document already acknowledges that open interest is a proxy, option trade direction is unknown, and macro/news flows can overwhelm the gamma map (`docs/dashboard_data_thesis.md:648-706`).
- Public's current option-chain API already exposes bid, ask, sizes, volume, open interest, mid price, Greeks, and implied volatility; a separate strategy-quote endpoint returns multi-leg pricing. The app can improve setup realism without changing data vendors. [Public option-chain API](https://public.com/api/docs/resources/market-data/get-option-chain), [Public strategy-quote API](https://public.com/api/docs/resources/option-details/get-strategy-quote)
- FINRA emphasizes that 0DTE options can lose value rapidly and that broker liquidation near the close can alter outcomes. Time-to-close, settlement type, and defined max loss belong directly in the setup workflow. [FINRA 0DTE overview](https://www.finra.org/investors/insights/zeroing-in-options-trading-strategy)
- Cboe publishes multiple volatility horizons because one horizon does not describe all market conditions. VIX1D/short-term expectations and the VIX term structure are useful context beside a 0DTE gamma map. [Cboe VIX term structure](https://www.cboe.com/tradable-products/vix/term-structure), [VIX1D methodology](https://cdn.cboe.com/api/global/us_indices/governance/Volatility_Index_Methodology_Cboe_1-Day_Volatility_Index.pdf)

The right positioning is: **GEX describes the terrain; price, volatility, events, and execution decide whether a trade is actually attractive.**

## Vetted findings

| # | Finding | Category | Impact | Effort | Fix risk | Confidence | Evidence |
|---|---|---|---|---|---|---|---|
| 1 | Count independent opportunities, not every repeated broadcast, in Empirical Edge | Correctness / product trust | Very high | M | Medium | High | `signal_performance.py:155-189`, `signal_performance.py:437-468`; local DB has 21,387 signal rows per symbol over 30 days but only 27 SPX and 29 NDX setup keys |
| 2 | Separate data quality from predictive probability | Product trust | High | S for relabel; L for calibration | Low | High | `appy.py:232-272` computes freshness/profile quality, while `web/index.html:109-111` labels it only “Confidence” |
| 3 | Rename “Whale Flow” because the app does not observe whale flow | Correctness / UX | High | S | Low | High | `web/index.html:152-158`; `appy.py:1418-1427` builds a weighted index GEX basket; `docs/dashboard_data_thesis.md:686-690` calls the baskets interpretations |
| 4 | Use executable option quotes for setup economics | Correctness / trading | Very high | M | Medium | High | `appy.py:1294-1310` explicitly uses a Greek-implied theoretical mid; stored option rows omit bid/ask/IV/volume (`models.py:64-82`) |
| 5 | Add non-GEX context before strengthening directional language | Direction / model risk | High | M-L | Medium | High | Cockpit inputs are all derived from the same GEX family (`web/main.js:2149-2161`); the project documents macro/news/flow limitations (`docs/dashboard_data_thesis.md:646-706`) |
| 6 | Replace static percentage fallbacks with volatility- and time-aware zones | Direction / risk | Medium-high | M | Medium | High | Fixed symbol sensitivities at `appy.py:93-120`; fallback targets and invalidations use 0.6%/1.2% constants at `web/main.js:2744-2772` |

### Why finding 1 is urgent

The current outcome table contains 42,728 labeled rows at each of the 15-, 30-, and 60-minute horizons. Signals average about 713 rows per symbol per trading day. Those observations overlap heavily: consecutive signals often share most of the same future price path. The UI can therefore display a large `n` even when the evidence comes from only a few trading days or one sustained regime.

The result is not necessarily a wrong win rate, but it is an overconfident presentation of evidence. The NBER warns that combining and selecting among multiple trading signals creates severe backtest-overfitting risk; repeated overlapping observations add another dependence problem. [NBER: Backtesting Strategies Based on Multiple Signals](https://www.nber.org/papers/w21329)

## Prioritized product direction

### 1. Build a trustworthy Edge Lab

**What the user should see**

- `Data quality: 82%` and `Historical edge: 56% target-first` as two separate concepts.
- `18 independent days / 43 non-overlapping setups`, not `n=500` repeated snapshots.
- Win-rate interval, median net expectancy after slippage, target-first rate, invalidation-first rate, MFE, MAE, and time-to-target.
- Exact setup results when sufficient; otherwise a visibly labeled fallback such as symbol+bias or regime+bias.
- A calibration chart: when the app said 60-70%, did the event occur approximately 60-70% of the time?

**Implementation shape**

- Emit a new opportunity only on a meaningful state transition, after a cooldown, or on a non-overlapping fixed interval. Do not store the same setup on every broadcast.
- Use one trading day as the primary clustering unit. Report unique days and unique opportunities beside raw rows.
- Add walk-forward evaluation: train/tune only on prior days and score later days. Keep a final untouched holdout period.
- Deduct an explicit fill model: strategy ask for entry, bid for exit as a conservative baseline, plus fees.
- Add bootstrap confidence intervals by trading day and display “insufficient evidence” until minimum day/opportunity thresholds are reached.

**Success measure:** users can distinguish data freshness from predictive edge, and no statistic implies hundreds of independent trades when only a few days were observed.

**Effort:** L for the full Edge Lab; M for correcting sampling and labels first.

### 2. Make setup cards execution-aware

**What the user should see**

- Strategy bid / mid / ask, quote timestamp, and spread width.
- `Executable debit`, max loss, max reward, reward/risk, breakeven, fees, and slippage assumption.
- Leg liquidity: bid size, ask size, volume, open interest, and IV.
- Liquidity grade with a hard “No setup” state when quotes are stale, crossed, missing, or too wide.
- Time remaining, settlement type, and a late-day risk warning.
- Optional account-risk input that calculates contracts from maximum acceptable loss. Keep this local and do not place orders automatically.

**Implementation shape**

- Extend `RawOptionGreek` or add an immutable `OptionQuote` snapshot with bid, ask, sizes, last, volume, IV, and source timestamps.
- Query Public's strategy-quote endpoint for the proposed butterfly/spread immediately before presenting economics.
- Store both modeled and quoted values so the backtest can measure model error and actual spread cost.
- Add a quote-validity policy per symbol and time of day.

**Success measure:** every displayed debit is either a timestamped executable quote or clearly marked unavailable; backtests include realistic transaction costs.

**Effort:** M.

### 3. Add a Market Conditions Ribbon

Place a single compact ribbon above the cockpit rather than adding another full navigation page.

**Suggested cells**

- **Event risk:** next CPI, jobs report, FOMC statement/press conference, minutes to event.
- **Implied volatility:** VIX1D or a locally computed remaining-day implied move from the SPX ATM straddle.
- **Volatility curve:** VIX1D/VIX9D/VIX or front VIX term-structure slope.
- **Move budget:** percent of the implied daily move already consumed; remaining expected range.
- **Realized state:** opening-range width, short-horizon realized volatility, and trend efficiency.
- **Breadth/risk appetite:** simple equal-weight vs cap-weight participation, advance/decline input if available, and IWM/QQQ confirmation.

The event calendar can use the official [Federal Reserve FOMC calendar](https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm) and [BLS release calendar/ICS feed](https://www.bls.gov/schedule/news_release/cpi.htm). Cache calendar data locally and attach event tags to every signal/outcome so the Edge Lab can answer, for example, “Does this setup work on CPI days?”

**Decision rule:** an event or volatility override should reduce confidence or force `WAIT`; it should not mechanically reverse a GEX signal.

**Success measure:** the user can explain whether today's market is normal, event-driven, volatility-rich, or range-exhausted before considering a trade.

**Effort:** M for event clock + implied move; L for full breadth/term-structure coverage.

### 4. Replace one-point calls with scenario cards

The current cockpit outputs one bias, one target, and one invalidation. A stronger decision surface would show mutually exclusive scenarios:

| Scenario | Required fields |
|---|---|
| Pin / mean reversion | Trigger zone, target zone, invalidation, maximum time, evidence |
| Upside expansion | Break/reclaim trigger, next liquidity zone, invalidation, remaining move budget |
| Downside expansion | Break/fail trigger, next liquidity zone, invalidation, remaining move budget |
| No trade | Exact reason: stale data, event window, poor liquidity, weak edge, or mixed pillars |

Use zones instead of false-precision point estimates. Zone width can be based on strike spacing, remaining implied move, recent realized volatility, and quote liquidity. Show “what would change my mind” beside each scenario.

**Success measure:** a user knows what must happen before entry and can recognize when the original thesis has failed.

**Effort:** M.

### 5. Alert only on meaningful state transitions

The existing event bridge, activity feed, and NinjaTrader broadcaster make this an adjacent feature, not a new platform.

Add deduplicated alerts for:

- Spot crossing and holding/rejecting the flip.
- Net GEX sign change or rapid local-slope change.
- Call/put wall approach, breach, and reclaim.
- Cockpit transition `WAIT -> CALL/PUT`, or an active setup losing validity.
- Data-quality drop, stale quote, or collector failure.
- 50%, 80%, or 100% of implied move consumed.
- Scheduled event windows.

Every alert should include symbol, timestamp, old state, new state, trigger level, current scenario, and cooldown. Avoid alerts for every refresh.

**Success measure:** alerts describe a decision change, not merely new data.

**Effort:** M.

### 6. Turn TRACE into a replay and review tool

TRACE already has the time/strike history and an interactive scrubber. Add a historical date picker and overlay the cockpit state, alerts, target/invalidation zones, and eventual outcomes. A lightweight journal can record:

- planned scenario and trigger;
- actual entry/exit and option fill;
- risk amount and reason for exit;
- screenshot/note;
- slippage, MFE, MAE, and rule adherence.

Weekly review should group results by regime, time of day, event tag, setup type, and liquidity grade. Keep model performance and trader execution performance separate.

SpotGamma's current TRACE guide is a useful interaction benchmark: historical dates, a timeline slider, multiple lenses, prior-time markers, and gamma/delta/charm views. These are design references, not proof of trading efficacy. [SpotGamma TRACE guide](https://spotgamma.com/wp-content/uploads/2026/03/TRACE-User-Guide_30-March-2026.pdf)

**Success measure:** the app helps the user improve process, not just consume another live signal.

**Effort:** M for replay overlays; L with journal/import analytics.

### 7. Add Delta and Charm lenses after the trust foundations

The repository already contains modeled charm calculations for backtesting (`backtest_gamma_butterflies.py:268-343`), while TRACE currently offers only net/call/put GEX modes (`web/index.html:399-403`). Reusing the existing ECharts heatmap for modeled delta-pressure and charm-pressure lenses is a natural extension.

Hard boundary: label them **modeled pressure**, not observed order flow. The current dataset does not identify customer vs dealer side or trade aggressor. Actual participant-specific flow would require a different feed and validation methodology.

Also rename the existing “Whale Flow” tile to **Index Gamma Basket** immediately. “Flow” should be reserved for actual transaction-derived information.

**Success measure:** users can distinguish position-based gamma, price-change delta pressure, and time-decay charm pressure without believing the app observes dealer books.

**Effort:** M for modeled lenses; L or external-data cost for true flow.

## Recommended 90-day sequence

### Weeks 1-2: product-trust patch

- Rename `Confidence` to `Data Quality`.
- Rename `Whale Flow` to `Index Gamma Basket`.
- Mark all setup debit/risk numbers `Modeled` until live quotes arrive.
- Change Empirical Edge to show unique days and non-overlapping opportunities.
- Add an “insufficient evidence” threshold.

### Weeks 3-5: executable setups

- Store Public bid/ask/sizes/volume/IV timestamps.
- Use the strategy-quote endpoint for butterflies and spreads.
- Add liquidity gates, fees, settlement/time-to-close warnings, and local risk sizing.
- Backfill no data; do not synthesize historical bid/ask.

### Weeks 6-8: conditions and scenarios

- Add the event clock and remaining-day implied move.
- Replace exact fallback targets with volatility-aware target/invalidation zones.
- Add scenario cards and explicit no-trade reasons.
- Tag signals/outcomes with event, volatility, and time-of-day context.

### Weeks 9-12: learning loop

- Add state-transition alerts with cooldowns.
- Add TRACE historical date replay and signal overlays.
- Add day-clustered walk-forward statistics and calibration views.
- Begin the manual trade journal; postpone account-history import until the manual workflow is proven.

## Success metrics

Track product quality rather than raw signal count:

- Percentage of setup cards backed by fresh executable strategy quotes.
- Percentage of active setups with explicit trigger, target zone, invalidation, horizon, and no-trade reason.
- Independent opportunity count and unique trading days per performance bucket.
- Calibration error and confidence-interval width, not only win rate.
- Median expectancy after conservative fills and fees.
- Alert-to-action ratio and duplicate-alert rate.
- User rule-adherence rate and realized slippage from the journal.

## What not to build yet

- **Automatic order execution.** First prove quotes, risk controls, sampling, and replay. The app's current educational/context positioning is appropriate.
- **An AI trade narrator.** It would mostly restate uncalibrated inputs. Add it only after every sentence can cite a scenario, data timestamp, and empirical bucket.
- **More strategy cards.** Two well-priced, well-tested defined-risk structures are more useful than ten theoretical ones.
- **A stronger “dealer certainty” score.** The data does not observe dealer inventory or participant side.
- **A single magic composite.** Preserve the separate pillars and show disagreement; combining more signals increases overfitting risk.

## Verification and audit boundaries

Verification run during this review:

- `python -m pytest -q -p no:cacheprovider` with bytecode disabled: **41 passed**.
- Active JavaScript syntax checks: **passed**.
- `node --test test_web_request_coordinator.js test_web_trace_timeline.js`: **6 passed**.
- Local SQLite analysis was opened in read-only mode.

Not audited in depth:

- Broker-order execution or live-account behavior; no orders were placed.
- The numerical accuracy of Public's upstream Greeks/quotes.
- NinjaTrader rendering and end-to-end live socket behavior.
- A full security or dependency audit.
- Statistical significance of the existing strategy backtests beyond the sampling issues documented above.

No source code was modified. This report is the only repository change.
