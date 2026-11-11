VERIFIED — the evidence loop, live index-bar labeling, version-1 fallback, and schema-v2 key compatibility pass. The original version-1 scenario builder is byte-identical to baseline `8bb4e75`.

# Phase 1 checkpoint

Branch: `codex/decision-engine-improvements`, based on pushed baseline `8bb4e75`. No PR, per the user's direction.

## Changes

- `backend/schema_migrations.py`, `backend/models.py`: additive schema versions 4 and 5 for `engine`, label version, risk multiple, path resolution, pin-held evidence, and two-sided pin bands. Existing rows retain version 1. Version 5 also upgrades databases that already applied version 4.
- `backend/signal_performance.py`, `backend/edge_lab.py`: engine-separated deduplication, scenario recording, version-2 target/invalidation/timeout and pin labels, and version-2 Edge Lab default.
- `backend/publicData.py`, `backend/ninjatrader_broadcaster.py`: record the Cockpit scenario for every configured symbol with a valid snapshot and the two index vote streams after collection, independently of bridge startup. Shadow scenario-v2 events are recorded separately.
- `backend/market_context.py`, `backend/appy.py`: bounded cached BLS/FOMC calendar load and Eastern Time conversion; failed refresh yields an unavailable event state with warnings.
- `backend/demo_data.py`: version-2 synthetic outcomes for fresh demo data, plus an additive upgrade of existing synthetic evidence.

## Verification so far

Baseline before Phase 1: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `npm run test:backend` 203 passed.

Current after live activation fixes: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed on an isolated demo port; `python -m pytest backend/tests -q` 234 passed. Focused collector, calendar, signal, migration, Edge Lab, and historical replay tests passed. An initial browser run exposed two existing-demo upgrade cases: version-1 synthetic outcomes and a database already at schema version 4 before pin bands were added. The synthetic evidence upgrade and version-5 migration fixed both; the full suite passed on rerun.

The Visual Studio 2019 compiler was found despite an empty `vswhere` inventory. The native executable and installer built; packaged demo and frozen live/demo sidecar smoke checks passed, and `npm run native:test` passed four Rust tests.

## Final Gate 1 evidence

- `backend/scenario_engine.py` has the same Git blob (`2b126fdb284651e6bac5d1a8ecfe6e0e353e08a2`) as baseline `8bb4e75`. The selector regression test checks that choosing `scenario_v1` preserves its active scenario and decision values; the demo service test changes the setting to v1 and back to v2. This function-level comparison substitutes for a separate before/after Cockpit screenshot fixture.
- Confirmed SPX/NDX DAY-period minute bars now supply 60-second outcome paths; other symbols and missing periods use recorded snapshot resolution.
- Version 0.1.6 verification: `npm run build` passes, `npm test` passes 85 tests, browser workflows pass 17 tests, and `python -m pytest backend/tests -q` passes 243 tests. The packaged native checks are recorded in `docs/reports/phase-2.md`.

The schema-v2 constructor key comparison against baseline `8bb4e75` found the same 18 dashboard keys and 26 top-level payload keys. A version-2 mapping test now verifies that selecting v2 changes values without adding or removing payload keys.

## Deviations and limits

The installed Public.com SDK is 0.1.25, while the plan cited 0.1.28. Phase 0's market-hour probe and Phase 3's expanded live smoke were subsequently completed and are documented in their own reports. The open-interest update time remains unverified because the sampled contract values did not change across the three scheduled readings.

`backend/tests/test_edge_lab.py` changed its synthetic outcome fixture to version 2 because Edge Lab now intentionally filters to version-2 evidence by default; no assertion was removed or weakened.
