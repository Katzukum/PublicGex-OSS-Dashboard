BLOCKED — the option-scoring code is verified, but ten expanded-data sessions and a valid regime comparison are unavailable.

# Phase 4 checkpoint

Version-2 outcomes now mark a debit spread or pin butterfly from stored two-sided entry and exit quotes at the first barrier or timeout, including per-contract fees. Missing legs or quotes are excluded and counted. Edge Lab presents underlying R, option P&L/R, and matched random-time and 15-minute momentum R; its holdout splits by session, and CALIBRATED requires both day-clustered difference intervals above zero. The flat 0.10-point estimate is removed. `scripts/regime-study.py` provides a read-only study with day-clustered intervals, using Public.com one-minute index bars when collected and labeled collector-resolution proxies otherwise.

The November 11 probe confirmed day-period SPX/NDX one-minute bars and two-sided prices on the sampled SPX chain; the live collector persisted bars and used 60-second SPX/NDX outcome paths. Public.com option OHLCV bars do not themselves contain two-sided bid/ask fills, so conservative option P&L uses stored quotes. Only one expanded-data session is currently available. An exploratory run on the older independent-app history has 11 sessions but essentially no negative-regime 30-minute SPX observations, so its positive-minus-negative interval is unavailable. No regime premise decision or CALIBRATED status is asserted.

Offline checks: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `python -m pytest backend/tests -q` 234 passed. A regression test now accepts observations close to the requested horizon without requiring an exact timestamp. The browser test checks the underlying and option columns.
