import numpy as np
import pytest

pytest.importorskip('scipy', reason='Install requirements-research.txt for offline model tests')
pytest.importorskip('numba', reason='Install requirements-research.txt for offline model tests')

from research.price_models.kalman import KalmanModel
from research.price_models.baselines import Baseline


def block(seed=1, n=80):
    rng = np.random.default_rng(seed)
    g = rng.uniform(-1, 1, n)
    returns = 0.2 + 2 * g[:-1] + rng.normal(0, 0.8, n - 1)
    y = np.r_[100000., 100000. + np.cumsum(returns)]
    return {'y': y, 'g': g, 'anchor': y + 3, 'price_anchor': y.copy()}


@pytest.mark.parametrize('use_gex', [False, True])
def test_kalman_causal_and_reset(use_gex):
    model = KalmanModel(use_gex).fit([block(i) for i in range(4)])
    b = block(21)
    first = model.predict(b)
    changed = {k: v.copy() for k, v in b.items()}
    changed['y'][40:] += 1000
    changed['g'][40:] *= -1
    changed['anchor'][40:] -= 1000
    second = model.predict(changed)
    np.testing.assert_allclose(first['mean'][:40], second['mean'][:40])
    np.testing.assert_allclose(first['variance'][:40], second['variance'][:40])
    assert np.all(np.isfinite(first['mean']))
    assert np.all(first['variance'] > 0)
    model.predict(block(60))
    np.testing.assert_allclose(first['mean'], model.predict(b)['mean'])


def test_kalman_recovers_known_gamma_drift():
    model = KalmanModel(True).fit([block(i, 180) for i in range(6)])
    assert model.beta[0] * model.scale > 1.2
    assert model.diagnostics['converged']


def test_random_walk_has_zero_mean_and_uses_only_past_variance():
    model = Baseline().fit([block()])
    b = block(4)
    p = model.predict(b)
    np.testing.assert_array_equal(p['mean'], 0)
    assert np.all(p['variance'][:, 5] > p['variance'][:, 2])
    changed = {k: v.copy() for k, v in b.items()}
    changed['y'][40:] += 500
    np.testing.assert_allclose(p['variance'][:40], model.predict(changed)['variance'][:40])
