# Phase 5 — Model code ready; publication blocked by evidence gate

`docs/model-features.md` freezes five signal-time features. `scripts/fit-edge-model.py` fits one regularized logistic model per scenario family with expanding, weekly-refit walk-forward predictions and a day-clustered Brier skill interval. Timeouts are non-target outcomes. `backend/edge_model.py` applies reviewed coefficients without a runtime model dependency. Missing features or model files return null; model artifacts are written with `user_reviewed: false`.
Edge Lab renders ten reliability bins with counts and observed target rates; they remain empty until approved probabilities have outcomes.

Gate 4 approval, at least 40 sessions and 200 independent version-2 opportunities, family-level positive Brier skill intervals, and report review are pending. No live probability is published.

Offline checks: `npm run build` passed; `npm test` 85 passed; `npm run test:e2e` 17 passed; `npm run test:backend` 229 passed. Pure predictor fixtures check review gating, coefficients, missing features, and the frozen signal-time feature boundary. `scikit-learn` is confined to `backend/requirements-dev.txt` and absent from the packaged lock file.
