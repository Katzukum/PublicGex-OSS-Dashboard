# Phase 4 — Code ready; data gate pending

Version-2 outcomes now mark a debit spread or pin butterfly from stored two-sided entry and exit quotes at the first barrier or timeout, including per-contract fees. Missing legs or quotes are excluded and counted. Edge Lab presents underlying R, option P&L/R, and matched random-time and 15-minute momentum R; its holdout splits by session, and CALIBRATED requires both day-clustered difference intervals above zero. The flat 0.10-point estimate is removed. `scripts/regime-study.py` provides a read-only study with day-clustered intervals, using Public.com one-minute index bars when collected and labeled collector-resolution proxies otherwise.

The required ten sessions of expanded data, a confirmed one-minute index-bar capability, live option quote coverage, and the regime premise decision are pending. Public.com option OHLCV bars do not themselves contain two-sided bid/ask fills, so conservative option P&L uses stored quotes.

Offline checks: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `npm run test:backend` 229 passed. The updated browser test checks the underlying and option columns. Existing chart-label assertions were updated because the distribution now has option R units.
