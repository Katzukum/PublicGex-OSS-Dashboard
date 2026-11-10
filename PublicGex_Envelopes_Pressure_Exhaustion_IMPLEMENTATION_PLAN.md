# Option Envelopes, Estimated Hedge Pressure, and Option Exhaustion — implementation plan

## Summary

Implement an observational research release in this independent PublicGex Dashboard: Option Envelopes first, Estimated Hedge Pressure second, Option Exhaustion third, followed by a historical shadow evaluator. Preserve existing decision outputs throughout. This document resolves implementation choices left open in the source proposal; its numerical defaults are engineering research hypotheses, not validated trading thresholds.

- Source proposal: `PublicGex_Envelopes_Pressure_Exhaustion_Plan.md` (preserve unchanged).
- Execution tracker: `PublicGex_Envelopes_Pressure_Exhaustion_tasks.json`.
- Target root: `C:/Users/desk/Desktop/Javascript Projects/PublicGexDashboard`.
- Inspected local revision: `a7bba1bd5c20ae47e1dbf8a1513c6f4214b7213e`, October 6, 2026. The proposal names an earlier revision; actual local code governs integration points.
- Deliverables from this planning task: this Markdown plan and the JSON tracker. Implementation and historical validation have not been performed.

## Target outcome and boundaries

Cockpit shows Flow/Structural Envelopes, a pressure chart with EMA15/EMA30 and coverage, and the latest inferred exhaustion candidate. TRACE shows historical envelopes, synchronized pressure, and evidence-bearing markers at their actual availability time. One-Off shows Structural Envelopes. A read-only CLI evaluates all three feature families against chronological baselines.

Hard requirements from `AGENTS.md`:

- React, strict TypeScript, and TradingView Lightweight Charts for all chart rendering. Reuse the existing chart host and custom primitives. Do not introduce ECharts or Plotly.
- Preserve Python analytical semantics, `oi_activity_v1`, default OI Gamma Sweep results, null prices, and NinjaTrader schema-v2 numerical fields.
- Leave scenario scoring, bias, target, invalidation, execution candidates, and signal generation unchanged. Research metadata must not enter these calculations indirectly.
- Use only this app's data, credentials, settings, logs, locks, and listeners. Never read or modify the original application's directory for execution or evaluation.
- Retain Tauri ownership of processes, explicit RPC allowlists, read-only broker usage, automatic live Collector/NinjaTrader startup preferences, and external port 5010. Busy ports and missing credentials must remain nonfatal; do not reclaim another listener.
- Demo remains synthetic, isolated, and unable to collect or broadcast live data.

Excluded from this release: wider collection windows, new vendors, order placement, derived-data tables, signal integration, and additional NinjaTrader fields. The proposal's optional broadcaster extension and signal integration are future gated work, not completion requirements for this release.

## Repository findings that affect implementation

| Existing code | Consequence for this plan |
| --- | --- |
| `backend/models.py: RawOptionGreek` stores volume, delta/gamma, quote prices and timestamps | No schema migration is initially needed; presence of columns does not guarantee usable historical values. |
| `backend/positioning.py: build_positioning/load_positioning` handles duplicate identities, adjacent observations, resets, and 120-second gaps | Extract reusable mechanics while preserving current-universe and current-gamma valuation semantics in positioning. |
| `load_positioning` reads only about 15 minutes and restricts expirations to the selected snapshot | It cannot directly supply session-cumulative pressure. Add a full-session research reader using the shared mechanics. |
| `backend/appy.py: get_trace_data` selects the latest snapshot per minute for heatmaps; several legacy queries require positive OI | Pressure must read every snapshot, including zero-OI contracts. Preserve legacy queries and add research queries. |
| TRACE currently returns latest-snapshot positioning even when its cursor moves | New research metadata must carry historical timestamps and availability bounds. Do not quietly present latest research at an earlier cursor. |
| `backend/gamma_sweep.py` weights contracts by OI and returns all modeled zero crossings | Add a keyword-only weight selector with the existing OI default, then adapt its output to envelopes. |
| Collection timestamps are naive local wall time; `execution_quotes.py` treats naive quote-source times as UTC | Do not subtract these clocks directly. Research needs an explicit clock policy and tests before pressure is usable. |
| `src/LightweightChart.tsx`, `chartViewport.ts`, `TraceCharts.tsx`, and `traceHeatmap.ts` already own chart lifecycle, view retention, and annotations | Extend these conventions rather than introducing a second chart framework. |
| `backend/positioning_validation.py` already opens a selected DB read-only and uses the earliest 80% of sessions for development | Extend it without importing application startup, ORM initialization, or collector code. |

## Architecture and data access

```text
This app's stored snapshots and option rows
    -> observation_intervals.py (identity, ordering, interval validity)
    -> research_observations.py (read-only session loading and clock normalization)
         -> option_envelopes.py -> configurable gamma_sweep.py
         -> flow_pressure.py -> exhaustion.py
    -> appy.py existing dashboard / TRACE / One-Off RPC results
    -> strict types and validators -> existing Lightweight Charts host

The same pure calculators and read-only reader -> positioning_validation.py
```

New backend files: `backend/observation_intervals.py`, `backend/research_observations.py`, `backend/option_envelopes.py`, `backend/flow_pressure.py`, `backend/exhaustion.py`. Calculators accept plain rows/configuration and never open databases or start processes.

`research_observations.py` reads the selected symbol's calendar session through an explicit `(timestamp, snapshot_id)` upper bound. Include a snapshot header for empty observations, uniquely identified contracts at each snapshot, all stored expirations, zero-OI contracts, quote fields, and spot samples. Use ordered batched queries, not one query per minute or contract. Validate adjacent snapshots before aggregating to minutes. Never bridge a missing contract or choose only contracts that survive until the final snapshot: doing so creates survivorship bias in pressure.

Contract identity is `(upper(symbol), expiration_date, osi_symbol)`. Exclude all duplicate copies of an identity within a snapshot. Adjacent snapshots must have strictly increasing times, gap at most 120 seconds, both contract endpoints, and finite nonnegative integral cumulative volumes with a nonnegative delta. A failed interval breaks continuity; never search farther back for a replacement endpoint. Keep exclusion counts by reason. Existing positioning still values valid volume changes with its selected snapshot's gamma and spot; pressure uses each interval's right endpoint.

Time policy for v1: preserve the existing calendar-session partition of naive collection times. Add research setting `observation_timezone = "America/New_York"`, explicitly documenting that this assumes collection on the current Eastern-time workstation. Localize only in the new research reader; do not rewrite stored timestamps or alter positioning. Quote timestamps with offsets represent their stated instant; naive quote-source times follow the existing UTC convention only after confirming collector/fixture serialization. If provenance cannot be established, classify those rows as unknown with a clock warning. Reject ambiguous/nonexistent local times instead of guessing. Emit original display timestamp plus canonical UTC `available_at`/minute-end timestamps for research, and keep timezone in cache/config identity. A different collection workstation must set the correct observation timezone before research is trusted.

## Feature decisions

### 1. Option Envelopes

Implement `build_option_envelopes(rows, *, spot, source="volume_gamma", symbol=None, as_of=None)`.

- `volume_gamma`: signed contribution is `sign * volume * gamma * spot^2`; `oi_gamma`: replace volume with reported OI. Calls have sign +1, puts -1. Require finite positive spot/strike, finite nonnegative gamma and weight, and a recognized side. Missing values remain missing; valid zero weights are measured zeros.
- Aggregate signed contributions by strike, rank distinct strikes with nonzero absolute net concentration by descending absolute strength, distance to spot, then ascending strike. Major/Minor are the top two; an absent rank is null. Return positive absolute strengths alongside levels. Exact call/put cancellation does not create a meaningful ranked level.
- Add `weight_field="open_interest" | "volume"` to `build_gamma_sweep` as a keyword-only argument. Reject other selectors. Preserve the old default branch's output and zero-crossing implementation. Do not require positive OI in the volume branch.
- Select the modeled crossing nearest current spot; break equidistant ties with the lower price. Return null if there is no crossing in the modeled range. Never extrapolate or substitute the legacy flip strike. An identically zero modeled curve is indeterminate for the new envelope adapter, not hundreds of valid envelope zeros.
- Return collected strike bounds and modeled sweep bounds separately. Flag Major/Minor within the outer 10% of the collected range, and a modeled zero within the outer 10% of the sweep range. A missing in-range crossing warns that an outside crossing is unknown; do not claim one exists.
- Coverage includes total/usable/excluded contract counts and sweep-usable count. `ready` means all candidate rows are usable and all three requested levels exist without boundary limitations; `partial` means at least one level is usable but exclusions, missing levels, or boundary limitations exist; otherwise `unavailable`. Null levels never become zero prices.
- Dashboard returns both modes. TRACE returns a timeline computed at each selected historical snapshot using only that snapshot. One-Off returns Structural mode; Flow is unavailable unless its stored provenance establishes same-session cumulative volume for that profile. Never join live/main DB observations into One-Off to fill it.
- Keep the existing approximately +/-3% collection range unchanged.

### 2. Estimated Hedge Pressure

Implement `build_estimated_pressure(observations, *, symbol, as_of, config)` using shared validated intervals. Do not reuse legacy modeled delta/charm pressure fields for this feature.

For a positive known volume delta, classify with the right-endpoint quote only when bid/ask/last are finite, bid >= 0, ask > bid, last >= 0, signed delta is valid for the option side and within [-1, 1], and interval spot is positive. Require bid, ask, and last timestamps with ages in [0, 120] seconds at the right endpoint; allow at most 5 seconds of future clock skew, clamped to age zero. Require the last timestamp to be later than the left endpoint and no later than right endpoint + 5 seconds. Unverifiable or stale quotes make the known volume delta unclassified.

```text
score = clip((last - (bid + ask)/2) / ((ask - bid)/2), -1, 1)
hedge_shares = delta_volume * score * signed_option_delta * 100
hedge_dollars = hedge_shares * right_endpoint_spot
```

An exact midpoint is classified neutral with measured zero impulse. Keep its volume in a separate neutral counter; do not assign BUY/SELL. Score > 0 selects BUY, score < 0 selects SELL. Four directional buckets store full classified contract volume, signed delta dollars, and unsigned `delta_volume * gamma * spot^2` activity; missing gamma makes that activity null without invalidating delta-based pressure. Preserve the four expected signs: call-buy positive, call-sell negative, put-buy negative, put-sell positive.

Minute and missing-data rules:

1. Assign the entire impulse to the minute containing its right endpoint. Do not spread it over unseen trade times. Publish completed minute points at minute end; expose the current incomplete minute separately as provisional, excluded from events and EMAs.
2. Bucket coverage and session coverage include classified/neutral/unclassified known volume, valid/discarded intervals by reason, observed minutes, max gap, and missing-clock counts. Classification ratio is classified known volume / all known positive delta volume, including neutral in the numerator. Reset/missing/gap intervals with unknowable volume get separate counts, never a fabricated zero-volume denominator.
3. A valid unchanged-volume interval proves zero activity. A minute with no valid interval is null. A minute with only unclassified positive flow is null. Partially classified minutes may return the sum of known impulses with `partial` status and exclusions; label cumulative values as observed classified pressure, not complete flow.
4. Start cumulative pressure at zero at the first valid baseline and add only classified impulses. Preserve the accumulator across missing minutes but output null in those minutes; later output resumes the observed total with an incomplete-session warning. Do not draw a line through the gap. Reset only at a session boundary.
5. EMA uses alpha `2/(N+1)` on completed one-minute cumulative values. Seed internally with the first numeric value, publish after N consecutive usable minutes, and restart the EMA warmup after a null or <30%-classified active minute. Defaults are N=15 and N=30. No forward fill and no poll-count EMA.
6. A minute is usable for downstream windows if it has a valid zero-activity observation or classification ratio >=30%. `slope_5m` is `(C[t]-C[t-5])/5` in dollars/minute over six consecutive usable completed points; five-minute impulse is `C[t]-C[t-5]`. EMA differences require both values. Rising/falling/flat uses slope sign, exact zero means flat, null means unavailable.
7. Session status is unavailable with fewer than two completed observed minutes or <30% classification of known positive volume; partial with 30%-<70%, fewer than five usable completed minutes, or any unexplained gaps/resets/clock exclusions; ready with >=70%, at least five usable completed minutes, and no such exclusions. With valid observed zero volume throughout, ratio is null and status is partial with a no-traded-volume note; numeric measured zero points remain valid.

Always display: “Estimated from option volume + quotes; dealer/customer identity is not observed.” Also explain that a snapshot's last trade cannot classify every trade between polls. Do not infer opening/closing activity or change in OI from volume.

### 3. Option Exhaustion

Implement `build_exhaustion_events(pressure, spot_observations, envelope_history, *, config, as_of)` with model `estimated_option_exhaustion_v1`. Events are candidates and scores are evidence scores, not reversal probabilities.

Bars are constructed from observed spot samples: first/max/min/last per minute, `bar_source="collector_spot_observations"`. Require at least two distinct sample times with positive finite prices to make a bar eligible; retain singleton bars as partial. Require five consecutive eligible completed bars by default. A zero five-bar range gives null price position and no price-filtered event. This conservative default intentionally suppresses signals during sparse polling and is reported in coverage.

Candidate gate at completed minute t:

- Current pressure minute is usable and classified ratio >=30%; positive relevant bucket flow exists.
- Use absolute bucket delta-dollar flow as the extremity measure. Compare to the previous 60 eligible completed minutes for that same bucket, excluding t, with at least 30 reference values. Include measured zero bucket flow; exclude unknown minutes. Percentile is `count(reference <= current)/count(reference)`; require current flow >0. This trailing definition never uses later session values.
- Low/Medium/High/Strong cutoffs are 0.70/0.80/0.90/0.95, default High. Explain that Strong is a stricter threshold, not “more markers.”
- With price filter enabled, CALL requires five-bar price position >=0.70; PUT requires <=0.30, inclusive. With it disabled, still require a valid completed current price bar and enough pressure history; score location as zero if the five-bar position is unavailable.
- Require at least one weakening flag among pressure_turn, pressure_price_divergence, ema15_reversal, flow_deceleration. Envelope proximity alone cannot trigger a marker.

Let `d` be the bucket's inferred hedge direction (+1 call-buy/put-sell, -1 call-sell/put-buy), C the cumulative classified pressure, and F the absolute bucket flow. The following fixed v1 definitions apply only when their inputs are consecutive and usable:

| Evidence | Definition |
| --- | --- |
| pressure_turn | `d*(C[t-1]-C[t-2]) > 0` and `d*(C[t]-C[t-1]) <= 0` |
| flow_deceleration | `F[t] < F[t-1] < F[t-2]`, with all three positive |
| ema15_reversal | `d*(C[t-1]-EMA15[t-1]) > 0` and `d*(C[t]-EMA15[t]) <= 0` |
| pressure_price_divergence | For CALL, current close exceeds all prior five closes while C[t] is below C at the latest prior maximum close; for PUT, current close is below all prior five closes while C[t] is above C at the latest prior minimum close. Use only the prior five completed eligible paired bars. |
| major/minor_gamma_proximity | Absolute current close-to-level distance <=0.1% of current close, using the selected envelope source's most recent already-available level at t. |

Evidence score: `0.30*percentile + 0.25*weakening + 0.20*location + 0.15*divergence + 0.10*proximity`. Weakening is 1 if turn, EMA reversal, or deceleration is true, otherwise 0. Location is price_position for CALL and 1-price_position for PUT, or 0 when unavailable with filter disabled. Divergence/proximity are 0 or 1. Return all components plus availability flags; do not renormalize missing evidence or add another score cutoff in v1.

Events carry deterministic ID `(model, symbol, session, minute, side, action, config fingerprint)`, `timestamp` at minute end, `available_at`, CALL/PUT side, BUY/SELL action, estimated label, price, percentile, price position, score/components, evidence flags, classification coverage, nearest envelope context, and `hedge_direction`. Suppress additional events for the same side/action for five minutes after an emission; allow exactly at +5 minutes. Cooldown resets by session and never depends on future candidates.

CALL is an upper-range candidate and PUT a lower-range candidate under the source proposal's filter; neither implies that BUY/SELL is a trading recommendation. Preserve hedge direction separately so call-sell/put-sell do not acquire misleading directional labels. Cockpit's active candidate is the latest event no older than five completed minutes; among simultaneous events use highest evidence score, then stable ID. Distinguish unavailable data from no active condition.

## API, caching, and replay contract

Extend existing RPC results; no new RPC is needed. `backend/service.py: DOMAIN_METHODS` and Rust allowlists stay restrictive.

| Surface | Additions |
| --- | --- |
| Dashboard | `envelopes: {flow, structural}`, `estimated_pressure`, `exhaustion: {model, status, current, coverage, warnings}` |
| TRACE | latest `envelopes`, `envelope_history: [{snapshot_id, as_of, available_at, flow, structural}]`, `estimated_pressure`, `exhaustion_events`, `exhaustion: {model, status, coverage, warnings}`, `research_config` |
| One-Off | `envelopes`, with explicit unavailable Flow as appropriate; no fabricated session pressure/history |

All model objects have fixed version literals, ready/partial/unavailable status, symbol/session/as-of metadata, coverage, and warnings. Envelope levels/strengths, pressure values, ratios without a denominator, and warmup values are nullable. Pressure points include timestamp/available_at, minute status, impulse and cumulative dollars/shares, EMAs, four directional buckets, neutral volume, and coverage. Shared config includes timezone, EMA periods, envelope source, and exhaustion settings with a stable fingerprint. Return canonical unavailable objects from the backend. Frontend accepts absent new top-level fields for older responses, but rejects malformed fields when present through `src/api.ts`; do not coerce strings, NaN, infinity, bad enums, negative coverage, or ratios outside [0,1].

TRACE must use an explicit as-of cursor boundary, not only a formatted minute label. Supply availability timestamps for selected snapshots so UI can select the last envelope already available and show only pressure points/events whose minute end is <= cursor as-of. Never draw the latest envelope backward over the session: render historical stepped levels broken at unavailable values. Hover/cursor research summaries resolve from that historical state. Full-session pressure is calculated from session start even if the visible window is short, then clipped for display; panning cannot reset cumulative pressure or percentile warmup. Existing latest-only positioning remains labeled as such.

Cache keys include workspace/engine identity, symbol, session, final snapshot ID and timestamp, visible request window where applicable, all three model versions, and configuration fingerprint. Store session research separately from legacy caches; bounded four-session LRU with 30-second TTL is the initial policy, return copies, and clear it on relevant settings changes. One-Off uses a separate engine namespace. Never cache unavailable historical fields as synthesized zeros. Avoid recalculating each history prefix independently: use a single chronological pass and per-snapshot envelope memoization.

## Frontend and settings

- Add `src/EnvelopePanel.tsx`, `src/PressurePanel.tsx`, `src/pressure.css`, and `src/ExhaustionMarkers.ts`; extend `src/types.ts`, `src/api.ts`, `src/Cockpit.tsx`, `src/ResearchViews.tsx`, `src/TraceCharts.tsx`, and `src/ProfileCharts.tsx`.
- Reuse `src/LightweightChart.tsx` and `src/chartViewport.ts`. Envelope source toggle sits near Gamma Sweep. TRACE uses solid Zero, stronger broken Major, and lighter broken Minor lines, with labels/strengths/coverage and the app's theme colors.
- Pressure uses a separate dollar scale and lower TRACE pane, synchronized by actual time range and crosshair with recursion guards. Nulls become whitespace gaps, not zeros. Retain zoom, replay cursor, source choice, and chart state across refresh, hidden tabs, and theme changes. Suspend hidden replay as today.
- Exhaustion custom markers expose all evidence in accessible tooltips/text, remain distinguishable for all four buckets, and participate in PNG capture. Test dense markers without obscuring price. Cockpit shows only the current candidate.
- Add nested `option_research` defaults/validation in `backend/app_settings.py`, `src/types.ts`, `src/api.ts`, and `src/ManagementViews.tsx`: `envelope_source="volume_gamma"`, `pressure_ema_1=15`, `pressure_ema_2=30`, `exhaustion_sensitivity="high"`, `exhaustion_price_filter_enabled=true`, `exhaustion_period_bars=5`, `exhaustion_call_above=0.70`, `exhaustion_put_below=0.30`, `observation_timezone="America/New_York"`.
- Validate EMA integers 2..120 with first < second, period integer 2..30, and `0 <= put_below < call_above <= 1`; validate timezone names and preset/source enums. Merge nested defaults with existing saved settings without overwriting unrelated preferences. Surface source/sensitivity/filter controls first; place period/threshold/timezone controls under advanced research settings. EMA periods remain persisted with defaults and can stay out of the initial UI.
- Config changes invalidate research state/caches consistently. Display the active configured EMA periods and price-filter thresholds, not hard-coded defaults.
- In `backend/demo_data.py`, seed deterministic quote/volume/spot observations that exercise the real calculators. Include sufficiently long sessions for 30 reference minutes plus warmup, two or more spot observations per eligible minute, upper/lower candidates, neutral flow, gaps, and missing values. Keep synthetic labels and isolation guards.

## Historical shadow evaluation

Extend `backend/positioning_validation.py` with selectable families `envelope_touch`, `pressure_direction`, `pressure_divergence`, and `exhaustion_reversal`. Preserve existing CLI/output behavior by default. Add explicit `--families`, `--output-json`, and `--output-csv` options; export files are separate from the selected read-only source DB. Include model/config versions and session IDs in reports.

- Use earliest 80% of sessions for development and remaining sessions for holdout, with at least two sessions; keep all records for one session in the same split. Freeze thresholds/config before holdout and never tune on it.
- Evaluate 5/15/30-minute outcomes with complete forward coverage and max 120-second gaps. Select nonoverlapping 30-minute observation starts per family for primary summaries; an all-candidates exhaustion appendix may report clustered events with session counts explicitly, never as independent samples. Calculate features from the prefix available at the evaluation instant, then load forward outcomes separately.
- Envelope outcomes: initial distance, first touch within 0.01% of spot tolerance, time to touch, signed excursions through level and back toward entry, and 5/15/30-minute returns after the first touch when covered. Freeze each event's level at activation. Compare Flow/Structural Major/Minor with persistent OI levels and nearest-strike baselines on identical eligible samples and distance buckets.
- Pressure states: 5/15-minute dollar slopes, EMA differences/crosses, trailing price divergence, and slope acceleration. Report forward returns, MFE/MAE in points and percent, and baseline target/invalidation outcomes only when contemporaneous baseline levels are available. Missing targets are null. Directional MFE/MAE follows the sign of the pressure condition.
- Exhaustion outcomes: upper CALL candidate hypothesizes downward reversal, lower PUT candidate upward reversal; additionally segment BUY/SELL and hedge direction. Report continuous signed returns/MFE/MAE and reversal-hit rate using a predeclared 0.1% entry-spot move in the hypothesized direction. Continuation failure rate is 1 minus that hit rate among fully covered candidates. If filter is disabled, mark these side-based outcome hypotheses explicitly. Segment by gamma regime and within 0.1% of Major/Minor/no nearby level.
- Every comparison reports eligible/excluded observations with reasons, covered sessions, classification quality, and missing forward outcomes. Use common eligible samples within each family comparison, not one global filter that discards valid old Structural sessions just because quote data is absent.
- At least 20 independent sessions and 100 usable exhaustion candidates are prerequisites to reviewing later signal integration, not proof of predictive value. An undersized dataset still produces a valid report marked insufficient evidence. This release is complete when the evaluator and research UI work; it does not wait for future market sessions or auto-enable trading logic.

## Ordered implementation tasks

The JSON tracker contains concrete subgoals, verification criteria, empty editable notes, and dependencies. All tasks begin `todo`.

| ID | Outcome | Dependencies |
| --- | --- | --- |
| T01 | Freeze regression fixtures and research contracts/defaults | none |
| T02 | Shared interval mechanics, session reader, and clock policy | T01 |
| T03 | Envelope calculations and existing-RPC backend integration | T02 |
| T04 | Envelope types/validation, Cockpit/TRACE/One-Off UI | T03 |
| T05 | Pressure calculations, minute series, coverage, RPC integration | T02, T03 |
| T06 | Pressure chart and cursor-safe synchronization | T04, T05 |
| T07 | Causal exhaustion detector with deterministic evidence | T03, T05 |
| T08 | Exhaustion markers, summaries, and persisted settings | T06, T07 |
| T09 | Deterministic demo and end-to-end replay/regression coverage | T08 |
| T10 | Read-only chronological shadow evaluator and exports | T07, T08 |
| T11 | Bound research query/cache/payload costs | T09, T10 |
| T12 | Full regression/native verification and release documentation | T11 |

## Verification and release gates

At T01 capture the current fixture outputs for regime, scenario type, bias, target, invalidation, execution candidates, NinjaTrader schema-v2 numbers, default Gamma Sweep, and positioning. Compare unchanged legacy fields for identical fixtures after each analytical integration. Separate research tests from the baseline; do not update baselines to accommodate unintended changes.

Add `backend/tests/test_observation_intervals.py`, `test_research_observations.py`, `test_option_envelopes.py`, `test_flow_pressure.py`, and `test_exhaustion.py`. Extend `test_positioning.py`, `test_gamma_sweep.py`, `test_positioning_validation.py`, `test_service.py`, `test_settings.py`, and existing decision/signal regression tests where necessary. Cover all four pressure signs; unchanged/reset/missing/duplicate volume; exact 120-second boundary; stale/future/ambiguous clocks; null versus zero; zero-OI retention; multiple/no/degenerate zero crossings; deterministic ranking; minute completion; EMA/gap/session reset; inclusive 70/30 boundaries; percentile warmup/ties; all presets; each evidence flag; cooldown; and sparse spot observations.

Causality gate: appending or modifying observations strictly after cutoff must not change any earlier envelope state, completed pressure point, or exhaustion event. Full-session output filtered at cutoff must equal direct prefix calculation. Apply this to calculator, RPC, and TRACE cursor fixtures, including same-minute snapshots and duplicate timestamp IDs.

Frontend coverage: extend `src/api.test.ts`, add `src/optionResearch.test.tsx` and pure model/primitive tests as needed. Verify strict malformed-payload rejection, omitted-field compatibility, null/zero labels, source changes, historical summaries, warnings, warmup, marker evidence, and settings persistence. Add `tests/e2e/option-research.spec.ts` using real Lightweight Charts APIs and existing `tests/e2e/chart-helpers.ts`; verify synchronized time/crosshair, cursor timing, theme/tab retention, resize, and PNG export alongside existing workflow suites.

Performance gate: use a deterministic five-symbol session benchmark of 390 minutes, four snapshots per minute, and 500 contracts per snapshot (measure one symbol's open view at a time). Record machine and cold/warm RPC latency, peak memory, returned bytes, and query counts before/after. Target <=2 seconds cold, <=500 ms warm, <=10 MB JSON per symbol/session; if missed, optimize batched reads, incremental scans, or payload shape before release. No per-contract/minute SQL and no unbounded cache. Do not add a derived table merely to pass without profiling evidence and a separate migration plan.

Required final commands from the project root:

```powershell
npm run build
npm test
python -m pytest backend/tests -q
npm run test:e2e
npm run native:test
```

Use the project's environment for Python: `npm run test:backend` invokes `.venv/Scripts/python.exe` with pytest and is the documented equivalent when system Python lacks dependencies. Native commands must follow `scripts/native-*` and README; do not use an unrelated global Rust setup.

For a packaged research release also run `npm run native:build`, `node scripts/native-ui-smoke.mjs`, and `.venv/Scripts/python.exe scripts/native-smoke.py`, checking that new modules are included in the frozen backend and chart primitives work under production CSP. Demo and fresh temporary-workspace smoke checks need no broker credentials. An actual live account/NinjaTrader chart session is not implied by these checks; record any unperformed manual checks accurately. Do not start a real live collector solely for validation.

Update `README.md` and the current section of `docs/IMPLEMENTATION.md` with research semantics, coverage limitations, settings, CLI examples, measured performance, and actual verification results. Existing historical implementation notes contain superseded chart/port choices; use `AGENTS.md` and current README as authority.

## Assumptions, risks, and later decisions

- The source proposal is the product specification, not independent verification of another vendor's implementation. No equivalence to proprietary participant or dealer data is claimed.
- Existing stored quote clocks may be insufficiently documented. T02 must verify serialization with fixtures; unknown provenance reduces coverage instead of silently asserting accuracy. No live credentials are required to implement this gate.
- Thirty-minute classifier history plus sparse spot sampling can leave exhaustion unavailable early or all day. The UI must explain this, and the evaluator must report exclusions. Do not relax data quality implicitly to produce attractive demo/live markers.
- The strike window truncates possible levels. Display coverage and boundary warnings; widening collection is a separate measured change that must preserve legacy calculation windows.
- Future optional NinjaTrader research fields require schema-v2 compatibility tests and explicit follow-up scope. Future scenario integration requires sufficient frozen holdout evidence, independent comparison of each proposed input, and a separately reviewed plan. Neither happens automatically as part of T12.
