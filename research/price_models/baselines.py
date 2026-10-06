"""Fixed, deliberately simple price-only benchmarks."""
import numpy as np


class Baseline:
    def __init__(self, momentum=False):
        self.momentum = momentum

    def fit(self, blocks):
        returns = np.concatenate([np.diff(b['y']) for b in blocks])
        self.initial_variance = max(float(np.mean(returns**2)), 1e-6)
        self.diagnostics = {'initial_variance': self.initial_variance, 'ewma_decay': 0.94}
        return self

    def predict(self, block, max_steps=6):
        y = np.asarray(block['y'])
        returns = np.diff(y)
        n = len(y)
        mean, variance = np.zeros((n, max_steps)), np.zeros((n, max_steps))
        state_var = self.initial_variance
        for i in range(n):
            if i:
                state_var = 0.94 * state_var + 0.06 * returns[i - 1]**2
            drift = float(np.mean(returns[max(0, i - 3):i])) if i and self.momentum else 0.0
            for h in range(1, max_steps + 1):
                # Fixed damping prevents assuming a 15-minute trend persists indefinitely.
                mean[i, h - 1] = drift * sum(0.8**j for j in range(h))
                variance[i, h - 1] = max(state_var, 1e-6) * h
        return {'mean': mean, 'variance': variance}
