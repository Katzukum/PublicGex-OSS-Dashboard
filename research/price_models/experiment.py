"""Chronological, session-separated comparison of four non-neural model families.

Run from repository root with ``python -m research.price_models.experiment``.
This module never imports the application ORM, opens a broker, or writes a DB.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import time

import numpy as np
import pandas as pd
from scipy.special import ndtr
from scipy.stats import norm

from .baselines import Baseline
from .kalman import KalmanModel


PROTOCOL = {
    'version': 2,
    'symbols': ['SPX', 'NDX'],
    'grid_minutes': 5,
    'forecast_steps': [1, 2, 3, 4, 5, 6],
    'headline_horizons_minutes': [15, 30],
    'warmup_observations': 6,
    'fold_test_sessions': 7,
    'primary': {'name': 'modern', 'initial_training_sessions': 14,
                'sources': 'active + July-August pre-migration backup'},
    'sensitivity': {'name': 'all_history', 'initial_training_sessions': 20,
                    'sources': 'primary + one deduplicated legacy backup'},
    'interval_probability': 0.8,
    'bootstrap_resamples': 2000,
    'bootstrap_unit': 'whole trading date, paired model losses, equal date weight',
    'seed': 20260930,
    'selection': 'Fixed model specifications; no hyperparameter selection using held-out dates.',
    'training': 'Expanding window. Entire test sessions are later than all training sessions.',
    'forecast_covariates': 'Freeze GEX and OU level anchors at forecast origin; Kalman freezes current wall-distance feature too. Never use realized future GEX.',
    'scoring': 'Log-return basis points. Equal-weight daily losses; overlapping origins are not independent.',
    'intervals': 'Uncalibrated Gaussian moment intervals, including Markov mixture moment approximation.',
    'claim_scope': 'Exploratory forecasts, not trading profits or validated full-path coverage.',
    'posthoc_quality_sensitivity': 'After discovering a one-origin partial date, also report frozen forecasts on dates with >=10 origins. This does not replace the primary results and does not refit/tune models.',
}


def json_safe(value):
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, np.ndarray):
        return json_safe(value.tolist())
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (Path, np.datetime64)):
        return str(value)
    return value


def write_json(path, data):
    Path(path).write_text(json.dumps(json_safe(data), indent=2, allow_nan=False), encoding='utf-8')


def registry():
    from .garch import GarchModel
    from .ou import OUModel
    from .markov import MarkovModel
    models = [('random_walk', lambda: Baseline()), ('momentum', lambda: Baseline(True))]
    for family, constructor in [('kalman', KalmanModel), ('garch', GarchModel),
                                ('ou', OUModel), ('markov', MarkovModel)]:
        for gex in (False, True):
            models.append((f'{family}_{"gex" if gex else "price"}',
                           lambda c=constructor, g=gex: c(use_gex=g)))
    return models


def chronological_folds(blocks, initial_sessions, test_sessions=7):
    days = sorted({b['session'] for b in blocks})
    for k, start in enumerate(range(initial_sessions, len(days), test_sessions)):
        train_days, test_days = days[:start], days[start:start + test_sessions]
        training = [b for b in blocks if b['session'] in train_days]
        testing = [b for b in blocks if b['session'] in test_days]
        if not training or not testing:
            continue
        assert max(train_days) < min(test_days)
        yield k, training, testing, train_days, test_days


def score_forecasts(actual, mean, variance):
    actual, mean = np.asarray(actual), np.asarray(mean)
    sd = np.sqrt(np.maximum(np.asarray(variance), 1e-12))
    error = actual - mean
    z = error / sd
    radius = norm.ppf(0.9) * sd
    lower, upper = mean - radius, mean + radius
    width = 2 * radius
    interval_score = width + 10 * np.maximum(lower - actual, 0) + 10 * np.maximum(actual - upper, 0)
    crps = sd * (z * (2 * ndtr(z) - 1) + 2 * np.exp(-z*z / 2) / np.sqrt(2 * np.pi) - 1 / np.sqrt(np.pi))
    return {'squared_error': error**2, 'absolute_error': np.abs(error),
            'covered80': (np.abs(error) <= radius).astype(float), 'width80': width,
            'interval_score80': interval_score, 'crps_normal': crps,
            'direction_correct': np.where((np.abs(mean) < 1e-10) | (np.abs(actual) < 1e-10),
                                          0.5, (np.sign(mean) == np.sign(actual)).astype(float))}


def paired_bootstrap_skill(candidate, baseline, *, seed=20260930, samples=2000, square_root=False):
    """Percent loss reduction. Resample paired dates, never individual origins."""
    candidate, baseline = np.asarray(candidate), np.asarray(baseline)
    if len(candidate) != len(baseline) or not len(candidate):
        raise ValueError('Paired nonempty arrays required')
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(candidate), size=(samples, len(candidate)))
    c, b = candidate[idx].mean(axis=1), baseline[idx].mean(axis=1)
    point_c, point_b = candidate.mean(), baseline.mean()
    if square_root:
        c, b = np.sqrt(c), np.sqrt(b)
        point_c, point_b = math.sqrt(point_c), math.sqrt(point_b)
    skill = 100 * (1 - c / np.maximum(b, 1e-12))
    return {'improvement_pct': float(100 * (1 - point_c / max(point_b, 1e-12))),
            'ci95': np.quantile(skill, [0.025, 0.975]).tolist(), 'paired_days': len(candidate)}


def summarize(predictions):
    group = ['experiment', 'symbol', 'horizon_minutes', 'model']
    losses = ['squared_error', 'absolute_error', 'covered80', 'width80', 'interval_score80',
              'crps_normal', 'direction_correct']
    daily = predictions.groupby(group + ['session'], sort=True)[losses].mean().reset_index()
    daily_counts = predictions.groupby(group + ['session']).size().rename('origins').reset_index()
    daily = daily.merge(daily_counts, on=group + ['session'], validate='one_to_one')
    counts = predictions.groupby(group).size()
    summary = []
    for keys, frame in daily.groupby(group, sort=True):
        record = dict(zip(group, keys))
        record.update({'origins': int(counts.loc[keys]), 'test_days': len(frame),
                       'rmse_bps': float(np.sqrt(frame.squared_error.mean())),
                       'mae_bps': float(frame.absolute_error.mean()),
                       'coverage80': float(frame.covered80.mean()),
                       'width80_bps': float(frame.width80.mean()),
                       'interval_score80_bps': float(frame.interval_score80.mean()),
                       'crps_normal_bps': float(frame.crps_normal.mean()),
                       'direction_accuracy': float(frame.direction_correct.mean())})
        selection = daily[(daily.experiment == keys[0]) & (daily.symbol == keys[1]) &
                          (daily.horizon_minutes == keys[2])]
        for comparator, label in [('random_walk', 'vs_random_walk'),
                                  (keys[3].replace('_gex', '_price'), 'vs_price_only')]:
            if label == 'vs_price_only' and not keys[3].endswith('_gex'):
                continue
            ref = selection[selection.model == comparator].set_index('session')
            own = frame.set_index('session')
            common = own.index.intersection(ref.index)
            if len(common):
                record[label] = {
                    'rmse': paired_bootstrap_skill(own.loc[common, 'squared_error'],
                                                   ref.loc[common, 'squared_error'], square_root=True),
                    'interval_score80': paired_bootstrap_skill(own.loc[common, 'interval_score80'],
                                                                ref.loc[common, 'interval_score80'])}
        summary.append(record)
    return summary, daily


def common_successful_folds(predictions, fits):
    """Exclude an entire symbol/fold if any requested model failed there."""
    bad = {(f['experiment'], f['symbol'], f['fold']) for f in fits if not f['success']}
    if not bad or predictions.empty:
        return predictions
    keep = [(row.experiment, row.symbol, row.fold) not in bad
            for row in predictions[['experiment', 'symbol', 'fold']].itertuples(index=False)]
    return predictions.loc[keep].copy()


def minimum_day_support(predictions, minimum=10):
    """Explicit post-hoc quality sensitivity, retaining original primary scores."""
    keys = ['experiment', 'symbol', 'session']
    unique = predictions.drop_duplicates(keys + ['block', 'timestamp'])
    counts = unique.groupby(keys).size().rename('origins').reset_index()
    accepted = counts[counts.origins >= minimum][keys]
    return predictions.merge(accepted, on=keys, how='inner', validate='many_to_one'), counts


def run_experiment(blocks, name, initial_sessions, output, model_names=None, max_folds=None):
    records, fits, fold_manifest, examples = [], [], [], []
    model_specs = registry()
    if model_names:
        model_specs = [(n, f) for n, f in model_specs if n in model_names]
    for symbol in sorted({b['symbol'] for b in blocks}):
        symbol_blocks = sorted([b for b in blocks if b['symbol'] == symbol],
                               key=lambda b: str(b['timestamp'][0]))
        for fold, training, testing, train_days, test_days in chronological_folds(symbol_blocks, initial_sessions):
            if max_folds is not None and fold >= max_folds:
                break
            manifest = {'experiment': name, 'symbol': symbol, 'fold': fold,
                        'training_sessions': train_days, 'test_sessions': test_days,
                        'training_observations': sum(len(b['y']) for b in training),
                        'testing_observations': sum(len(b['y']) for b in testing)}
            fold_manifest.append(manifest)
            print(f'{name} {symbol} fold {fold}: {len(train_days)} train days -> {test_days[0]}..{test_days[-1]}', flush=True)
            for model_name, factory in model_specs:
                started = time.perf_counter()
                model = factory()
                try:
                    model.fit(training)
                    if model.diagnostics.get('converged') is False:
                        raise RuntimeError('Model did not converge; excluded from scoring')
                    for block in testing:
                        predicted = model.predict(block, max_steps=6)
                        means, variances = np.asarray(predicted['mean']), np.asarray(predicted['variance'])
                        expected_shape = (len(block['y']), 6)
                        if means.shape != expected_shape or variances.shape != expected_shape:
                            raise ValueError(f'Invalid forecast shapes: {means.shape}, {variances.shape}')
                        if not np.all(np.isfinite(means)) or not np.all(np.isfinite(variances)) or np.any(variances <= 0):
                            raise ValueError('Nonfinite forecast or nonpositive variance')
                        # Every horizon/model uses exactly the same origins with all 30 future minutes observed.
                        origins = np.arange(PROTOCOL['warmup_observations'], len(block['y']) - 6)
                        for h in range(1, 7):
                            actual = block['y'][origins + h] - block['y'][origins]
                            metrics = score_forecasts(actual, means[origins, h-1], variances[origins, h-1])
                            for j, i in enumerate(origins):
                                row = {'experiment': name, 'symbol': symbol, 'fold': fold,
                                       'model': model_name, 'session': block['session'],
                                       'block': block['block'], 'timestamp': str(block['timestamp'][i]),
                                       'horizon_minutes': h*5, 'actual_bps': float(actual[j]),
                                       'mean_bps': float(means[i, h-1]), 'variance_bps2': float(variances[i, h-1])}
                                row.update({key: float(value[j]) for key, value in metrics.items()})
                                records.append(row)
                    fit_record = dict(manifest, model=model_name, success=True,
                                      seconds=time.perf_counter()-started, diagnostics=model.diagnostics)
                except Exception as exc:
                    # A failed fit is explicit and invalidates its fold; no substitute model is scored.
                    records = [r for r in records if not (r['experiment'] == name and r['symbol'] == symbol and
                               r['fold'] == fold and r['model'] == model_name)]
                    fit_record = dict(manifest, model=model_name, success=False,
                                      seconds=time.perf_counter()-started, error=repr(exc),
                                      diagnostics=getattr(model, 'diagnostics', {}))
                    print(f'  FAILED {model_name}: {exc}', flush=True)
                fits.append(fit_record)
                print(f'  {model_name}: {fit_record["seconds"]:.1f}s', flush=True)
                write_json(output / f'{name}_fits.json', fits)
    return common_successful_folds(pd.DataFrame(records), fits), fits, fold_manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--output', type=Path, default=Path('research/results/price_models_20260930'))
    parser.add_argument('--experiments', nargs='+', choices=['modern', 'all_history'], default=['modern', 'all_history'])
    parser.add_argument('--symbols', nargs='+', default=['SPX', 'NDX'])
    parser.add_argument('--models', nargs='*')
    parser.add_argument('--max-folds', type=int)
    parser.add_argument('--data-only', action='store_true')
    args = parser.parse_args(argv)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    from .data import load_blocks
    # Persist the declared protocol before fitting or observing model scores.
    protocol = dict(PROTOCOL, command=sys.argv, python=sys.version, platform=platform.platform(),
                    exploratory_subset=bool(args.models or args.max_folds), symbols=args.symbols)
    source_dir = Path(__file__).parent
    protocol['code_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(source_dir.glob('*.py'))}
    write_json(output/'protocol.json', protocol)
    blocks, audit = load_blocks(args.root, symbols=tuple(args.symbols))
    write_json(output/'data_audit.json', audit)
    print(f'Loaded {len(blocks)} blocks; {sum(len(b["y"]) for b in blocks)} grid observations', flush=True)
    if args.data_only:
        return 0
    predictions, fit_records, folds = [], [], []
    started = time.perf_counter()
    for name in args.experiments:
        subset = [b for b in blocks if 'legacy' not in str(b['source']).lower()] if name == 'modern' else blocks
        initial = PROTOCOL['primary' if name == 'modern' else 'sensitivity']['initial_training_sessions']
        pred, fits, manifest = run_experiment(subset, name, initial, output, args.models, args.max_folds)
        if len(pred):
            predictions.append(pred)
            pred.to_csv(output/f'{name}_predictions.csv.gz', index=False, compression='gzip')
        fit_records.extend(fits)
        folds.extend(manifest)
    if not predictions:
        raise RuntimeError('No held-out predictions were produced')
    predictions = pd.concat(predictions, ignore_index=True)
    summary, daily = summarize(predictions)
    robust, support = minimum_day_support(predictions)
    robust_summary, _ = summarize(robust)
    write_json(output/'sensitivity_min10_origins.json', robust_summary)
    support.to_csv(output/'day_support.csv', index=False)
    write_json(output/'summary.json', summary)
    write_json(output/'fits.json', fit_records)
    write_json(output/'folds.json', folds)
    daily.to_csv(output/'daily_losses.csv', index=False)
    run = {'seconds': time.perf_counter()-started, 'fit_count': len(fit_records),
           'failed_fits': [f for f in fit_records if not f['success']], 'prediction_rows': len(predictions)}
    write_json(output/'run.json', run)
    from .report import create_report
    create_report(output, summary, daily, predictions, audit, protocol, run)
    print(json.dumps({'output': str(output), **{k:v for k,v in run.items() if k != 'failed_fits'}}, indent=2), flush=True)
    return 1 if run['failed_fits'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
