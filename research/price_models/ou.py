"""Small, causal, anchor-conditioned Ornstein-Uhlenbeck research model.

Time is measured in five-minute steps and prices in log-price basis points.
The anchor is held fixed over each forecast.  A fitted strength of zero is
valid: the data need not support attraction to the supplied gamma level.
"""

from __future__ import annotations

import numpy as np


class OUModel:
    def __init__(self, use_gex: bool):
        self.use_gex = bool(use_gex)
        self.diagnostics = {}
        self._fitted = False

    def _anchor(self, block):
        y = np.asarray(block["y"], dtype=float)
        name = "anchor" if self.use_gex else "price_anchor"
        anchor = np.asarray(block[name], dtype=float)
        if anchor.shape != y.shape:
            raise ValueError(f"{name} and y must have identical shapes")
        # An unavailable level supplies no attraction, rather than a fabricated
        # gamma target.  This rule is also applied to the training transitions.
        return np.where(np.isfinite(anchor), anchor, y)

    def fit(self, blocks):
        offsets, changes = [], []
        block_count = 0
        missing_anchors = 0
        for block in blocks:
            y = np.asarray(block["y"], dtype=float)
            if y.ndim != 1 or not np.all(np.isfinite(y)):
                raise ValueError("Each y must be a finite one-dimensional array")
            anchor = self._anchor(block)
            name = "anchor" if self.use_gex else "price_anchor"
            missing_anchors += int(np.count_nonzero(~np.isfinite(block[name])))
            if len(y) < 2:
                continue
            offsets.append(anchor[:-1] - y[:-1])
            changes.append(np.diff(y))
            block_count += 1
        if not changes:
            raise ValueError("OUModel requires at least one within-block transition")
        x = np.concatenate(offsets)
        dy = np.concatenate(changes)
        denominator = float(x @ x)
        unconstrained = float(x @ dy / denominator) if denominator > 1e-12 else 0.0
        # alpha = 1 - exp(-kappa): exact OU discretization with a known anchor.
        self.alpha = float(np.clip(unconstrained, 0.0, 1.0 - 1e-6))
        self.kappa = float(-np.log1p(-self.alpha))
        residual = dy - self.alpha * x
        self.innovation_variance = max(float(np.mean(residual**2)), 1e-8)
        if self.kappa > 1e-10:
            self.diffusion_variance = float(
                self.innovation_variance * 2 * self.kappa
                / (-np.expm1(-2 * self.kappa))
            )
        else:
            self.diffusion_variance = self.innovation_variance
        self.diagnostics = {
            "model": "conditional_ou",
            "use_gex": self.use_gex,
            "anchor": "gamma_wall_anchor" if self.use_gex else "causal_trailing_price_mean",
            "transitions": int(len(dy)),
            "blocks": block_count,
            "missing_anchor_rows": missing_anchors,
            "alpha_per_step": self.alpha,
            "unconstrained_alpha": unconstrained,
            "kappa_per_5_minutes": self.kappa,
            "half_life_minutes": float(5 * np.log(2) / self.kappa) if self.kappa > 0 else None,
            "zero_attraction": bool(self.alpha == 0),
            "upper_strength_bound": bool(self.alpha >= 1 - 1e-6),
            "innovation_variance_bps2": self.innovation_variance,
            "diffusion_variance_bps2_per_step": self.diffusion_variance,
            "forecast_assumption": "Hold the origin's anchor fixed; conditional on fitted parameters.",
        }
        self._fitted = True
        return self

    def predict(self, block, max_steps=6):
        if not self._fitted:
            raise RuntimeError("Fit OUModel before predicting")
        if int(max_steps) != max_steps or max_steps < 1:
            raise ValueError("max_steps must be a positive integer")
        max_steps = int(max_steps)
        y = np.asarray(block["y"], dtype=float)
        if y.ndim != 1 or not np.all(np.isfinite(y)):
            raise ValueError("Each y must be a finite one-dimensional array")
        anchor = self._anchor(block)
        horizon = np.arange(1, max_steps + 1, dtype=float)
        response = -np.expm1(-self.kappa * horizon)
        mean = (anchor - y)[:, None] * response[None, :]
        if self.kappa > 1e-10:
            variance = self.innovation_variance * (
                -np.expm1(-2 * self.kappa * horizon)
                / -np.expm1(-2 * self.kappa)
            )
        else:
            variance = self.innovation_variance * horizon
        return {
            "mean": mean,
            "variance": np.broadcast_to(variance, mean.shape).copy(),
        }
