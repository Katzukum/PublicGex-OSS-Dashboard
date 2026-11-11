# Phase 3 — Gate blocked by market-hour capability evidence

Branch: `codex/decision-engine-improvements`. No PR, per the user's direction.

The optional collector path now stores expiration totals and contracts separately from the legacy 0DTE snapshot. It provides combined levels, a mapped index/ETF view, first-after-09:35 OI readings and prior-day comparisons, a four-second estimated-flow poller with coverage, and straddle ratios. `scripts/validate-term-levels.py` compares new levels with 0DTE levels on a chronological, matched-distance holdout. The decision engine still uses 0DTE levels. Both `multi_expiration_enabled` and `estimated_flow_enabled` default to false until the API probe confirms checks 0.2, 0.6, and 0.7. The collector caps requests at five per second even if an existing settings file requests more.

Live capability output, 30-minute zero-429 smoke run, ten expanded-data sessions, and validation report remain pending. The original app directory and runtime files were not read or modified.

Offline checks: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `npm run test:backend` 229 passed. New fixtures cover term strike mapping, OI gaps, flow counter resets and gaps, schema migration, and matched-distance validation.
