# Decision-engine completion checklist

This checklist tracks the improvement plan against the independent PublicGexDashboard app. The plan's live-data gates are recorded here while offline work continues. The original legacy app, its runtime files, and its credentials remain untouched.

## Phase 0: Public.com capabilities

- [x] Add a read-only, rate-limited probe that prints JSON and never opens the app database (`scripts/probe-public-api.py`).
- [ ] Run the probe with this app's credentials during regular market hours; commit `docs/api-capabilities.json` with timestamp, numbers, yes/no for checks 0.1–0.6, and zero HTTP 429 responses.
- [ ] **Deferred open-interest timing check 0.7:** compare the same 20 contracts around 09:25, 09:35, and 12:00 ET. If exact readings are unavailable, record the first reading after 09:35 and mark update timing unverified; do not infer an overnight change from missing observations.
- [ ] Record which later steps are enabled, use fallbacks, or are skipped. The installed SDK is 0.1.25; the plan referenced 0.1.28, so the report must distinguish observed behavior from the plan's assumptions.

## Phase 1: Evidence loop

- [x] Add additive migrations and tests for engine-tagged signals and version-2 outcomes.
- [x] Record broadcaster and scenario-v1 decisions for every configured symbol with a valid snapshot without duplicate bridge records.
- [x] Connect the BLS and FOMC cached calendars; report unavailable state and warnings on fetch failure.
- [x] Label target first, invalidation first, timeout, and pin held; preserve legacy outcomes.
- [x] Record snapshot-path resolution, session date in Eastern Time, and scenario event tags.
- [ ] If Phase 0 confirms index minute bars, use them for outcome paths and test ambiguous bars; otherwise keep the recorded snapshot resolution.
- [ ] Verify demo, migration, Cockpit snapshot, NinjaTrader key superset, and backend suite; write `docs/reports/phase-1.md`.

## Phase 2: Shadow scenario engine

- [x] Add structural levels and scenario-v2 logic in shadow collection without changing the displayed version-1 decision.
- [x] Add fixture, property, and modeled-zero fallback checks.
- [x] Add a read-only replay for scenario-v1 and scenario-v2 over stored snapshots.
- [ ] Run the replay with `--stride-minutes 0` over every snapshot, review malformed timestamps, and write the final Gate 2 frequency assessment. The current `docs/reports/replay-exploratory.json` is an hourly sample only.
- [ ] Review the replay report before switching the Cockpit or broadcaster to version 2.

## Phase 3: Public.com data expansion

- [ ] Add only the multi-expiration, overnight-OI, estimated-flow, and volatility inputs supported by Phase 0 evidence.
- [ ] Validate new level sources on a chronological session holdout before feeding decisions.
- [ ] Complete a 30-minute live smoke run with zero HTTP 429 responses and collect ten sessions of new data; write `docs/reports/phase-3.md`.

## Phase 4: Option-term scoring

- [ ] Use option bars for exit timing when available and stored bid/ask quotes for conservative fills; exclude missing two-sided quotes.
- [ ] Add session-separated baselines, a regime study, and Edge Lab sample/exclusion counts; write `docs/reports/phase-4.md`.

## Phase 5: Probability

- [ ] Wait for at least 40 sessions and 200 independent version-2 opportunities.
- [ ] Fit a walk-forward probability that treats timeouts as non-target outcomes, so the published value is not conditional on a barrier being hit.
- [ ] Publish only families whose out-of-sample Brier skill interval clears zero; write `docs/reports/phase-5.md`.

## Final verification and delivery

- [ ] Run `npm run build`, `npm test`, `npm run test:e2e`, and `python -m pytest backend/tests -q`.
- [ ] Run native compilation and smoke checks when desktop behavior changes. `npm run native:check` currently stops because MSVC C++ Build Tools (`cl.exe`) are not installed or discoverable; `vswhere` finds no installation with `Microsoft.VisualStudio.Component.VC.Tools.x86.x64`.
- [ ] Confirm database, credentials, logs, and collector locks remain isolated from the original app; NinjaTrader schema-v2 keys and port 5010 remain compatible.
- [ ] Push the decision-engine branch only when the requested work is done.
