"""A causal two-state Markov volatility model with a common return regression.

The latent states differ in innovation variance, not directional drift.  EM
smoothing is restricted to training.  Forecasts use only forward filtering,
reset at every supplied block, and freeze the origin's GEX covariate.
"""

from __future__ import annotations

import numpy as np

try:
    from numba import njit

    _ACCELERATED = True
except ImportError:  # The same small algorithm works without optional Numba.
    _ACCELERATED = False

    def njit(*args, **kwargs):
        return lambda function: function


@njit(cache=True)
def _expectation(residual, variances, transition, initial):
    """Scaled forward/backward recursion for one independently reset sequence."""
    n = len(residual)
    emission = np.empty((n, 2))
    maximum = np.empty(n)
    for t in range(n):
        value = residual[t] * residual[t]
        l0 = -0.5 * (np.log(2 * np.pi * variances[0]) + value / variances[0])
        l1 = -0.5 * (np.log(2 * np.pi * variances[1]) + value / variances[1])
        maximum[t] = max(l0, l1)
        emission[t, 0] = max(np.exp(l0 - maximum[t]), 1e-300)
        emission[t, 1] = max(np.exp(l1 - maximum[t]), 1e-300)
    forward = np.empty((n, 2))
    scale = np.empty(n)
    v0, v1 = initial[0] * emission[0, 0], initial[1] * emission[0, 1]
    scale[0] = v0 + v1
    forward[0, 0], forward[0, 1] = v0 / scale[0], v1 / scale[0]
    for t in range(1, n):
        v0 = (forward[t - 1, 0] * transition[0, 0]
              + forward[t - 1, 1] * transition[1, 0]) * emission[t, 0]
        v1 = (forward[t - 1, 0] * transition[0, 1]
              + forward[t - 1, 1] * transition[1, 1]) * emission[t, 1]
        scale[t] = v0 + v1
        forward[t, 0], forward[t, 1] = v0 / scale[t], v1 / scale[t]
    backward = np.ones((n, 2))
    for t in range(n - 2, -1, -1):
        b0 = emission[t + 1, 0] * backward[t + 1, 0]
        b1 = emission[t + 1, 1] * backward[t + 1, 1]
        backward[t, 0] = (transition[0, 0] * b0 + transition[0, 1] * b1) / scale[t + 1]
        backward[t, 1] = (transition[1, 0] * b0 + transition[1, 1] * b1) / scale[t + 1]
    gamma = forward * backward
    for t in range(n):
        total = gamma[t, 0] + gamma[t, 1]
        gamma[t, 0] /= total
        gamma[t, 1] /= total
    transitions = np.zeros((2, 2))
    for t in range(n - 1):
        for i in range(2):
            for j in range(2):
                transitions[i, j] += (
                    forward[t, i] * transition[i, j] * emission[t + 1, j]
                    * backward[t + 1, j] / scale[t + 1]
                )
    likelihood = 0.0
    for t in range(n):
        likelihood += np.log(scale[t]) + maximum[t]
    return likelihood, gamma, transitions


def _block_values(block, use_gex):
    y = np.asarray(block["y"], dtype=float)
    if y.ndim != 1 or not np.all(np.isfinite(y)):
        raise ValueError("Each y must be a finite one-dimensional array")
    g = np.asarray(block["g"], dtype=float) if use_gex else np.zeros(len(y))
    if g.shape != y.shape or not np.all(np.isfinite(g)):
        raise ValueError("g must be finite and have the same shape as y")
    returns = np.zeros(len(y))
    if len(y) > 1:
        returns[1:] = np.diff(y)
    return y, returns, g


class MarkovModel:
    def __init__(self, use_gex: bool):
        self.use_gex = bool(use_gex)
        self.diagnostics = {}
        self._fitted = False

    def fit(self, blocks):
        sequences, designs, outcomes = [], [], []
        for block in blocks:
            y, returns, g = _block_values(block, self.use_gex)
            if len(y) < 2:
                continue
            columns = [np.ones(len(y) - 1), returns[:-1]]
            if self.use_gex:
                columns.append(g[:-1])
            design = np.column_stack(columns)
            designs.append(design)
            outcomes.append(returns[1:])
        if not outcomes or sum(map(len, outcomes)) < 8:
            raise ValueError("MarkovModel requires at least eight within-block transitions")
        x, target = np.concatenate(designs), np.concatenate(outcomes)
        # A mild scale-aware ridge prevents a nearly constant GEX column from
        # producing an enormous coefficient.  The intercept is unpenalized.
        center = np.mean(x[:, 1:], axis=0)
        scale = np.maximum(np.std(x[:, 1:], axis=0), 1e-6)
        standardized = np.column_stack([np.ones(len(x)), (x[:, 1:] - center) / scale])
        penalty = np.eye(x.shape[1])
        penalty[0, 0] = 0
        standardized_beta = np.linalg.solve(standardized.T @ standardized + penalty, standardized.T @ target)
        beta = np.empty(x.shape[1])
        beta[1:] = standardized_beta[1:] / scale
        beta[0] = standardized_beta[0] - center @ beta[1:]
        raw_phi = float(beta[1])
        beta[1] = np.clip(beta[1], -0.95, 0.95)
        # Refit the intercept after the stationarity guard (and retain the
        # regularized GEX coefficient) so residuals remain centered.
        beta[0] = np.mean(target - x[:, 1:] @ beta[1:])
        for design, outcome in zip(designs, outcomes):
            sequences.append(np.ascontiguousarray(outcome - design @ beta))
        global_variance = max(float(np.mean(np.concatenate(sequences) ** 2)), 1e-8)
        floor = max(global_variance * 0.02, 1e-8)
        fits = []
        # Deterministic starts reduce sensitivity to a single EM initialization.
        for factors in ((0.3, 2.0), (0.7, 4.0)):
            variance = np.maximum(global_variance * np.asarray(factors), floor)
            transition = np.array([[0.95, 0.05], [0.05, 0.95]])
            initial = np.array([0.5, 0.5])
            previous = -np.inf
            converged = False
            iterations = 0
            for iteration in range(100):
                likelihood = 0.0
                occupancy, squares = np.zeros(2), np.zeros(2)
                starts, counts = np.zeros(2), np.zeros((2, 2))
                for residual in sequences:
                    loglik, gamma, changes = _expectation(residual, variance, transition, initial)
                    likelihood += loglik
                    occupancy += gamma.sum(axis=0)
                    squares += (gamma * residual[:, None] ** 2).sum(axis=0)
                    starts += gamma[0]
                    counts += changes
                iterations = iteration + 1
                if np.isfinite(previous) and abs(likelihood - previous) <= 1e-7 * (1 + abs(previous)):
                    converged = True
                    break
                previous = likelihood
                variance = np.clip(squares / np.maximum(occupancy, 1e-12), floor, global_variance * 100)
                # Small pseudocounts keep poorly sampled state transitions
                # finite; these are not additional observations or sequences.
                counts += np.array([[1.0, 0.1], [0.1, 1.0]])
                transition = counts / counts.sum(axis=1, keepdims=True)
                initial = (starts + 0.5) / (starts.sum() + 1.0)
            # Score the final parameters, including the last M step if the
            # iteration cap was reached, and report matching occupancies.
            likelihood, occupancy = 0.0, np.zeros(2)
            for residual in sequences:
                loglik, gamma, _ = _expectation(residual, variance, transition, initial)
                likelihood += loglik
                occupancy += gamma.sum(axis=0)
            fits.append((likelihood, variance, transition, initial, occupancy, converged, iterations))
        best = max(fits, key=lambda fit: fit[0])
        likelihood, variance, transition, initial, occupancy, converged, iterations = best
        order = np.argsort(variance)
        self.variances = variance[order].copy()
        self.transition = transition[np.ix_(order, order)].copy()
        self.initial = initial[order].copy()
        self.beta = beta.copy()
        self.diagnostics = {
            "model": "two_state_markov_volatility",
            "use_gex": self.use_gex,
            "transitions": int(len(target)),
            "blocks": len(sequences),
            "regression_coefficients": {
                "intercept_bps": float(beta[0]), "ar1": float(beta[1]),
                "gex_bps": float(beta[2]) if self.use_gex else 0.0,
            },
            "ar1_clipped": bool(raw_phi != beta[1]),
            "state_innovation_variances_bps2": self.variances.tolist(),
            "transition_matrix": self.transition.tolist(),
            "initial_probabilities": self.initial.tolist(),
            "training_state_occupancy": (occupancy[order] / occupancy.sum()).tolist(),
            "training_log_likelihood": float(likelihood),
            "converged": bool(converged),
            "iterations": iterations,
            "initializations": len(fits),
            "state_variance_ratio": float(self.variances[1] / self.variances[0]),
            "numba_accelerated": _ACCELERATED,
            "state_meaning": "Low and high residual volatility; states do not imply up or down.",
            "forecast_assumption": "Causal filtered states; hold origin GEX fixed; conditional on fitted parameters.",
        }
        self._fitted = True
        return self

    def _next_state_probabilities(self, returns, g):
        n = len(returns)
        result = np.empty((n, 2))
        next_probability = self.initial.copy()
        for t in range(n):
            if t:
                expected_return = self.beta[0] + self.beta[1] * returns[t - 1]
                if self.use_gex:
                    expected_return += self.beta[2] * g[t - 1]
                residual = returns[t] - expected_return
                log_emission = -0.5 * (np.log(self.variances) + residual**2 / self.variances)
                weight = next_probability * np.exp(log_emission - np.max(log_emission))
                filtered = weight / weight.sum()
                next_probability = filtered @ self.transition
            result[t] = next_probability
        return result

    def predict(self, block, max_steps=6):
        if not self._fitted:
            raise RuntimeError("Fit MarkovModel before predicting")
        if int(max_steps) != max_steps or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        max_steps = int(max_steps)
        y, returns, g = _block_values(block, self.use_gex)
        probabilities = self._next_state_probabilities(returns, g)
        mean, shock_variance = np.empty((len(y), max_steps)), np.empty((len(y), max_steps))
        constant = np.full(len(y), self.beta[0])
        if self.use_gex:
            constant += self.beta[2] * g
        future_return, cumulative = returns.copy(), np.zeros(len(y))
        for step in range(max_steps):
            future_return = constant + self.beta[1] * future_return
            cumulative += future_return
            mean[:, step] = cumulative
            shock_variance[:, step] = probabilities @ self.variances
            probabilities = probabilities @ self.transition
        # Future shocks have zero cross-covariance despite persistent regimes.
        # AR propagation changes each shock's weight in the cumulative return.
        weights = np.cumsum(self.beta[1] ** np.arange(max_steps)) ** 2
        variance = np.empty_like(mean)
        for step in range(max_steps):
            variance[:, step] = shock_variance[:, :step + 1] @ weights[:step + 1][::-1]
        return {"mean": mean, "variance": np.maximum(variance, 1e-8)}
