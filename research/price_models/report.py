"""Standalone HTML and static scientific plots for the offline experiment."""
from __future__ import annotations

import base64
from html import escape
import json
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from scipy.stats import norm


NAMES = {
    'random_walk': 'Random walk + EWMA', 'momentum': 'Damped momentum',
    'kalman_price': 'Kalman · price', 'kalman_gex': 'Kalman · GEX',
    'garch_price': 'GARCH · price', 'garch_gex': 'GARCH · GEX',
    'ou_price': 'OU · price anchor', 'ou_gex': 'OU · gamma-wall anchor',
    'markov_price': 'Markov · price', 'markov_gex': 'Markov · GEX',
}


def _image(path):
    return 'data:image/png;base64,' + base64.b64encode(Path(path).read_bytes()).decode('ascii')


def _skill(row, comparison='vs_random_walk', metric='rmse'):
    obj = row.get(comparison, {}).get(metric)
    if not obj:
        return '—'
    lo, hi = obj['ci95']
    return f'{obj["improvement_pct"]:+.1f}% [{lo:+.1f}, {hi:+.1f}]'


def _tables(summary, experiment):
    chunks = []
    for symbol in ['SPX', 'NDX']:
        for horizon in [15, 30]:
            rows = [r for r in summary if r['experiment'] == experiment and r['symbol'] == symbol and r['horizon_minutes'] == horizon]
            if not rows:
                continue
            rows.sort(key=lambda r: list(NAMES).index(r['model']))
            body = ''
            for r in rows:
                body += ('<tr><td>' + escape(NAMES[r['model']]) + '</td>'
                         f'<td>{r["rmse_bps"]:.2f}</td><td>{r["mae_bps"]:.2f}</td>'
                         f'<td>{r["coverage80"]*100:.1f}%</td><td>{r["width80_bps"]:.2f}</td>'
                         f'<td>{r["interval_score80_bps"]:.2f}</td><td>{_skill(r)}</td>'
                         f'<td>{_skill(r, metric="interval_score80")}</td>'
                         f'<td>{_skill(r, "vs_price_only", "interval_score80")}</td></tr>')
            chunks.append(f'<h3>{symbol} · {horizon} minutes</h3><p>{rows[0]["test_days"]} held-out dates; '
                          f'{rows[0]["origins"]:,} overlapping origins per model.</p><div class="scroll"><table>'
                          '<thead><tr><th>Model</th><th>RMSE ↓</th><th>MAE ↓</th><th>80% coverage</th>'
                          '<th>Band width</th><th>Interval score ↓</th><th>RMSE gain vs RW</th>'
                          '<th>Interval gain vs RW</th><th>GEX interval gain vs price</th></tr></thead>'
                          f'<tbody>{body}</tbody></table></div>')
    return '\n'.join(chunks)


def create_report(output, summary, daily, predictions, audit, protocol, run):
    output = Path(output)
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False, 'axes.spines.right': False,
                         'figure.facecolor': 'white', 'axes.facecolor': '#f7f9fc'})
    candidates = [n for n in NAMES if n.endswith('_gex')]
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True)
    for row_index, symbol in enumerate(['SPX', 'NDX']):
        rows = [r for r in summary if r['experiment'] == 'modern' and r['symbol'] == symbol and
                r['horizon_minutes'] == 30 and r['model'] in candidates]
        rows.sort(key=lambda r: candidates.index(r['model']))
        for col, metric in enumerate(['rmse', 'interval_score80']):
            ax = axes[row_index, col]
            values = [r['vs_random_walk'][metric]['improvement_pct'] for r in rows]
            intervals = [r['vs_random_walk'][metric]['ci95'] for r in rows]
            positions = np.arange(len(rows))
            ax.barh(positions, values, color=['#267a9d' if v > 0 else '#c45c47' for v in values], alpha=.85)
            for pos, (lo, hi) in zip(positions, intervals):
                ax.plot([lo, hi], [pos, pos], color='#24384a', lw=1.5)
                ax.plot([lo, hi], [pos, pos], '|', color='#24384a')
            ax.set_yticks(positions, [NAMES[r['model']].replace(' · ', '\n') for r in rows])
            ax.axvline(0, color='#526170', lw=1)
            ax.set_title(f'{symbol} · 30m · {"forecast-center RMSE" if metric == "rmse" else "80% interval score"}')
            ax.set_xlabel('Improvement over random walk (%) — right is better')
            ax.invert_yaxis()
            ax.grid(axis='x', alpha=.2)
    fig.suptitle('Held-out performance on newer history\nLines: paired whole-day bootstrap 95% intervals', fontsize=15)
    performance = output/'performance.png'
    fig.savefig(performance, dpi=160)
    plt.close(fig)

    # Deterministic illustration: latest SPX held-out day, earliest eligible origin at/after 10:30.
    subset = predictions[(predictions.experiment == 'modern') & (predictions.symbol == 'SPX')]
    example_text = 'No eligible SPX illustration.'
    example_path = None
    if len(subset):
        session = subset.session.max()
        possible = sorted(subset.loc[subset.session == session, 'timestamp'].unique())
        after = [t for t in possible if str(t)[11:16] >= '10:30']
        origin = (after or possible)[0]
        example = subset[(subset.session == session) & (subset.timestamp == origin)]
        fig, axes = plt.subplots(2, 2, figsize=(12, 8), constrained_layout=True, sharex=True, sharey=True)
        for ax, name in zip(axes.ravel(), candidates):
            rows = example[example.model == name].sort_values('horizon_minutes')
            if not len(rows):
                continue
            h = np.r_[0, rows.horizon_minutes.to_numpy()]
            center = np.r_[0, rows.mean_bps.to_numpy()]
            sd = np.r_[0, np.sqrt(rows.variance_bps2.to_numpy())]
            actual = np.r_[0, rows.actual_bps.to_numpy()]
            ax.fill_between(h, center-norm.ppf(.9)*sd, center+norm.ppf(.9)*sd, color='#8bb9d0', alpha=.4, label='Pointwise 80% band')
            ax.plot(h, center, color='#166381', lw=2, label='Forecast mean')
            ax.plot(h, actual, 'o-', color='#d55d41', lw=1.5, ms=3, label='Observed snapshot path')
            ax.axhline(0, color='#777', ls=':', lw=.8)
            ax.set_title(NAMES[name])
            ax.set_xlabel('Minutes from forecast origin')
            ax.set_ylabel('Log-price change (basis points)')
            ax.grid(alpha=.15)
        axes[0, 0].legend(fontsize=8)
        fig.suptitle(f'SPX · {str(origin)[:16]} New York assumed\nFixed-origin forecasts; illustration was not selected by forecast accuracy', fontsize=14)
        example_path = output/'example_paths.png'
        fig.savefig(example_path, dpi=160)
        plt.close(fig)
        example_text = f'SPX {origin}. Latest held-out date; first eligible origin at/after 10:30. '
        example_text += 'Bands describe each future endpoint separately; they are not an 80% guarantee for the entire path.'

    failed = run.get('failed_fits', [])
    fit_records = json.loads((output/'fits.json').read_text(encoding='utf-8'))
    diagnostics = []
    for fit in fit_records:
        d = fit.get('diagnostics', {})
        flags = []
        if d.get('converged') is False:
            flags.append('not converged')
        if d.get('zero_attraction'):
            flags.append('zero OU attraction')
        if d.get('variance_at_lower_bound'):
            flags.append('Kalman variance at lower bound')
        flags += d.get('warnings', [])
        if flags:
            diagnostics.append(f'{fit["experiment"]}/{fit["symbol"]}/fold {fit["fold"]}/{fit["model"]}: ' + '; '.join(flags))

    sources = [
        ('Kalman local linear trend', 'https://www.statsmodels.org/stable/examples/notebooks/generated/statespace_local_linear_trend.html'),
        ('Gamma and intraday volatility: Amaya et al.', 'https://cdn.cboe.com/resources/education/research_publications/gammasqueezes.pdf'),
        ('GARCH volatility equations', 'https://arch.readthedocs.io/en/stable/univariate/univariate_volatility_modeling.html'),
        ('OU likelihood and exact transitions', 'https://arxiv.org/abs/1803.06460'),
        ('Markov switching and filtered versus smoothed probabilities', 'https://www.statsmodels.org/stable/examples/notebooks/generated/markov_autoregression.html'),
        ('Chronological forecasting evaluation', 'https://otexts.com/fpp3/tscv.html'),
    ]
    source_html = ''.join(f'<li><a href="{escape(url)}">{escape(label)}</a></li>' for label, url in sources)
    primary = [r for r in summary if r['experiment'] == 'modern' and r['horizon_minutes'] in (15, 30)]
    directional_wins = [r for r in primary if r['model'].endswith('_gex') and r['vs_random_walk']['rmse']['improvement_pct'] > 0.05]
    center_finding = (f'{len(directional_wins)} GEX model/symbol/horizon combinations reduced center RMSE; examine their paired intervals and simpler comparisons.'
                      if directional_wins else 'No GEX variant improved forecast-center RMSE over the no-change forecast in the four primary symbol/horizon comparisons. GARCH ties by construction.')
    volatility = [r['vs_random_walk']['interval_score80']['improvement_pct'] for r in primary if r['model'] in ('garch_price', 'markov_price')]
    volatility_finding = (f'Price-only GARCH and Markov interval-score gains span {min(volatility):.1f}% to {max(volatility):.1f}% versus the random-walk/EWMA benchmark. These gains should not be attributed to GEX.' if volatility else '')
    support = pd.read_csv(output/'day_support.csv')
    thin = support[support.origins < 10]
    thin_html = thin.to_html(index=False, border=0) if len(thin) else '<p>All scored dates have at least ten origins.</p>'
    robust_summary = json.loads((output/'sensitivity_min10_origins.json').read_text(encoding='utf-8'))
    robust_rows = [r for r in robust_summary if r['experiment'] == 'modern' and r['horizon_minutes'] == 30 and r['model'] in candidates]
    robust_html = '<table><thead><tr><th>Symbol/model</th><th>Days</th><th>Coverage80</th><th>RMSE gain vs RW</th><th>Interval gain vs RW</th><th>GEX interval gain vs price</th></tr></thead><tbody>'
    for r in robust_rows:
        robust_html += f'<tr><td>{r["symbol"]} / {NAMES[r["model"]]}</td><td>{r["test_days"]}</td><td>{100*r["coverage80"]:.1f}%</td><td>{_skill(r)}</td><td>{_skill(r, metric="interval_score80")}</td><td>{_skill(r,"vs_price_only","interval_score80")}</td></tr>'
    robust_html += '</tbody></table>'
    failures_html = f'<p class="warning">{len(failed)} fits failed; inspect fits.json. Their entire symbol/folds were excluded from every model to preserve common comparison support.</p>' if failed else '<p>All fitted folds produced finite forecasts. No model was silently substituted.</p>'
    example_html = f'<img src="{_image(example_path)}" alt="Four fixed-origin model forecasts and the observed path">' if example_path else ''
    html = f'''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Price-path model experiment</title><style>
body{{font:16px/1.55 system-ui,sans-serif;color:#213447;background:#edf2f7;margin:0}}main{{max-width:1260px;margin:auto;padding:36px;background:white}}
h1{{font-size:32px;margin-bottom:8px}}h2{{margin-top:38px;border-bottom:1px solid #ccd8e3;padding-bottom:8px}}h3{{margin-top:26px}}
p,li{{max-width:1050px}}.eyebrow{{color:#486c81;letter-spacing:.08em;font-size:12px;font-weight:700}}.scroll{{overflow-x:auto}}
table{{border-collapse:collapse;font-size:12px;width:100%;white-space:nowrap}}th,td{{padding:9px 10px;border-bottom:1px solid #dbe3eb;text-align:right}}th:first-child,td:first-child{{text-align:left}}th{{background:#eaf0f6}}tr:nth-child(even){{background:#f8fafc}}
img{{max-width:100%;height:auto}}code,pre{{background:#eef3f7;padding:3px 6px}}pre{{white-space:pre-wrap;font-size:12px;overflow-wrap:anywhere;padding:16px}}
.note{{border-left:4px solid #4783a1;background:#f0f6fa;padding:16px}}.warning{{background:#fff0e6;padding:12px}}a{{color:#166381}}details{{margin:16px 0}}footer{{font-size:13px;color:#5b6d7d;margin-top:36px}}
</style></head><body><main><div class="eyebrow">OFFLINE RESEARCH · FOUR MODEL FAMILIES · NO NEURAL NETWORKS</div>
<h1>Can the snapshots refine the next 15–30 minutes?</h1>
<p>Chronological tests of Kalman trend, gamma-conditioned GARCH, anchored OU and a two-state Markov model. Each has a price-only comparison. Model 5 (quantile regression) was excluded as requested.</p>
<div class="note"><strong>Read the results as an exploratory comparison.</strong> These are forecasts of log-price changes and pointwise ranges, not evidence of profitable execution. Positive gain means lower held-out loss. Intervals resample whole dates and do not correct for trying multiple model families or dependence across neighboring dates.</div>
{failures_html}
<p><strong>{escape(center_finding)}</strong></p><p>{escape(volatility_finding)}</p>
<h2>Primary comparison: newer data</h2>
<p>Active September history plus the July–August backup. The first 14 usable sessions train the initial models; later sessions are evaluated in blocks of seven, expanding training only after each block. SPX and NDX are fitted independently and are correlated market evidence.</p>
<img src="{_image(performance)}" alt="Thirty-minute held-out model gains with day-bootstrap intervals">
<p>RMSE and MAE measure forecast-center error. Interval score balances narrow bands against missed outcomes; lower is better. Nominal 80% coverage should be near 80%, but widening bands alone does not establish useful predictions. GARCH uses zero mean, so its center error equals the random-walk center by construction.</p>
{_tables(summary, 'modern')}
<h2>Older-history sensitivity</h2>
<p>Adds one legacy backup, with slower observations and longer gaps between dates. The first 20 usable sessions train the initial fit. These test dates differ from and overlap the primary run, so this is not independent replication. Compare within-run paired losses, not raw errors across the two experiments. Sampled raw checks reduce, but do not eliminate, historical calculation differences.</p>
{_tables(summary, 'all_history')}
<h2>Partial-day sensitivity: at least ten origins</h2>
<p>This check was added after observing that one partial date had only one scored forecast origin. Each date receives equal weight, so one hit or miss on that date can materially move reported coverage. The primary results above are preserved. This post-hoc diagnostic reuses exactly the same forecasts, excludes dates with fewer than ten origins, and does not refit or tune any model.</p>
<div class="scroll">{robust_html}</div><details><summary>Dates with fewer than ten scored origins</summary><div class="scroll">{thin_html}</div></details>
<h2>A fixed example path</h2><p>{escape(example_text)}</p>{example_html}
<h2>What was actually tested</h2><ul>
<li><strong>Kalman:</strong> local linear latent level/slope. The GEX version adds normalized gamma and clipped wall distance to the level transition. Gamma and the current wall-distance feature remain frozen for each forecast; this is constant conditional drift, not a force recalculated as projected price approaches the wall.</li>
<li><strong>GARCH:</strong> zero conditional mean and a positive variance intercept <code>omega * exp(theta * gamma)</code>. Gamma is in the variance itself, not merely a mean regressor. The price-only version sets theta to zero.</li>
<li><strong>OU:</strong> nonnegative attraction to a fixed-origin anchor. Price-only uses a trailing price center; GEX uses a proxy combining strongest call/put contract strikes, weighted by aggregate side GEX. A fitted zero attraction is allowed.</li>
<li><strong>Markov:</strong> two persistent residual-volatility states with a common AR(1) return mean; the GEX version adds a gamma mean regressor. Regression is fit first, then the volatility-state HMM (two-stage estimation, not joint likelihood). State probabilities are filtered using observations available at the origin. States are not presupposed to mean trend or pinning.</li>
<li><strong>Benchmarks:</strong> zero-drift random walk and damped short-term momentum, both using a causal EWMA volatility estimate. No quantile model was fit.</li></ul>
<h2>Data and validation safeguards</h2><ul>
<li>Read-only SQLite access. No live application, broker, collector settings, or database changes. One copy of duplicate legacy history is included.</li>
<li>Five-minute cash-session grid; known holidays and early closes handled. Gaps split sequences and forecasts never cross a gap or session boundary.</li>
<li>Spot is the latest nonstale snapshot as of the grid. GEX comes from the preceding collection cycle. Modern spot/features may be up to 90/150 seconds old; legacy up to 210/420 seconds. Collector timestamps are assumed New York local time and precede actual API responses: exact executable availability cannot be recovered.</li>
<li>All model coefficients, scales and initial variances come from preceding training dates. No future GEX, full-day volatility or smoothed future state probabilities enter held-out forecasts.</li>
<li>Every scored origin has six later observations in the same block, allowing identical 5–30-minute comparisons. Dates, not overlapping snapshot rows, are the uncertainty unit. Training blocks can contain shorter sequences.</li>
<li>Gaussian moment bands ignore parameter uncertainty and future GEX changes; Markov bands approximate a mixture. Empirical coverage tests those limitations. The latest-day plot is illustrative, not a selected success.</li>
<li>No tuning on held-out results. Hyperparameter and model-family exploration means these dates are now research validation data, not a pristine future confirmation sample.</li></ul>
<details><summary>Fit diagnostics and boundary warnings</summary><pre>{escape(json.dumps(diagnostics, indent=2))}</pre></details>
<details><summary>Full data audit</summary><pre>{escape(json.dumps(audit, indent=2))}</pre></details>
<details><summary>Declared protocol and source-code hashes</summary><pre>{escape(json.dumps(protocol, indent=2))}</pre></details>
<h2>Reproduce</h2><pre>python -m pip install -r requirements-research.txt
python -m pytest -q test_price_model_data.py test_price_model_kalman.py test_price_model_garch.py test_price_model_ou.py test_price_model_markov.py test_price_model_experiment.py
python -m research.price_models.experiment --output research/results/price_models_20260930</pre>
<p>Machine-readable evidence: <a href="summary.json">summary.json</a>, <a href="daily_losses.csv">daily losses</a>, <a href="day_support.csv">per-date sample counts</a>, <a href="sensitivity_min10_origins.json">partial-day sensitivity</a>, <a href="fits.json">fit parameters and convergence</a>, <a href="folds.json">exact training/test dates</a>, <a href="data_audit.json">data audit</a>, and compressed prediction CSVs for each run.</p>
<h2>Method references</h2><ul>{source_html}</ul>
<footer>Run time: {run['seconds']:.1f} seconds · {run['fit_count']} fits · {run['prediction_rows']:,} scored forecast rows. Method references motivate the experiment; they do not validate this dataset or these results.</footer>
</main></body></html>'''
    (output/'report.html').write_text(html, encoding='utf-8')
