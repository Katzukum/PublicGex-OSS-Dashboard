import numpy as np
import pandas as pd
import pytest

pytest.importorskip('scipy', reason='Install requirements-research.txt for offline model tests')
pytest.importorskip('numba', reason='Install requirements-research.txt for offline model tests')

from research.price_models.experiment import chronological_folds, paired_bootstrap_skill, score_forecasts, common_successful_folds, minimum_day_support


def test_folds_use_whole_later_days_and_expand():
    blocks = [{'session': f'2026-09-{d:02}', 'id': (d, k)} for d in range(1, 16) for k in range(2)]
    folds = list(chronological_folds(blocks, 7, 3))
    assert [len(f[3]) for f in folds] == [7, 10, 13]
    all_test = []
    for _, train, test, train_days, test_days in folds:
        assert max(train_days) < min(test_days)
        assert not set(train_days).intersection(test_days)
        assert len(train) == 2 * len(train_days)
        all_test.extend(b['id'] for b in test)
    assert len(all_test) == len(set(all_test))


def test_interval_score_penalizes_misses_and_excessive_width():
    hit = score_forecasts(np.array([0.]), np.array([0.]), np.array([1.]))
    miss = score_forecasts(np.array([10.]), np.array([0.]), np.array([1.]))
    too_wide = score_forecasts(np.array([0.]), np.array([0.]), np.array([100.]))
    assert hit['covered80'][0] == 1
    assert miss['covered80'][0] == 0
    assert miss['interval_score80'][0] > hit['interval_score80'][0]
    assert too_wide['interval_score80'][0] > hit['interval_score80'][0]
    assert hit['crps_normal'][0] > 0


def test_paired_day_bootstrap_preserves_exact_loss_ratio():
    baseline = np.arange(1, 21, dtype=float)
    result = paired_bootstrap_skill(baseline*.81, baseline, square_root=True)
    np.testing.assert_allclose(result['ci95'], [10, 10], atol=1e-10)
    assert result['paired_days'] == 20


def test_neutral_forecast_is_not_scored_as_wrong_direction():
    scores = score_forecasts(np.array([-1., 1., 0.]), np.zeros(3), np.ones(3))
    np.testing.assert_array_equal(scores['direction_correct'], [.5, .5, .5])


def test_failed_model_excludes_its_entire_fold_from_comparison():
    rows = pd.DataFrame([{'experiment': 'modern', 'symbol': 'SPX', 'fold': f, 'model': m}
                         for f in [0, 1] for m in ['random_walk', 'kalman']])
    fits = [dict(r, success=not (r['fold'] == 0 and r['model'] == 'kalman')) for r in rows.to_dict('records')]
    clean = common_successful_folds(rows, fits)
    assert set(clean.fold) == {1}
    assert set(clean.model) == {'random_walk', 'kalman'}


def test_day_support_counts_unique_origins_not_models_or_horizons():
    rows = pd.DataFrame([{'experiment': 'modern', 'symbol': 'SPX', 'session': str(day),
                         'block': 'b', 'timestamp': str(i), 'model': model, 'horizon_minutes': h}
                        for day, n in [(1, 1), (2, 12)] for i in range(n)
                        for model in ['random_walk', 'kalman'] for h in [15, 30]])
    clean, counts = minimum_day_support(rows, 10)
    assert set(clean.session) == {'2'}
    assert counts.origins.tolist() == [1, 12]
