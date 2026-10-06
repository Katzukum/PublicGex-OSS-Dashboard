"""Small, causal GARCH / GARCH-X benchmark for the price-path experiment.

The mean is deliberately zero in both variants.  Gamma changes the positive
variance intercept, not the mean or an assumed direction of dealer hedging::

    h[t+1] = omega * exp(theta * g[t]) + alpha * r[t]**2 + beta * h[t]

Inputs are equally spaced, gap-free blocks.  A new block resets the filter to a
variance estimated on the training data.  Multi-step forecasts freeze the gamma
known at the origin; they are conditional scenarios, not forecasts of gamma.
"""

from __future__ import annotations

import math

import numpy as np
from scipy.optimize import minimize
from scipy.signal import lfilter


class GarchModel:
    """Zero-mean Gaussian GARCH(1,1), optionally with a gamma variance term."""

    _MAX_PERSISTENCE = 0.995
    _THETA_BOUND = 3.0

    def __init__(self, use_gex: bool):
        self.use_gex = bool(use_gex)
        self.diagnostics: dict = {"fitted": False, "use_gex": self.use_gex}
        self._parameters = None
        self._scale_variance = None

    def _arrays(self, block):
        y = np.asarray(block["y"], dtype=float)
        if y.ndim != 1 or not np.all(np.isfinite(y)):
            raise ValueError("Block y must be a finite one-dimensional array")
        if self.use_gex:
            g = np.asarray(block["g"], dtype=float)
            if g.shape != y.shape or not np.all(np.isfinite(g)):
                raise ValueError("Block g must be finite and have the same shape as y")
            if np.any(np.abs(g) > 1.0 + 1e-10):
                raise ValueError("Normalized gamma must lie in [-1, 1]")
        else:
            g = np.zeros_like(y)
        return y, g

    @staticmethod
    def _unpack(parameters):
        log_omega, persistence, alpha_share = parameters[:3]
        theta = parameters[3] if len(parameters) == 4 else 0.0
        return (
            float(np.exp(log_omega)),
            float(persistence * alpha_share),
            float(persistence * (1.0 - alpha_share)),
            float(theta),
        )

    @staticmethod
    def _filter_variance(returns, lagged_g, omega, alpha, beta, theta):
        """Variance of each observed return, with independent row/block resets.

        Returns are normalized by training RMS.  The presample variance and the
        expected squared presample shock are both 1, avoiding a fabricated zero
        shock at each session boundary.  Padded tails never enter the likelihood.
        """
        forcing = omega * np.exp(theta * lagged_g)
        forcing[:, 0] += alpha
        forcing[:, 1:] += alpha * returns[:, :-1] ** 2
        initial = np.full((returns.shape[0], 1), beta, dtype=float)
        variance, _ = lfilter([1.0], [1.0, -beta], forcing, axis=1, zi=initial)
        return variance

    def fit(self, blocks):
        self._parameters = None
        self._scale_variance = None
        self.diagnostics = {"fitted": False, "use_gex": self.use_gex}
        prepared = []
        supplied_blocks = 0
        for block in blocks:
            supplied_blocks += 1
            y, g = self._arrays(block)
            if len(y) >= 2:
                prepared.append((np.diff(y), g[:-1]))
        count = sum(len(returns) for returns, _ in prepared)
        if count < 20:
            raise ValueError("GARCH fitting requires at least 20 observed returns")
        scale_variance = sum(float(returns @ returns) for returns, _ in prepared) / count
        if not np.isfinite(scale_variance) or scale_variance <= 1e-12:
            raise ValueError("GARCH fitting requires nonzero finite training return variance")

        shape = (len(prepared), max(len(returns) for returns, _ in prepared))
        returns = np.zeros(shape, dtype=float)
        lagged_g = np.zeros(shape, dtype=float)
        mask = np.zeros(shape, dtype=bool)
        for index, (block_returns, block_g) in enumerate(prepared):
            length = len(block_returns)
            returns[index, :length] = block_returns / math.sqrt(scale_variance)
            lagged_g[index, :length] = block_g
            mask[index, :length] = True
        squared = returns[mask] ** 2

        def objective(parameters):
            omega, alpha, beta, theta = self._unpack(parameters)
            variance = self._filter_variance(returns, lagged_g, omega, alpha, beta, theta)[mask]
            if np.any(variance <= 0) or not np.all(np.isfinite(variance)):
                return float("inf")
            return float(0.5 * np.mean(np.log(variance) + squared / variance))

        # This parameterization guarantees alpha >= 0, beta >= 0 and
        # alpha + beta < 1 for every optimizer evaluation.
        bounds = [(math.log(1e-6), math.log(10.0)), (0.0, self._MAX_PERSISTENCE), (0.001, 0.999)]
        if self.use_gex:
            bounds.append((-self._THETA_BOUND, self._THETA_BOUND))
        starts = []
        for persistence, share, theta in ((0.90, 0.08, 0.0), (0.65, 0.30, 0.75), (0.25, 0.70, -0.75)):
            start = [math.log(1.0 - persistence), persistence, share]
            if self.use_gex:
                start.append(theta)
            starts.append(np.asarray(start, dtype=float))

        attempts = []
        successful = []
        for start in starts:
            try:
                result = minimize(
                    objective,
                    start,
                    method="L-BFGS-B",
                    bounds=bounds,
                    options={"maxiter": 350, "ftol": 1e-10, "gtol": 1e-6, "maxls": 40},
                )
                valid = bool(result.success and np.isfinite(result.fun) and np.all(np.isfinite(result.x)))
                attempt = {
                    "success": valid,
                    "status": int(result.status),
                    "message": str(result.message),
                    "iterations": int(result.nit),
                    "function_evaluations": int(result.nfev),
                    "objective": float(result.fun) if np.isfinite(result.fun) else None,
                    "parameters": [float(value) for value in result.x],
                    "start": [float(value) for value in start],
                }
                if valid:
                    successful.append((float(result.fun), len(attempts), result.x.copy()))
            except (FloatingPointError, ValueError, RuntimeError) as error:
                attempt = {"success": False, "message": str(error), "start": start.tolist()}
            attempts.append(attempt)

        self.diagnostics.update({
            "model": "GARCH-X(1,1)" if self.use_gex else "GARCH(1,1)",
            "mean": "zero",
            "training_returns": count,
            "training_blocks": len(prepared),
            "ignored_short_blocks": supplied_blocks - len(prepared),
            "initial_variance_bp2": float(scale_variance),
            "optimizer": "L-BFGS-B",
            "parameterization": ["log_omega_scaled", "alpha_plus_beta", "alpha_share"] + (["theta"] if self.use_gex else []),
            "bounds": [[float(low), float(high)] for low, high in bounds],
            "optimizer_attempts": attempts,
            "future_gex": "frozen at forecast origin" if self.use_gex else "unused",
            "block_initialization": "training-only mean squared return; expected presample shock",
            "distribution": "Gaussian conditional moments; empirical coverage must be checked",
        })
        if not successful:
            raise RuntimeError("Every GARCH optimization start failed; inspect model.diagnostics")

        value, selected, parameters = min(successful, key=lambda item: item[0])
        omega, alpha, beta, theta = self._unpack(parameters)
        names = self.diagnostics["parameterization"]
        boundaries = [
            names[index]
            for index, (parameter, (low, high)) in enumerate(zip(parameters, bounds))
            if min(abs(parameter - low), abs(parameter - high)) <= 1e-4 * max(1.0, high - low)
        ]
        self._parameters = (omega, alpha, beta, theta)
        self._scale_variance = scale_variance
        self.diagnostics.update({
            "fitted": True,
            "converged": True,
            "selected_attempt": selected,
            "objective": value,
            "omega_bp2": float(omega * scale_variance),
            "alpha": alpha,
            "beta": beta,
            "persistence": alpha + beta,
            "theta": theta,
            "parameters_at_bounds": boundaries,
            "warnings": (["One or more fitted parameters are at an optimizer bound"] if boundaries else []),
        })
        return self

    def predict(self, block, max_steps=6):
        if self._parameters is None:
            raise RuntimeError("Fit GarchModel before predicting")
        if isinstance(max_steps, bool) or int(max_steps) != max_steps or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        max_steps = int(max_steps)
        y, g = self._arrays(block)
        size = len(y)
        mean = np.zeros((size, max_steps), dtype=float)
        variance = np.empty_like(mean)
        if size == 0:
            return {"mean": mean, "variance": variance}

        omega, alpha, beta, theta = self._parameters
        returns = np.diff(y) / math.sqrt(self._scale_variance)
        intercept = omega * np.exp(theta * g)
        next_variance = np.empty(size, dtype=float)
        next_variance[0] = intercept[0] + alpha + beta
        if size > 1:
            observed_variance = self._filter_variance(
                returns[None, :], g[None, :-1], omega, alpha, beta, theta,
            )[0]
            next_variance[1:] = intercept[1:] + alpha * returns ** 2 + beta * observed_variance

        # Conditional return covariances are zero under the zero-mean model, so
        # cumulative log-return variance is the sum of expected future variances.
        cumulative = np.zeros(size, dtype=float)
        for step in range(max_steps):
            cumulative += next_variance
            variance[:, step] = cumulative * self._scale_variance
            next_variance = intercept + (alpha + beta) * next_variance
        if not np.all(np.isfinite(variance)) or np.any(variance <= 0):
            raise FloatingPointError("GARCH produced invalid forecast variances")
        return {"mean": mean, "variance": variance}
