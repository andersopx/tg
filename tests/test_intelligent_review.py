import json
import tempfile
import unittest

from database import Database
from learner import Learner


class IntelligentReviewTests(unittest.TestCase):
    def test_review_records_asset_strategy_stats(self):
        with tempfile.NamedTemporaryFile() as f:
            db = Database(f.name)
            trade_id = db.insert_trade({
                "window_ts": 123,
                "asset": "BTC",
                "market_slug": "btc-updown-5m-123",
                "market_question": "BTC Up/Down",
                "token_id": "tok",
                "direction": "Down",
                "entry_price": 0.10,
                "size": 1.0,
                "shares": 10.0,
                "cost": 1.0,
                "filled_shares": 10.0,
                "filled_cost": 1.0,
                "avg_price": 0.10,
                "model_probability": 0.18,
                "edge_after_fees": 0.07,
                "reference_price": 100000,
                "signal_data": {
                    "strategy_name": "wick_rejection_odds",
                    "pattern": "wick_rejection_odds_down",
                    "model_probability": 0.18,
                    "edge_after_fees": 0.07,
                    "seconds_left": 120,
                    "indicators": {
                        "strategy_name": "wick_rejection_odds",
                        "distance_to_reference": 80,
                        "velocity_to_line": 45,
                        "upper_wick_body_ratio": 2.1,
                        "upper_wick_range_ratio": 0.45,
                    }
                }
            })
            db.resolve_trade(trade_id, "win", 10.0, 9.0, settlement_source="test", final_price=1.0)
            review = Learner(db).review_trade(db.get_trade_by_id(trade_id))
            self.assertIn("wick_rejection_odds", review)
            stats = db.get_asset_strategy_stats()
            self.assertEqual(stats[0]["asset"], "BTC")
            self.assertEqual(stats[0]["strategy_name"], "wick_rejection_odds")
            self.assertEqual(stats[0]["trades"], 1)
            reviews = db.get_recent_trade_reviews()
            self.assertEqual(reviews[0]["trade_id"], trade_id)


if __name__ == "__main__":
    unittest.main()
