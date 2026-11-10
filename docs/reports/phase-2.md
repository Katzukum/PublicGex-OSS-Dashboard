IN PROGRESS — scenario-v2 runs in shadow; replay, frequency gate, and user switch decision remain open.

# Phase 2 checkpoint

Branch: `codex/decision-engine-improvements`, based on pushed baseline `8bb4e75`. No pull request or new push yet, per user direction.

## Implemented

- `backend/levels.py`: modeled zero-gamma crossing nearest spot, cumulative crossing fallback marked `proxy`, call/put walls, 0DTE pin concentration, implied move and strike spacing.
- `backend/scenario_engine_v2.py`: positive, negative and transition regimes; late pin, call/put wall fades, and price-led expansions with named thresholds and reward-to-risk gates.
- `backend/publicData.py`: computes and records shadow version-2 decisions for each successfully collected symbol. The Cockpit and NinjaTrader remain on their existing decision logic.
- `backend/models.py`, `backend/schema_migrations.py`: additive two-sided pin band fields so the shadow pin outcome can be scored without changing the meaning of the existing `invalidation` field.

## Verification so far

`backend/tests/test_scenario_engine_v2.py` checks level definitions, modeled-zero fallback, pin timing, wall fades, both expansions, missing inputs, transition blockers, and random profile invariants. `backend/tests/test_collector_decisions.py` confirms all five symbols receive separate scenario-v1 and scenario-v2 records with the bridge off, and a repeat cycle creates no duplicates.

Project verification after these changes: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `npm run test:backend` 217 passed.

## Remaining Gate 2 evidence

- Add read-only historical replay for versions 1 and 2 and commit per-symbol/session counts.
- Check the planned 1–15 eligible version-2 scenarios per symbol per session frequency gate without tuning to fit.
- Review replay results with the user before changing the `decision_engine` setting or broadcasting version 2.

The pin scenario uses an explicit low/high invalidation band. The directional reward-to-risk filter does not apply to the pin because a pin's measured event is staying inside the band over the horizon, rather than hitting a directional target before a single invalidation price.
