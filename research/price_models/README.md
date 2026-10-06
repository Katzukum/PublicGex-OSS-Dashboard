# Offline price-path experiments

Four small models, each with a price-only and a GEX variant:

1. Kalman local linear trend.
2. Zero-mean GARCH(1,1), optionally with GEX in its positive variance intercept.
3. OU attraction to a trailing price center or a gamma-wall proxy.
4. Two-state Markov residual volatility with a common AR(1) return equation.

The user excluded quantile regression; it is not implemented or fitted here.
This package does not import the live app, place orders, edit settings, or write
SQLite database pages. Research dependencies are separate from dashboard runtime
dependencies. Tests needing SciPy/Numba skip if those optional packages are absent.

## Run

```powershell
python -m pip install -r requirements-research.txt
python -m pytest -q test_price_model_data.py test_price_model_kalman.py test_price_model_garch.py test_price_model_ou.py test_price_model_markov.py test_price_model_experiment.py
python -m research.price_models.experiment --output research/results/price_models_20260930
```

Use a new output directory for a later experiment to preserve earlier evidence.
For a quick data audit, add `--data-only`. For a development smoke run, use
`--symbols SPX --experiments modern --max-folds 1`; the saved protocol explicitly
marks such a run as a subset. `--root` selects the database directory.

## Evaluation design

The primary experiment uses active September data and the July–August backup,
with 14 initial training sessions and seven-session chronological test blocks.
The older-history sensitivity includes one legacy copy and begins with 20
training sessions. Each symbol is fitted independently. No coefficient or model
hyperparameter is selected on held-out results. All eight variants and two
baselines are reported, including poor models and zero-attraction OU fits.

The five-minute cash-session grid is causal and bounded by freshness limits.
GEX comes from the preceding snapshot. Invalid/stale points break sequences;
holidays, early closes, duplicate records, and gaps are handled explicitly.
Historical naive timestamps are assumed to be New York local time. They mark
collection start, so exact API-response availability remains unknown.

Each scored origin has six prior returns and six future grid observations in
the same block. The same origins are scored for every model and every 5–30-minute
horizon. Fits use preceding sessions only. Filters reset at block boundaries.
Future GEX is never read: GEX and OU anchors are frozen at the origin. Kalman
also freezes the current wall-distance feature, producing constant conditional
drift rather than recomputing a force around a fixed wall.

Summary losses equally weight dates. Paired bootstrap intervals resample whole
dates to account for overlapping origins within each date. They do not account
for cross-date dependence, parameter uncertainty, or model selection. A failed
model excludes that entire symbol/fold from every model's comparison and causes
a nonzero command exit. Nonconverged fits are not silently accepted.

After discovering a one-origin partial day in the initial run, a separate,
explicitly post-hoc sensitivity reports the same frozen forecasts only on dates
with at least ten origins. It does not refit models or replace primary results.

## Outputs and interpretation

- `report.html`: standalone report with embedded charts, methods and sources.
- `protocol.json`: fixed design, command, runtime details, code hashes.
- `data_audit.json`: source fingerprints, calendar, exclusions, freshness and
  sampled checks of summary GEX/wall semantics against raw contracts.
- `*_predictions.csv.gz`: forecast means/variances, observed returns and losses.
- `summary.json`, `daily_losses.csv`: day-weighted scores and paired uncertainty.
- `day_support.csv`, `sensitivity_min10_origins.json`: partial-day support check.
- `fits.json`, `folds.json`: fit diagnostics and exact chronological partitions.

Prices and errors use log-price basis points (one basis point is approximately
0.01%). The 80% bands are Gaussian moment approximations, including for the
Markov mixture. They are endpoint intervals, not simultaneous path guarantees.
The interval score penalizes both unnecessarily wide bands and missed outcomes;
lower is better. GARCH's zero mean equals the random-walk center by construction,
so its value should be judged by volatility/range scores, not directional RMSE.

The Markov regression and volatility HMM are estimated in two stages. OU uses
a weighted average of two strongest contract walls, not a whole-chain gamma
center. All methods remain hypotheses about this data; these experiments do
not establish execution profitability or dealer inventory causality.
