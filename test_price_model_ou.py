"""Behavioral checks for the research-only conditional OU forecaster."""

import json
import unittest

import numpy as np

from research.price_models.ou import OUModel


def _block(y, anchor=1000.0):
    y = np.asarray(y, dtype=float)
    trailing = np.array([np.mean(y[max(0, i - 5):i + 1]) for i in range(len(y))])
    return {"symbol": "SYNTH", "session": "synthetic", "block": 0,
            "y": y, "g": np.zeros(len(y)),
            "anchor": np.full(len(y), anchor), "price_anchor": trailing}


class TestOUModel(unittest.TestCase):
    def test_recovers_mean_reversion_and_exact_horizon_moments(self):
        rng = np.random.default_rng(901)
        blocks = []
        alpha, noise = 0.24, 1.7
        for index in range(12):
            y = np.empty(350)
            y[0] = 980 + index * 3
            for t in range(1, len(y)):
                y[t] = y[t - 1] + alpha * (1000 - y[t - 1]) + rng.normal(0, noise)
            blocks.append(_block(y))
        model = OUModel(True).fit(blocks)
        self.assertAlmostEqual(model.alpha, alpha, delta=0.025)
        self.assertAlmostEqual(model.innovation_variance, noise**2, delta=0.3)
        prediction = model.predict(_block([1010]))
        h = np.arange(1, 7)
        expected_mean = -10 * (1 - (1 - model.alpha)**h)
        expected_variance = model.innovation_variance * (1 - (1 - model.alpha)**(2 * h)) / (1 - (1 - model.alpha)**2)
        np.testing.assert_allclose(prediction["mean"][0], expected_mean)
        np.testing.assert_allclose(prediction["variance"][0], expected_variance)
        json.dumps(model.diagnostics, allow_nan=False)

    def test_does_not_force_attraction_when_data_moves_away(self):
        block = _block(np.arange(1001, 1101, dtype=float))
        model = OUModel(True).fit([block])
        self.assertEqual(model.alpha, 0)
        self.assertTrue(model.diagnostics["zero_attraction"])
        self.assertIsNone(model.diagnostics["half_life_minutes"])
        result = model.predict(block)
        np.testing.assert_array_equal(result["mean"], 0)
        np.testing.assert_allclose(result["variance"][0], np.arange(1, 7))

    def test_prediction_is_causal_and_fit_is_independent(self):
        rng = np.random.default_rng(123)
        block = _block(1000 + rng.normal(0, 3, 150))
        model = OUModel(False).fit([block])
        baseline = model.predict(block)
        changed = {key: value.copy() if isinstance(value, np.ndarray) else value for key, value in block.items()}
        changed["y"][70:] += 100
        changed["price_anchor"][70:] -= 100
        changed["anchor"][70:] += 300
        changed["g"][70:] = 1
        for key, expected in baseline.items():
            np.testing.assert_array_equal(model.predict(changed)[key][:70], expected[:70])
        # Other forecasts/fits cannot mutate the first model's state.
        model.predict(changed)
        OUModel(True).fit([changed])
        for key in baseline:
            np.testing.assert_array_equal(model.predict(block)[key], baseline[key])
        self.assertTrue(np.all(np.isfinite(baseline["variance"])))
        self.assertTrue(np.all(baseline["variance"] > 0))

    def test_fitting_does_not_bridge_disjoint_blocks(self):
        left, right = _block([999, 1000, 1001]), _block([2000, 2000, 2000], anchor=2000)
        model = OUModel(True).fit([left, right])
        self.assertEqual(model.diagnostics["transitions"], 4)
        self.assertEqual(model.diagnostics["blocks"], 2)
        self.assertEqual(model.alpha, 1 - 1e-6)


if __name__ == "__main__":
    unittest.main()
