# Decision-engine completion checklist

This checklist tracks the improvement plan against the independent PublicGexDashboard app. The plan's live-data gates are recorded here while offline work continues. The original legacy app, its runtime files, and its credentials remain untouched.

## Market-open run, November 11, 2026

- [x] Rebuild frontend (`npm run build`) and Python sidecar (`npm run build:sidecar`); frozen live and demo sidecar smoke tests pass.
- [x] Launch the updated live development app with its own ignored `data/` workspace; collector saved all five symbols, recorded vote/v1/v2 signals, and NinjaTrader listened on port 5010.
- [x] Fix the collector's first-cycle stall by loading the numerical workspace before starting its lifetime thread; verify the next cycle completed and `npm run test:backend` passed (229 tests).
- [x] Compile the native desktop executable and installer with Visual Studio 2019 Build Tools; packaged demo/live UI smoke passes. The rebuilt native app is launched against its own desktop workspace and owns NinjaTrader port 5010.
- [x] Finish the 09:25, 09:35, and noon ET Public.com probe: 171 requests, zero HTTP 429 responses. Multiple expirations, SPX/NDX day-period minute bars, option bars, VIX, and quote polling work; VIX1D/VIX9D and week/month period minute bars do not. OI was present but unchanged across all three readings, so update timing remains unverified. Expanded collection flags remain off pending their own live smoke.
- [x] Complete the 30-minute expanded live smoke in `docs/reports/phase-3.md`: 21 successful cycles, 24.15-second median, 2,595 term rows per snapshot on average, zero HTTP 429, and 87.37 MiB/hour main-database growth over a separate 30.33-minute size interval.

## Phase 0: Public.com capabilities

- [x] Add a read-only, rate-limited probe that prints JSON and never opens the app database (`scripts/probe-public-api.py`).
- [x] Run the probe with this app's credentials during regular market hours and save `docs/api-capabilities.json` with timestamps, coverage, check results, and zero HTTP 429 responses.
- [x] **Open-interest timing check 0.7:** compare the same 20 contracts at 09:25, 09:35, and 12:00 ET. All readings were available and identical; update timing remains unverified.
- [x] Record enabled, fallback, and skipped paths in `docs/reports/phase-0.md`, distinguishing observed SDK 0.1.25 behavior from the plan's 0.1.28 assumptions.

## Phase 1: Evidence loop

- [x] Add additive migrations and tests for engine-tagged signals and version-2 outcomes.
- [x] Record broadcaster and scenario-v1 decisions for every configured symbol with a valid snapshot without duplicate bridge records.
- [x] Connect the BLS and FOMC cached calendars; report unavailable state and warnings on fetch failure.
- [x] Label target first, invalidation first, timeout, and pin held; preserve legacy outcomes.
- [x] Record snapshot-path resolution, session date in Eastern Time, and scenario event tags.
- [x] Add the gated Public.com index-minute-bar collector and bar-path labeler, with invalidation winning an ambiguous bar; snapshot paths remain the fallback until check 0.3 passes.
- [ ] Verify demo, migration, Cockpit snapshot, NinjaTrader key superset, and backend suite; write `docs/reports/phase-1.md`.

## Phase 2: Shadow scenario engine

- [x] Add structural levels and scenario-v2 logic in shadow collection without changing the displayed version-1 decision.
- [x] Add fixture, property, and modeled-zero fallback checks.
- [x] Add a read-only replay for scenario-v1 and scenario-v2 over stored snapshots.
- [x] Replay every snapshot and write the Gate 2 assessment in `docs/reports/phase-2.md`: 34,384 valid of 34,385 stored, one malformed timestamp, zero replay errors. All five symbol means exceed the planned 1–15 eligible state transitions per session.
- [ ] Resolve the failed Gate 2 frequency measure and review the report before any Cockpit or broadcaster switch. Version 2 remains shadow-only; no thresholds were tuned to fit the result.

## Phase 3: Public.com data expansion

- [x] Add isolated term snapshots/contracts, combined and mapped views, first-after-09:35 OI, estimated-flow poller, and straddle ratios. Live collection flags remain off until Phase 0 confirms capability.
- [x] Add matched-distance chronological holdout validation. New levels stay observational until it passes on stored sessions.
- [x] Complete a 30-minute expanded live smoke with zero HTTP 429 responses and write `docs/reports/phase-3.md`.
- [ ] Collect ten independent expanded-data sessions and pass the chronological matched-distance holdout before any new level source feeds version 2.

## Phase 4: Option-term scoring

- [x] Add target/invalidation/timeout exit timing with stored bid/ask for conservative option fills; exclude and count missing two-sided quotes. Option OHLCV bars have no bid/ask, so they cannot supply conservative fills.
- [x] Add session-separated random and 15-minute momentum quote baselines, regime-study code, and Edge Lab sample/exclusion counts.
- [ ] Run the regime study after ten sessions of expanded data and write the Gate 4 report.

## Phase 5: Probability

- [ ] Wait for at least 40 sessions and 200 independent version-2 opportunities.
- [x] Add offline weekly-refit walk-forward fitting and pure-Python prediction that counts timeouts as non-target outcomes.
- [x] Gate publication on a positive day-clustered Brier skill interval and an explicitly reviewed coefficient artifact; the default remains null.
- [ ] Fit after the minimum data and Gate 4 decision, review the report, and write `docs/reports/phase-5.md`.

## Final verification and delivery

- [x] Run offline suites after code changes: `npm run build`, `npm test` (85 passed), `npm run test:e2e` (17 passed), and `npm run test:backend` (229 passed). Repeat after market-hour activation tomorrow.
- [x] Run native compilation and packaged demo smoke. `vswhere` missed an installed Visual Studio 2019 Build Tools compiler, so `scripts/native-run.ps1` now discovers it directly; `npm run native:check` passes.
- [x] Confirm the browser and native data directories, credentials, logs, and collector locks stay within this independent app; the native bridge owns original externally compatible port 5010.
- [x] Compare NinjaTrader schema-v2 constructor key sets against baseline `8bb4e75`: all 18 dashboard keys and 26 top-level payload keys remain present with zero removals or additions.
- [x] Push `codex/decision-engine-improvements` after offline code checks. Live market-hour validation remains on this checklist.
