# PublicGex Market-Decision Implementation Plan

**Plan date:** 2026-08-14  
**Target baseline:** `7ee8f2e`  
**Source report:** `plans/market-decision-improvement-report.md`  
**Structured tasks:** `plans/market_decision_tasks.json`  
**Tracking dashboard:** `plans/market_decision_plan.html`

## Summary

Turn PublicGex from a GEX-heavy monitoring dashboard into a trustworthy decision-support workspace that answers five questions in order:

1. What market condition is active?
2. What scenarios are valid, and what would trigger each one?
3. Is there independent historical evidence for the scenario?
4. Is a defined-risk option structure executable at a fresh market quote?
5. What happened afterward, and what can be learned from it?

This plan implements every direction in the research report while deliberately simplifying the navigation. The old **Gamma** tab is removed. Its profile is already present in Cockpit; its unique Net Gamma Trend moves to TRACE. The old **Setups** tab is repurposed as **Edge Lab**. Its butterfly and debit-spread cards move into a new Cockpit **Scenario & Execution** section so the proposed trade is adjacent to the evidence, trigger, target zone, invalidation, and market context that justify it.

No automatic order placement is in scope. Public API access is used only for market data and strategy quotes.

## Target outcome

The finished navigation is:

| Order | View | Responsibility |
|---|---|---|
| 1 | Cockpit | Current conditions, data quality, scenarios, execution candidates, risk, and concise evidence |
| 2 | Edge Lab | Independent opportunity statistics, calibration, expectancy, filters, and review summaries |
| 3 | Regime | Traders Basket and Index Gamma Basket compasses, component quality, and cross-asset agreement |
| 4 | TRACE | Intraday/replay heatmap, Net/Call/Put GEX, modeled Delta/Charm pressure, overlays, and Net Gamma Trend |
| 5 | Strike Matrix | Contract/strike inspection |
| 6 | One-Off | Ad hoc symbol/expiration profile generation |
| 7 | Settings | Data collection, quote, context, alert, and risk-display preferences |

The UI must never use `flow`, `whale`, or a probability-like percentage for information the system does not actually observe or calibrate.

## Decided product semantics

### Data Quality

`Data Quality` is the current freshness/profile/flip-quality score produced by `calculate_component_confidence()` in `appy.py`. Rename the function to `calculate_component_data_quality()` and expose:

- `data_quality_score` from 0 to 1;
- `data_quality_label` (`LOW`, `MEDIUM`, `HIGH`);
- `data_quality_warnings`;
- `snapshot_age_seconds`.

Data Quality is not a win probability. It can make a scenario ineligible when the score is below `0.45`, a snapshot is stale, the flip is missing, or required quotes are invalid.

### Historical Edge

`Historical Edge` comes only from non-overlapping decision opportunities. It exposes target-first rate, invalidation-first rate, after-cost expectancy, MFE, MAE, unique trading days, independent opportunity count, and a day-clustered confidence interval.

Default evidence gates:

- `INSUFFICIENT`: fewer than 20 independent opportunities or fewer than 10 unique trading days;
- `EMERGING`: at least 20 opportunities and 10 days;
- `CALIBRATED`: at least 50 opportunities, 20 days, an out-of-sample holdout, and a calibration/expectancy gate that passes.

Historical Edge remains informational and must not change the live directional score until it reaches `CALIBRATED` and its after-cost expectancy is positive on the holdout set.

### Index Gamma Basket

Rename every user-facing `Whale`, `Whale Flow`, and `Whale Market` label to `Index Gamma Basket` or `Index Basket`. Internal compatibility fields may survive one release, but all new payloads use `index_basket` naming. This is a weighted SPX/NDX/IWM GEX interpretation, not observed participant flow.

### Modeled pressure

TRACE Delta and Charm values are labeled `Modeled Delta Pressure` and `Modeled Charm Pressure`. They must never be described as customer flow, dealer inventory, or trade aggressor data.

## Architecture

### Decision workspace contract

Add a single Eel endpoint, `get_decision_workspace(symbol)`, in `appy.py`. It becomes the Cockpit's primary load contract and returns one snapshot-consistent payload:

```json
{
  "schema_version": 2,
  "symbol": "SPX",
  "as_of": "2026-08-14T13:45:00-04:00",
  "data_quality": {
    "score": 0.82,
    "label": "HIGH",
    "warnings": [],
    "snapshot_age_seconds": 18
  },
  "market_context": {
    "event_risk": "NORMAL",
    "next_event": null,
    "implied_move_points": 42.5,
    "implied_move_pct": 0.0066,
    "range_consumed_pct": 0.41,
    "realized_vol_15m": 0.0018,
    "cross_asset_score": 0.34,
    "warnings": []
  },
  "regime": {},
  "scenarios": [],
  "active_scenario_id": null,
  "execution_candidates": [],
  "historical_edge": {
    "status": "INSUFFICIENT",
    "independent_opportunities": 8,
    "unique_days": 4
  },
  "eligibility": {
    "can_trade": false,
    "reasons": ["insufficient historical evidence"]
  }
}
```

Build the payload from one selected GEX snapshot ID. Any dependent query must use that snapshot or its timestamp boundary. The frontend discards a response if its symbol/generation is stale, following the existing request-coordinator pattern.

Keep `get_dashboard_data()`, `get_market_overview()`, and `get_trade_setups()` temporarily for One-Off, NinjaTrader, and migration tests. Remove `get_trade_setups()` after the Cockpit and broadcaster consume `get_decision_workspace()` and rename its backend builder to `build_execution_candidates()`.

### New backend modules

| File | Responsibility |
|---|---|
| `schema_migrations.py` | Additive, idempotent SQLite migrations using `PRAGMA user_version`; never require resetting the 6 GB database |
| `option_math.py` | Shared delta/gamma/charm and pricing helpers extracted from `gamma_sweep.py` and `backtest_gamma_butterflies.py` |
| `execution_quotes.py` | Public SDK strategy-quote adapter, quote validation, liquidity grades, conservative fill economics, risk sizing |
| `market_context.py` | Economic-calendar cache/providers, ATM-straddle implied move, realized range/volatility, cross-asset context |
| `scenario_engine.py` | Pure, testable scenario generation and eligibility rules |
| `decision_alerts.py` | State-transition detection, persisted deduplication, cooldowns, Eel/NinjaTrader alert payloads |

Keep these modules independent of Eel and DOM concerns. `appy.py` orchestrates them and exposes serialized dictionaries.

### Frontend organization

Keep `web/main.js` as the Eel orchestration entry point but move pure/testable feature logic into browser/CommonJS dual-use modules matching the style of `web/request_coordinator.js`:

| File | Responsibility |
|---|---|
| `web/decision_workspace.js` | Scenario/eligibility/quote presentation helpers and local risk sizing |
| `web/edge_lab.js` | Edge filters, summary formatting, calibration-series shaping |
| `web/trace_replay.js` | Replay date/timeline state and overlay shaping |

All charts remain Apache ECharts. Do not add another charting library.

## Data model and migration

### Additive migration framework

Create `schema_migrations.py` before adding columns. Migrations run inside a transaction where SQLite supports it, are idempotent, and update `PRAGMA user_version` only after success. Before the first production migration, use the existing backup path in `models.py`. Do not call `--reset-db` for this rollout.

Add a temporary-database test for a legacy schema, current schema, interrupted migration retry, and a database with the new schema already applied.

### Quote fields on `RawOptionGreek`

Add nullable columns to the existing immutable contract snapshot row:

- `bid`, `ask`, `mid_price`, `last_price` (`Float`);
- `bid_size`, `ask_size`, `volume` (`Integer`);
- `implied_volatility` (`Float`);
- `bid_timestamp`, `ask_timestamp`, `last_timestamp` (`DateTime`).

Old rows remain null and are never synthetically backfilled. Update `requirements.txt` to require `publicdotcom-py>=0.1.22`, because that installed SDK version includes `get_strategy_quote()` and current option-chain quote/Greek models.

### Signal event additions

Retain `SignalEvent.confidence` for legacy reads but treat it as legacy Data Quality. Add:

- `data_quality` (`Float`);
- `edge_probability` (`Float`, nullable);
- `state_key` (`String`, indexed);
- `scenario_type` (`String`, indexed);
- `session_date` (`Date`, indexed);
- `event_tags_json` (`Text`);
- `liquidity_grade` (`String`);
- `is_opportunity` (`Boolean`, indexed, default false).

New broadcasts record a state transition, not every refresh. A state key includes symbol, bias family, score bucket, regime, dealer state, liquidity state, active scenario, event-risk bucket, and liquidity grade.

For each evaluation horizon, derive independent samples greedily in time order: after selecting an opportunity, exclude later same-symbol opportunities until that horizon has elapsed. Opposite-direction transitions may be stored for replay but do not create an overlapping independent sample for the same horizon.

Legacy signal rows are marked/treated as `is_opportunity = false` and excluded from default Edge Lab statistics. A separate diagnostic may compare legacy observations, but it must be visibly labeled correlated/legacy.

### New tables

Add:

1. `market_context_snapshots`: selected snapshot ID, captured time, session date, event name/time/risk, implied move, session OHLC, range consumed, realized volatility, cross-asset score, warnings JSON.
2. `decision_alerts`: emitted time, symbol, alert type, dedupe key (unique), prior/next state JSON, scenario ID, level, delivery status.
3. `trade_journal_entries`: optional signal event ID, symbol, scenario type, planned zones, actual entry/exit, option strategy, contracts, max risk, notes, exit reason, adherence score, timestamps.

Journal data is manual and local in this plan. Account-history import and screenshots are deferred.

## Implementation changes

### Phase 1 — Information architecture and trust language

Files: `web/index.html`, `web/main.js`, `web/style.css`, `appy.py`, `ninjatrader_broadcaster.py`, `OpenGamma.cs`, `docs/dashboard_data_thesis.md`, `docs/api_reference.md`.

1. Change navigation to Cockpit, Edge Lab, Regime, TRACE, Strike Matrix, One-Off, Settings.
2. Delete `view-dashboard` and the Gamma nav button. Remove its KPI/regime gauge and duplicate profile/sweep DOM.
3. Move Net Gamma Trend (`historyChart`) into TRACE beneath the heatmap as a collapsible secondary panel.
4. Replace `view-setups` with a new `view-edge-lab`; remove standalone setup cards from that view.
5. Add a Cockpit `Scenario & Execution` section containing scenario cards and execution-candidate cards.
6. Rename Flow navigation to Regime. Rename Whale labels to Index Gamma Basket.
7. Rename visible Confidence labels to Data Quality. Add a separate Historical Edge location.
8. Extract `updateSharedSymbolHeader(data)` from `renderDashboard()` so removing the Gamma DOM cannot break `loadSymbol()`.
9. Remove dashboard-only chart/toggle references from `switchView()`, `toggleGammaSweepOverlay()`, resize lists, and chart-instance cleanup.
10. Update NinjaTrader payload schema to version 2 with new names, retaining fallback aliases for one release.

Verification:

- A DOM contract test confirms there are no `view-dashboard`, `view-setups`, Gamma, Whale Flow, or bare Confidence labels.
- `loadSymbol()` works with Cockpit, Edge Lab, Regime, TRACE, Strike Matrix, and One-Off active.
- No chart resize/render call references a removed element.

### Phase 2 — Executable quote capture and Cockpit candidates

Files: `models.py`, `schema_migrations.py`, `publicData.py`, `execution_quotes.py`, `appy.py`, `web/decision_workspace.js`, `web/main.js`, `web/index.html`, `web/style.css`, `requirements.txt`.

1. Persist quote fields already present in Public option-chain responses.
2. Build `execution_quotes.py` around `PublicApiClient.get_strategy_quote()` with an injected client for tests.
3. Reject a candidate when any required quote is missing, non-finite, crossed, older than 30 seconds, or when the strategy spread exceeds 25% of mid. Make age/spread thresholds configurable in `settings.json`.
4. Grade liquidity `A/B/C/REJECTED` using quote validity, spread percentage, sizes, volume, and open interest. Document the exact rubric in code and docs.
5. Use the strategy ask as the conservative debit to enter and strategy bid as the conservative value to exit. Show mid for context only.
6. Return quote timestamp, bid/mid/ask, spread, max loss/reward, breakeven, fees field (zero/unknown until configured), settlement type, and minutes to close.
7. Add a local-only maximum-risk input. Contracts equal `floor(max_risk_dollars / (max_loss_per_contract + configured_fees))`; blank/invalid input displays no size.
8. Do not call Public order or preflight endpoints and do not add an order button.

Verification:

- Fixture tests cover valid, stale, missing, zero-bid, crossed, and wide-spread strategy quotes.
- Old snapshots with null quote fields remain readable.
- Every Cockpit execution candidate is `EXECUTABLE`, `MODELED_ONLY`, or `REJECTED`; only `EXECUTABLE` may show a suggested contract count.

### Phase 3 — Market Conditions Ribbon

Files: `market_context.py`, `models.py`, `schema_migrations.py`, `appy.py`, `publicData.py`, `web/index.html`, `web/main.js`, `web/decision_workspace.js`, `web/style.css`, `settings.json`, `.env.example`, `.gitignore`, `requirements.txt`.

1. Add provider interfaces with last-known-good caching for the official BLS ICS feed and Federal Reserve FOMC calendar. Add `requests`, `beautifulsoup4`, and `icalendar` as direct dependencies with bounded major versions.
2. Cache normalized events under `runtime/market_context_cache.json`; ignore `runtime/` in git. A fetch failure uses non-expired cache and adds a warning instead of blocking the dashboard.
3. Compute remaining-day implied move from the nearest-ATM same-expiry SPX call+put mid. If valid quote data is unavailable, return `null` with a warning; do not substitute a constant.
4. Compute session high/low/open from current-day snapshots, `range_consumed_pct = (session_high - session_low) / implied_move_points`, 15-minute realized volatility, and existing SPY/QQQ/IWM cross-asset agreement.
5. Optionally quote VIX1D/VIX9D/VIX through Public when supported. Missing indices show unavailable; do not scrape Cboe pages.
6. Add context settings for event windows. Defaults: `CAUTION` from 60 to 15 minutes before a high-impact event; `BLOCKED` from 15 minutes before through 15 minutes after.
7. Event risk affects eligibility, not Data Quality. `BLOCKED` forces the No Trade scenario; `CAUTION` leaves scenarios visible but untriggered.

Verification:

- Frozen fixtures test timezone/DST conversion, duplicate events, cache expiry, network failure, and event-window boundaries.
- Context calculation returns deterministic results for stored quote/snapshot fixtures.
- The Cockpit remains usable with every external context field unavailable.

### Phase 4 — Server-side scenario engine

Files: `scenario_engine.py`, `option_math.py`, `appy.py`, `web/decision_workspace.js`, `web/main.js`, `web/index.html`, `web/style.css`, `ninjatrader_broadcaster.py`, `OpenGamma.cs`.

Move `buildTradePlan()`, `choosePriceObjective()`, and `chooseInvalidation()` out of `web/main.js`. The backend is the single source of truth.

Generate four mutually exclusive scenario types:

- `PIN_MEAN_REVERSION`;
- `UPSIDE_EXPANSION`;
- `DOWNSIDE_EXPANSION`;
- `NO_TRADE`.

Every actionable scenario returns:

- `id`, `type`, `status` (`WATCH`, `TRIGGERED`, `INVALIDATED`, `UNAVAILABLE`);
- trigger zone low/high and plain-language trigger condition;
- target zone low/high;
- invalidation level/zone;
- horizon minutes and expiry/settlement cutoff;
- evidence list with source timestamps;
- Data Quality and Historical Edge summaries;
- remaining implied-move budget;
- eligible execution structures.

Zone half-width is the maximum of half the local strike spacing, 10% of remaining implied move, and a realized-volatility buffer; cap it at 30% of implied move. When implied move and realized volatility are both unavailable, do not manufacture zones—return `NO_TRADE` with a reason.

Eligibility fails for stale/missing required data, Data Quality below 0.45, blocked event window, invalid quotes for an execution candidate, elapsed settlement cutoff, or missing target/invalidation structure. Insufficient Historical Edge is displayed prominently but does not hide a watch-only scenario; it prevents the scenario from being labeled validated.

Verification:

- Table-driven tests cover each scenario, mixed pillars, flip crossing, event blocks, quote rejection, late-day cutoff, and missing context.
- The same backend scenario contract feeds Cockpit, NinjaTrader, replay, alerts, and outcome labeling.

### Phase 5 — Independent Edge Lab

Files: `signal_performance.py`, `models.py`, `schema_migrations.py`, `appy.py`, `web/edge_lab.js`, `web/main.js`, `web/index.html`, `web/style.css`.

1. Record signal state transitions and active-scenario changes; stop inserting identical signal events on every broadcast.
2. Derive non-overlapping samples independently for 15-, 30-, and 60-minute horizons.
3. Cluster statistics by trading day. Add deterministic day-bootstrap confidence intervals, unique days, independent count, target-first, invalidation-first, MFE, MAE, median after-cost move, and time-to-target.
4. Use chronological walk-forward splits. Default holdout is the latest 20% of eligible days, with at least 5 holdout days; otherwise status remains `INSUFFICIENT`.
5. Add calibration buckets only when model probabilities exist. Do not infer a probability from Data Quality.
6. Add `get_edge_lab(filters)` with filters for symbol, horizon, scenario, regime, event tag, time-of-day bucket, and liquidity grade.
7. Render summary cards, an ECharts calibration plot, expectancy distribution, outcome breakdown, and a table of independent opportunities.
8. Keep legacy correlated observations off by default behind a diagnostic toggle with a warning.

Verification:

- Synthetic overlapping fixtures prove raw transitions collapse to the expected independent count for each horizon.
- Bootstrap and split logic are deterministic under a supplied seed.
- Empty, insufficient, emerging, and calibrated states render without misleading percentages.

### Phase 6 — Transition alerts and broadcast contract

Files: `decision_alerts.py`, `models.py`, `schema_migrations.py`, `appy.py`, `event_utils.py`, `ninjatrader_broadcaster.py`, `OpenGamma.cs`, `web/main.js`, `web/index.html`.

Alert types:

- flip cross/hold/reject;
- GEX sign change;
- wall approach/breach/reclaim;
- `WAIT -> CALL/PUT` or active scenario invalidation;
- Data Quality/stale quote/collector failure;
- 50%, 80%, and 100% implied-move consumption;
- event caution/block windows.

Persist a dedupe key composed of session date, symbol, alert type, scenario/level bucket, and transition direction. Default cooldown is 10 minutes, except a new opposite transition and critical data failure bypass cooldown. Emit alerts only on state changes, never on a routine refresh.

Broadcast schema version 2. NinjaTrader uses new Data Quality, Index Basket, scenario, target zone, invalidation, context, and alert fields; it falls back to schema version 1 for one release.

Verification:

- Replaying the same snapshot twice produces no duplicate alert.
- Opposite transitions emit immediately.
- Eel activity feed and NinjaTrader receive the same alert ID and scenario ID.

### Phase 7 — TRACE replay and modeled Delta/Charm

Files: `option_math.py`, `gamma_sweep.py`, `backtest_gamma_butterflies.py`, `appy.py`, `web/trace_replay.js`, `web/main.js`, `web/index.html`, `web/style.css`.

1. Extract shared inferred-volatility/charm math into `option_math.py`; update backtests and runtime callers to use it.
2. Add `get_trace_dates(symbol)` and make `get_trace_data(symbol, date, lens)` date-aware while keeping the current bounded default.
3. Overlay scenario transitions, alert markers, target/invalidation zones, and outcome markers on the replay timeline.
4. Move Net Gamma Trend from the removed Gamma view into TRACE and synchronize its zoom/time cursor.
5. Add `MODELED_DELTA_PRESSURE` from matched OSI contract delta-exposure changes between consecutive snapshots.
6. Add `MODELED_CHARM_PRESSURE` from shared charm math and stored OI/Greeks.
7. Include model/missing-contract coverage in every lens payload. If matched contract coverage is below 80%, show a warning and do not present the lens as complete.

Verification:

- Runtime and backtest charm calculations match the same fixtures.
- Date selection never mixes sessions.
- Replay overlays align by timestamp and symbol.
- UI labels always contain `Modeled` for Delta/Charm and never contain `flow`.

### Phase 8 — Manual journal and review

Files: `models.py`, `schema_migrations.py`, `appy.py`, `web/edge_lab.js`, `web/trace_replay.js`, `web/main.js`, `web/index.html`, `web/style.css`.

1. Add manual journal create/update/delete endpoints with validation and local SQLite persistence.
2. Allow a Cockpit scenario or TRACE replay state to prefill a journal entry, but require the user to enter actual option fill, contracts, and exit.
3. Compute realized P&L, slippage from quoted mid/ask, MFE/MAE from the underlying path, and rule adherence separately.
4. Add weekly review groupings by scenario, regime, event tag, time bucket, liquidity grade, and adherence.
5. Keep model outcome performance separate from user execution performance.

Verification:

- CRUD tests use a temporary database.
- A journal entry may exist without a linked signal, but a linked signal must match the symbol/session.
- Browser and backend validation reject negative contract counts and malformed timestamps.

### Phase 9 — Documentation, rollout, and cleanup

Files: `README.md`, `docs/index.md`, `docs/setup.md`, `docs/api_reference.md`, `docs/dashboard_data_thesis.md`, `docs/development-notes.md`, `.github/workflows/ci.yml`, tests.

1. Document the new navigation, terminology, quote source, scenario contract, evidence gates, event behavior, and modeled-pressure limitations.
2. Update the thesis so Data Quality, Historical Edge, Index Basket, and event eligibility are unambiguous.
3. Add all new JavaScript files to CI syntax checks and all `test_web_*.js` files to Node tests.
4. Add migration smoke tests against a copied schema-only fixture, not the production database.
5. Measure `get_decision_workspace()` and TRACE response time on the local 6 GB database. Targets: p95 under 500 ms for a cached workspace and under 2 seconds for a full TRACE session query.
6. Remove compatibility aliases only after one release and after NinjaTrader schema version 2 is verified.

## Test plan and verification gates

Run after each task that touches its layer and at every phase boundary:

```powershell
$env:PYTHONDONTWRITEBYTECODE='1'
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
node --check web/request_coordinator.js
node --check web/trace_timeline.js
node --check web/decision_workspace.js
node --check web/edge_lab.js
node --check web/trace_replay.js
node --check web/main.js
node --check ui-playground/playground.js
node --test test_web_*.js
```

Required higher-level checks:

- Fresh temporary database initialization and legacy additive migration both succeed.
- The production-sized database is copied before migration; validation runs on the copy first.
- A no-network run loads cached/local GEX data and renders explicit context/quote warnings.
- Rapid symbol switching cannot apply stale workspace, quote, Edge Lab, or TRACE responses.
- Removed tab IDs and labels are absent from HTML, JavaScript, CSS, tests, and docs.
- No code path submits, replaces, cancels, or preflights an order.
- NinjaTrader version 1 fallback and version 2 payload parsing both pass fixture tests.

## Rollout and dependency order

1. Land the additive migration framework before any schema change.
2. Land information architecture and terminology with compatibility aliases.
3. Land quote capture before execution-aware candidates and after-cost Edge Lab statistics.
4. Land market context before scenario eligibility and event-aware outcomes.
5. Land the backend scenario contract before Cockpit cards, alerts, replay overlays, and journal links.
6. Correct independent sampling before exposing Edge Lab as a primary tab.
7. Land alert persistence before broadcasting new state transitions.
8. Extract option math before Delta/Charm runtime lenses.
9. Land manual journal last; it consumes stable scenario, quote, and replay contracts.

Use feature flags in `settings.json` during rollout:

- `decision_workspace_v2`;
- `live_strategy_quotes`;
- `market_context_enabled`;
- `edge_lab_enabled`;
- `trace_pressure_lenses`;
- `journal_enabled`.

Defaults remain false until the corresponding phase passes verification. Navigation migration and terminology changes do not require a feature flag.

## Assumptions and hard boundaries

- The application remains local-first, Windows-compatible, Eel/Python/SQLite, and Apache ECharts-based.
- Public.com remains the option-chain and strategy-quote provider.
- Existing user database contents are preserved through additive migrations and backup; no destructive reset is acceptable.
- Old rows with missing quotes/context are valid historical GEX data but cannot support executable-price statistics.
- Economic-calendar and volatility context may be unavailable; the UI degrades explicitly rather than blocking startup.
- No claim of actual dealer inventory, whale flow, trade direction, or customer/dealer side is added.
- No automatic order execution, account-history import, screenshot storage, AI narrator, or additional option strategy families are included.
- Implementers must preserve unrelated user changes and must not commit runtime caches, databases, logs, or credentials.

## Completion definition

The program is complete when:

- Gamma and Setups no longer exist as standalone feature tabs;
- Cockpit presents market conditions, scenario triggers/zones, Data Quality, Historical Edge, and fresh execution candidates in one workspace;
- Edge Lab reports independent, day-aware, after-cost evidence without treating repeated broadcasts as trades;
- Regime uses truthful Index Basket terminology;
- TRACE supports historical replay, overlays, Net Gamma Trend, and clearly modeled Delta/Charm lenses;
- alerts describe deduplicated state transitions;
- a manual journal closes the review loop;
- all verification gates pass on fresh, legacy-fixture, no-network, and production-sized-copy scenarios.
