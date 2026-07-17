import unittest

from gex_levels import aggregate_gamma_levels, level_net_gex, level_side_gex
from ninjatrader_broadcaster import (
    _dashboard_payload_for_symbol,
    _local_net_gex,
    _nearest_significant,
    _ninjatrader_levels_for_symbol,
)


class GammaLevelTests(unittest.TestCase):
    def test_aggregate_keeps_call_put_and_net_gex(self):
        rows = [
            {"strike_price": 100, "option_type": "CALL", "gex_value": 500, "open_interest": 10},
            {"strike_price": 100, "option_type": "PUT", "gex_value": -450, "open_interest": 20},
            {"strike_price": 100, "option_type": "CALL", "gex_value": 0, "open_interest": 7},
            {"strike_price": 101, "option_type": "CALL", "gex_value": 30, "open_interest": 3},
        ]

        levels = aggregate_gamma_levels(rows, spot=100, per_side=None)
        level = next(item for item in levels if item["strike"] == 100)

        self.assertEqual(level["call_gex"], 500)
        self.assertEqual(level["put_gex"], -450)
        self.assertEqual(level["net_gex"], 50)
        self.assertEqual(level["gex"], 50)
        self.assertEqual(level["gross_gex"], 950)
        self.assertEqual(level["open_interest"], 37)

    def test_per_side_selection_uses_side_strength(self):
        rows = [
            {"strike_price": 99, "option_type": "PUT", "gex_value": -200, "open_interest": 1},
            {"strike_price": 101, "option_type": "CALL", "gex_value": 500, "open_interest": 1},
            {"strike_price": 101, "option_type": "PUT", "gex_value": -499, "open_interest": 1},
            {"strike_price": 102, "option_type": "CALL", "gex_value": 30, "open_interest": 1},
        ]

        levels = aggregate_gamma_levels(rows, spot=100, per_side=1)
        strikes = [level["strike"] for level in levels]

        self.assertEqual(strikes, [99, 101])

    def test_nearest_significant_uses_side_specific_gex(self):
        levels = [
            {"strike": 101, "gex": -5, "net_gex": -5, "call_gex": 400, "put_gex": -405},
            {"strike": 102, "gex": 80, "net_gex": 80, "call_gex": 80, "put_gex": 0},
        ]

        call_wall = _nearest_significant(levels, 100, "above", 1)
        put_pressure = _nearest_significant(levels, 100, "above", -1)

        self.assertEqual(call_wall["strike"], 101)
        self.assertEqual(put_pressure["strike"], 101)

    def test_level_helpers_keep_net_separate_from_sides(self):
        level = {"gex": 999, "net_gex": -5, "call_gex": 400, "put_gex": -405}

        self.assertEqual(level_net_gex(level), -5)
        self.assertEqual(level_side_gex(level, 1), 400)
        self.assertEqual(level_side_gex(level, -1), -405)

    def test_local_net_gex_uses_net_not_gross_sides(self):
        levels = [
            {"strike": 100, "gex": 999, "net_gex": -5, "call_gex": 400, "put_gex": -405},
            {"strike": 103, "gex": 30, "net_gex": 30, "call_gex": 30, "put_gex": 0},
        ]

        self.assertEqual(_local_net_gex({}, levels, 100), -5)

    def test_ninjatrader_levels_include_hollow_non_key_levels(self):
        overview = {
            "gamma_levels": {
                "NDX": [
                    {"strike": 100, "gex": 50},
                    {"strike": 102, "gex": -80},
                ]
            },
            "cockpit_levels": {
                "NDX": [
                    {"strike": 99, "gex": 10},
                    {"strike": 100, "gex": 50},
                    {"strike": 101, "gex": -20},
                    {"strike": 102, "gex": -80},
                ]
            },
        }

        levels = _ninjatrader_levels_for_symbol(overview, "NDX")

        self.assertEqual([level["strike"] for level in levels], [99, 100, 101, 102])
        self.assertEqual([level["is_key_level"] for level in levels], [False, True, False, True])
        self.assertNotIn("is_key_level", overview["cockpit_levels"]["NDX"][0])

    def test_ninjatrader_dashboard_signal_uses_explicit_key_levels_only(self):
        overview = {
            "compass": {"label": "NEUTRAL", "y_score": 0, "confidence": 1},
            "components": [
                {
                    "symbol": "NDX",
                    "spot": 100,
                    "flip_strike": 100,
                    "effective_gex": 0,
                    "confidence": 1,
                }
            ],
            "gamma_levels": {"NDX": []},
            "cockpit_levels": {
                "NDX": [
                    {"strike": 101, "gex": 500, "call_gex": 500, "put_gex": 0},
                ]
            },
        }

        dashboard = _dashboard_payload_for_symbol("NDX", overview)

        self.assertEqual(dashboard["dashboard_liquidity_detail"], "No liquidity profile")


if __name__ == "__main__":
    unittest.main()
