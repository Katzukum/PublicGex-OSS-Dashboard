IN PROGRESS — offline evidence-loop implementation is verified; the Phase 0 bar capability and final Gate 1 checks remain open.

# Phase 1 checkpoint

Branch: `codex/decision-engine-improvements`, based on pushed baseline `8bb4e75`. No pull request or new push yet, per user direction.

## Changes

- `backend/schema_migrations.py`, `backend/models.py`: additive schema versions 4 and 5 for `engine`, label version, risk multiple, path resolution, pin-held evidence, and two-sided pin bands. Existing rows retain version 1. Version 5 also upgrades databases that already applied version 4.
- `backend/signal_performance.py`, `backend/edge_lab.py`: engine-separated deduplication, scenario recording, version-2 target/invalidation/timeout and pin labels, and version-2 Edge Lab default.
- `backend/publicData.py`, `backend/ninjatrader_broadcaster.py`: record the Cockpit scenario for every configured symbol with a valid snapshot and the two index vote streams after collection, independently of bridge startup. Shadow scenario-v2 events are recorded separately.
- `backend/market_context.py`, `backend/appy.py`: bounded cached BLS/FOMC calendar load and Eastern Time conversion; failed refresh yields an unavailable event state with warnings.
- `backend/demo_data.py`: version-2 synthetic outcomes for fresh demo data, plus an additive upgrade of existing synthetic evidence.

## Verification so far

Baseline before Phase 1: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `npm run test:backend` 203 passed.

Current: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `npm run test:backend` 217 passed. Focused collector, calendar, signal, migration, and Edge Lab tests passed. An initial browser run exposed two existing-demo upgrade cases: version-1 synthetic outcomes and a database already at schema version 4 before pin bands were added. The synthetic evidence upgrade and version-5 migration fixed both; the full suite passed on rerun.

## Remaining Gate 1 evidence

- Capture a before/after Cockpit scenario fixture and compare NinjaTrader payload key sets.
- Use minute index bars if Phase 0 check 0.3 succeeds; otherwise document snapshot resolution.
- Record final suite counts and write the gate result.

## Deviations and limits

The user directed offline phases to continue while Phase 0's scheduled open-interest check remains on the completion checklist. The installed Public.com SDK is 0.1.25, while the plan cited 0.1.28. No live Public.com calls or live market-hour outcome checks have been made yet.

`backend/tests/test_edge_lab.py` changed its synthetic outcome fixture to version 2 because Edge Lab now intentionally filters to version-2 evidence by default; no assertion was removed or weakened.
