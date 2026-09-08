# Development Notes

## Charting Preference

Use Apache ECharts for dashboard and market-visualization charts by default.
This preference applies to this project and should be carried into future related
dashboard work unless a project has a strong reason to choose another charting
library.

## Decision-workspace invariants

- Server code owns scenario IDs, targets, invalidations, eligibility, and candidate status; the browser only renders them.
- Data Quality is not a win probability. Historical Edge cannot influence live bias until CALIBRATED and positive on holdout.
- Signal samples are state transitions and non-overlapping per horizon; legacy refresh-correlated rows are diagnostic-only.
- Delta and Charm surfaces must retain the word Modeled and display coverage below 80%.
- Public.com integration is limited to quotes/market data. Order and preflight endpoints are prohibited.
- Schema changes must be additive migrations verified on temporary databases before any production-path run.

Rollout order is controlled by `settings.json` feature flags: decision workspace and quotes, then Edge Lab/alerts, then TRACE replay/journal. Version-1 aliases are removed only after one verified release.

## Performance validation (2026-08-14)

Measured on an online SQLite backup of the 5.688 GB production database, after migrating only the copy: SPX cached decision-workspace p50 5.23 ms, p95 7.25 ms (target <500 ms); full 2026-08-14 TRACE session 897.31 ms for 8,232 cells (target <2 s). Cold workspace construction was 777.25 ms. The temporary benchmark database was deleted after measurement; the live database was never migrated or written.
