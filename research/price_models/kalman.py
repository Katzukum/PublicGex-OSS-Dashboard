"""Local linear trend with optional lagged gamma covariates in the transition.

Prices and forecasts use log-price basis points. Each block starts a new filter;
only training blocks estimate parameters. Forecast GEX covariates are frozen at
the origin, so these are conditional scenarios, not forecasts of future GEX.
"""
from __future__ import annotations

import numpy as np
from numba import njit
from scipy.optimize import minimize


@njit(cache=True)
def _filter(y, features, starts, qlevel, qslope, obsvar, beta):
    n = len(y)
    states = np.zeros((n, 5))
    loss = 0.0
    level = slope = p00 = p01 = p11 = 0.0
    for i in range(n):
        if starts[i]:
            level, slope = y[i], 0.0
            p00, p01, p11 = obsvar, 0.0, 1.0
        else:
            adjustment = 0.0
            for j in range(len(beta)):
                adjustment += beta[j] * features[i - 1, j]
            level += slope + adjustment
            a00 = p00 + 2 * p01 + p11 + qlevel
            a01 = p01 + p11
            a11 = p11 + qslope
            innovation_var = a00 + obsvar
            innovation = y[i] - level
            loss += 0.5 * (np.log(innovation_var) + innovation**2 / innovation_var)
            k0, k1 = a00 / innovation_var, a01 / innovation_var
            level += k0 * innovation
            slope += k1 * innovation
            p00 = max(a00 - k0 * a00, 1e-12)
            p01 = a01 - k0 * a01
            p11 = max(a11 - k1 * a01, 1e-12)
        states[i, 0] = level
        states[i, 1] = slope
        states[i, 2] = p00
        states[i, 3] = p01
        states[i, 4] = p11
    return loss, states


class KalmanModel:
    def __init__(self, use_gex=False):
        self.use_gex = bool(use_gex)

    def _features(self, block):
        if not self.use_gex:
            return np.zeros((len(block['y']), 0))
        return np.column_stack((
            np.asarray(block['g'], dtype=float),
            np.clip((np.asarray(block['anchor']) - block['y']) / self.scale, -8, 8),
        ))

    def fit(self, blocks):
        if not blocks or sum(len(b['y']) - 1 for b in blocks) < 25:
            raise ValueError('Kalman needs at least 25 training returns')
        returns = np.concatenate([np.diff(b['y']) for b in blocks])
        self.scale = max(float(np.sqrt(np.mean(returns**2))), 1e-3)
        y = np.concatenate([(np.asarray(b['y']) - b['y'][0]) / self.scale for b in blocks])
        features = np.concatenate([self._features(b) for b in blocks])
        starts = np.zeros(len(y), dtype=np.bool_)
        starts[np.cumsum([0] + [len(b['y']) for b in blocks[:-1]])] = True
        k = 2 if self.use_gex else 0

        def objective(theta):
            ql, qs, r = np.exp(theta[:3])
            loss, _ = _filter(y, features, starts, ql, qs, r, theta[3:])
            return loss + 5.0 * np.sum(theta[3:]**2)

        attempts = []
        candidates = []
        for variances in [(0.8, 0.002, 0.05), (0.2, 0.02, 0.4)]:
            initial = np.r_[np.log(variances), np.zeros(k)]
            result = minimize(objective, initial, method='L-BFGS-B',
                              bounds=[(-12, 3)] * 3 + [(-1, 1)] * k,
                              options={'maxiter': 200, 'ftol': 1e-9})
            attempts.append({'success': bool(result.success), 'message': str(result.message),
                             'objective': float(result.fun), 'iterations': int(result.nit)})
            if result.success and np.isfinite(result.fun):
                candidates.append(result)
        if not candidates:
            raise RuntimeError(f'Kalman optimization failed: {attempts}')
        best = min(candidates, key=lambda x: x.fun)
        self.qlevel, self.qslope, self.obsvar = np.exp(best.x[:3])
        self.beta = best.x[3:]
        self.diagnostics = {
            'converged': True, 'scale_bps': self.scale,
            'level_variance': float(self.qlevel * self.scale**2),
            'slope_variance': float(self.qslope * self.scale**2),
            'observation_variance': float(self.obsvar * self.scale**2),
            'gex_coefficients_scaled': self.beta.tolist(),
            'variance_at_lower_bound': bool(np.any(best.x[:3] < -11.95)),
            'attempts': attempts,
        }
        return self

    def predict(self, block, max_steps=6):
        raw = np.asarray(block['y'], dtype=float)
        y = (raw - raw[0]) / self.scale
        features = self._features(block)
        starts = np.zeros(len(y), dtype=np.bool_)
        starts[0] = True
        _, states = _filter(y, features, starts, self.qlevel, self.qslope,
                            self.obsvar, self.beta)
        mean = np.zeros((len(y), max_steps))
        variance = np.zeros_like(mean)
        u = features @ self.beta
        level, slope, p00, p01, p11 = states.T.copy()
        for h in range(max_steps):
            level += slope + u
            p00 = p00 + 2 * p01 + p11 + self.qlevel
            p01 = p01 + p11
            p11 = p11 + self.qslope
            mean[:, h] = (level - y) * self.scale
            variance[:, h] = np.maximum(p00 + self.obsvar, 1e-12) * self.scale**2
        return {'mean': mean, 'variance': variance}
