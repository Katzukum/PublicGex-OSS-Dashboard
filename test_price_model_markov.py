"""Synthetic recovery, causal filtering, and variance checks for the HMM."""

import itertools
import json
import unittest

import numpy as np

from research.price_models.markov import MarkovModel, _expectation


def _block(y, g=None):
    y = np.asarray(y, dtype=float)
    return {"symbol": "SYNTH", "session": "synthetic", "block": 0,
            "y": y, "g": np.zeros(len(y)) if g is None else np.asarray(g, dtype=float),
            "anchor": np.full(len(y), y[0]), "price_anchor": y.copy()}


def _synthetic_blocks(seed=23):
    rng = np.random.default_rng(seed)
    blocks = []
    for _ in range(10):
        n = 350
        g = rng.uniform(-1, 1, n)
        y = np.empty(n)
        y[0] = 1000
        state, current_return = int(rng.integers(2)), 0.0
        for t in range(1, n):
            if t > 1 and rng.random() > 0.94:
                state = 1 - state
            current_return = 0.2 * current_return + 0.8 * g[t - 1] + rng.normal(0, (0.7, 3.2)[state])
            y[t] = y[t - 1] + current_return
        blocks.append(_block(y, g))
    return blocks


class TestMarkovModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.blocks = _synthetic_blocks()
        cls.model = MarkovModel(True).fit(cls.blocks)

    def test_forward_backward_matches_enumerated_state_paths(self):
        residual = np.array([0.1, 2.0, -0.3, 0.8])
        variance = np.array([0.5, 4.0])
        transition = np.array([[0.8, 0.2], [0.3, 0.7]])
        initial = np.array([0.6, 0.4])
        likelihood, gamma, counts = _expectation(residual, variance, transition, initial)
        total, posterior, expected_counts = 0.0, np.zeros((4, 2)), np.zeros((2, 2))
        for path in itertools.product(range(2), repeat=4):
            weight = initial[path[0]]
            for t, state in enumerate(path):
                weight *= np.exp(-residual[t]**2 / (2 * variance[state])) / np.sqrt(2 * np.pi * variance[state])
                if t:
                    weight *= transition[path[t - 1], state]
            total += weight
            for t, state in enumerate(path):
                posterior[t, state] += weight
                if t:
                    expected_counts[path[t - 1], state] += weight
        self.assertAlmostEqual(likelihood, np.log(total), places=12)
        np.testing.assert_allclose(gamma, posterior / total, rtol=1e-12)
        np.testing.assert_allclose(counts, expected_counts / total, rtol=1e-12)

    def test_recovers_persistent_volatility_states_and_common_regression(self):
        model = self.model
        self.assertTrue(model.diagnostics["converged"])
        self.assertAlmostEqual(model.beta[1], 0.2, delta=0.06)
        self.assertAlmostEqual(model.beta[2], 0.8, delta=0.18)
        self.assertGreater(model.variances[1] / model.variances[0], 10)
        self.assertGreater(model.transition[0, 0], 0.85)
        self.assertGreater(model.transition[1, 1], 0.85)
        self.assertTrue(all(0.2 < p < 0.8 for p in model.diagnostics["training_state_occupancy"]))
        np.testing.assert_allclose(model.transition.sum(axis=1), 1)
        json.dumps(model.diagnostics, allow_nan=False)

    def test_future_price_and_gex_cannot_change_earlier_forecasts(self):
        block = self.blocks[0]
        expected = self.model.predict(block)
        changed = {key: value.copy() if isinstance(value, np.ndarray) else value for key, value in block.items()}
        changed["y"][130:] += np.arange(len(block["y"]) - 130) * 20
        changed["g"][130:] = -1
        actual = self.model.predict(changed)
        for key in expected:
            np.testing.assert_array_equal(actual[key][:130], expected[key][:130])
        self.assertTrue(np.all(np.isfinite(actual["variance"])))
        self.assertTrue(np.all(actual["variance"] > 0))

    def test_each_prediction_block_resets_and_other_fits_are_independent(self):
        baseline = self.model.predict(self.blocks[1])
        self.model.predict(self.blocks[0])
        other = MarkovModel(False).fit(self.blocks[:2])
        self.assertEqual(len(other.beta), 2)
        self.assertEqual(self.model.diagnostics["transitions"], 10 * 349)
        self.assertEqual(self.model.diagnostics["blocks"], 10)
        for key in baseline:
            np.testing.assert_array_equal(self.model.predict(self.blocks[1])[key], baseline[key])
        # Origin zero starts from the learned distribution, unaffected by the
        # last volatility state seen in a different block.
        next_probs = self.model._next_state_probabilities(np.array([0.0]), np.array([0.0]))
        np.testing.assert_array_equal(next_probs[0], self.model.initial)

    def test_cumulative_forecast_moments_match_independent_monte_carlo(self):
        model = self.model
        block = self.blocks[0]
        origin = 100
        result = model.predict(block)
        prefix = {key: value[:origin + 1] if isinstance(value, np.ndarray) else value for key, value in block.items()}
        returns = np.r_[0.0, np.diff(prefix["y"])]
        initial = model._next_state_probabilities(returns, prefix["g"])[-1]
        rng, size = np.random.default_rng(145), 150000
        state = (rng.random(size) >= initial[0]).astype(int)
        r = np.full(size, returns[-1])
        cumulative = np.zeros(size)
        constant = model.beta[0] + model.beta[2] * prefix["g"][-1]
        for step in range(6):
            r = constant + model.beta[1] * r + rng.normal(size=size) * np.sqrt(model.variances[state])
            cumulative += r
            self.assertAlmostEqual(np.mean(cumulative), result["mean"][origin, step], delta=0.06)
            self.assertAlmostEqual(np.var(cumulative), result["variance"][origin, step], delta=result["variance"][origin, step] * 0.035)
            state = (rng.random(size) >= model.transition[state, 0]).astype(int)


if __name__ == "__main__":
    unittest.main()
