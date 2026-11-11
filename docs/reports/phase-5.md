BLOCKED — model publication requires more independent sessions and opportunities.

# Phase 5 checkpoint

`docs/model-features.md` freezes five signal-time features. `scripts/fit-edge-model.py` fits one regularized logistic model per scenario family with expanding, weekly-refit walk-forward predictions and a day-clustered Brier skill interval. Timeouts are non-target outcomes. `backend/edge_model.py` applies reviewed coefficients without a runtime model dependency. Missing features or model files return null; model artifacts are written with `user_reviewed: false`.
Edge Lab renders ten reliability bins with counts and observed target rates; they remain empty until approved probabilities have outcomes.

The November 11 isolated live workspace currently has 1 session and 7 independent version-2 opportunities (4 timeouts counted as non-target outcomes); the minimum is 40 sessions and 200 opportunities. Gate 4, positive family-level Brier skill intervals, and report review are pending. No live probability is published.

Offline checks: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `python -m pytest backend/tests -q` 234 passed. Pure predictor fixtures check review gating, coefficients, missing features, and the frozen signal-time feature boundary. `scikit-learn` is confined to `backend/requirements-dev.txt` and absent from the packaged lock file.
