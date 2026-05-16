import unittest

from strategy import PinBarStrategy
from config import config


class DummyFeed:
    pass


class OddsReversalTests(unittest.TestCase):
    def test_odds_reversal_generates_down_when_price_reverts_from_above(self):
        old_enabled = config.ODDS_REVERSAL_ENABLED
        try:
            config.ODDS_REVERSAL_ENABLED = True
            s = PinBarStrategy(DummyFeed(), asset="BTC")
            ctx = {
                "window_ts": 1770000000,
                "seconds_left": 120,
                "price": 100120.0,
                "reference_price": 100000.0,
                "distance": 120.0,
                "sigma_remaining": 80.0,
                "p_up": 0.86,
                "p_down": 0.14,
                "velocity_to_line": 60.0,
                "recent_change": -0.0007,
                "pin_kind": "none",
                "per_min_vol": 0.001,
                "remaining_min": 2.0,
                "drift": -40.0,
                "recent_change_2m": -0.0007,
            }
            sig = s._signal_odds_reversal(ctx)
            self.assertIsNotNone(sig)
            self.assertEqual(sig.direction, "DOWN")
            self.assertIn("odds_reversal", sig.pattern)
            self.assertEqual(sig.indicators["strategy_name"], "odds_reversal")
            self.assertAlmostEqual(sig.indicators["max_token_price"], config.ODDS_REVERSAL_MAX_TOKEN_PRICE)
        finally:
            config.ODDS_REVERSAL_ENABLED = old_enabled

    def test_odds_reversal_rejects_when_not_closing_toward_line(self):
        s = PinBarStrategy(DummyFeed(), asset="BTC")
        ctx = {
            "window_ts": 1770000000,
            "seconds_left": 120,
            "price": 100120.0,
            "reference_price": 100000.0,
            "distance": 120.0,
            "sigma_remaining": 80.0,
            "p_up": 0.86,
            "p_down": 0.14,
            "velocity_to_line": 0.0,
            "recent_change": 0.0007,
            "pin_kind": "none",
            "per_min_vol": 0.001,
            "remaining_min": 2.0,
            "drift": 40.0,
        }
        self.assertIsNone(s._signal_odds_reversal(ctx))


if __name__ == "__main__":
    unittest.main()
