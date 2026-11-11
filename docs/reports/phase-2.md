FAILED — full historical replay exceeds the planned 1–15 eligible version-2 state transitions per symbol per session. Version 2 remains in shadow.

# Phase 2 checkpoint

Branch: `codex/decision-engine-improvements`, based on pushed baseline `8bb4e75`. No PR, per the user's direction.

## Implemented

- `backend/levels.py`: modeled zero-gamma crossing nearest spot, cumulative crossing fallback marked `proxy`, call/put walls, 0DTE pin concentration, implied move and strike spacing.
- `backend/scenario_engine_v2.py`: positive, negative and transition regimes; late pin, call/put wall fades, and price-led expansions with named thresholds and reward-to-risk gates.
- `backend/publicData.py`: computes and records shadow version-2 decisions for each successfully collected symbol. The Cockpit and NinjaTrader remain on their existing decision logic.
- `scripts/replay-scenarios.py`: read-only historical replay of both scenario builders, with an as-of snapshot cutoff and explicit sampling metadata. It accepts only this app's isolated data directories.
- `backend/models.py`, `backend/schema_migrations.py`: additive two-sided pin band fields so the shadow pin outcome can be scored without changing the meaning of the existing `invalidation` field.

## Verification so far

`backend/tests/test_scenario_engine_v2.py` checks level definitions, modeled-zero fallback, pin timing, wall fades, both expansions, missing inputs, transition blockers, and random profile invariants. `backend/tests/test_collector_decisions.py` confirms all five symbols receive separate scenario-v1 and scenario-v2 records with the bridge off, and a repeat cycle creates no duplicates.

Project verification after these changes: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `python -m pytest backend/tests -q` 234 passed. The packaged native demo and frozen live/demo sidecar smoke checks passed.

## Full replay and Gate 2

`python scripts/replay-full.py --workers 6` replayed all 34,385 stored snapshots from this app's isolated desktop database. It processed 34,384 valid snapshots across 11 sessions and 55 symbol-sessions, excluded one malformed timestamp, and reported zero replay errors. The parallel result was first checked against the existing 60-minute single-process sample: all 110 engine/symbol/session groups matched exactly. The complete JSON is [replay-full.json](replay-full.json).

| Symbol | Eligible v2 state transitions | Mean per session | Planned range |
| --- | ---: | ---: | ---: |
| IWM | 767 | 69.73 | 1–15 |
| NDX | 268 | 24.36 | 1–15 |
| QQQ | 487 | 44.27 | 1–15 |
| SPX | 611 | 55.55 | 1–15 |
| SPY | 665 | 60.45 | 1–15 |

All five symbol means exceed the planned range. This measure counts a new state when scenario, bias, target, invalidation, or pin band changes; moving structural levels can therefore produce many transitions within one broader trading episode. That counting definition was in the replay before the full result. It has not been changed or tuned after seeing the gate failure. The frequency and independence definition needs review before any v2 promotion. The Cockpit and NinjaTrader retain their established outputs, and no `decision_engine` switch was enabled.

The pin scenario uses an explicit low/high invalidation band. The directional reward-to-risk filter does not apply to the pin because a pin's measured event is staying inside the band over the horizon, rather than hitting a directional target before a single invalidation price.

Historical replay uses the current saved composition weights and an UNAVAILABLE event-calendar state where historical calendar observations were not stored. The replay omits research panels and chart levels, which do not enter either scenario decision.
