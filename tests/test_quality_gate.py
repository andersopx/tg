import unittest
from types import SimpleNamespace

from adaptive_quality_gate import AdaptiveQualityGate
from opportunity_scorer import OpportunityScorer


class FakeDB:
    def get_today_trade_count(self):
        return 0
    def get_today_realized_pnl(self):
        return 0.0
    def get_consecutive_losses(self):
        return 0
    def get_asset_strategy_stats(self):
        return []


class QualityGateTests(unittest.TestCase):
    def make_signal(self, strategy="odds_lag", prob=0.22):
        return SimpleNamespace(
            pattern=strategy + "_down",
            direction="DOWN",
            asset="BTC",
            timeframe="5m",
            target_probability=prob,
            sigma_remaining=50.0,
            distance_to_reference=80.0,
            indicators={"strategy_name": strategy, "velocity_to_line": 30.0},
        )

    def test_odds_ev_scores_high_multiplier_signal(self):
        scorer = OpportunityScorer(FakeDB())
        sig = self.make_signal("odds_lag", 0.22)
        score = scorer.score(
            signal=sig,
            token_price=0.10,
            ob={"best_ask": 0.10, "best_bid": 0.09, "spread": 0.01, "ask_depth_to_cap": 20, "weighted_avg_ask_to_cap": 0.10},
            opp_ob={"best_ask": 0.88, "best_bid": 0.86, "spread": 0.02},
            model_prob=0.22,
            edge_after_fees=0.10,
            fee_per_share=0.0063,
            expected_cost=1.0,
        )
        self.assertGreater(score.expected_value, 0.8)
        self.assertGreater(score.final_score, 65)

    def test_quality_gate_rejects_bad_spread(self):
        scorer = OpportunityScorer(FakeDB())
        sig = self.make_signal("odds_lag", 0.22)
        score = scorer.score(
            signal=sig,
            token_price=0.10,
            ob={"best_ask": 0.10, "best_bid": 0.01, "spread": 0.09, "ask_depth_to_cap": 20, "weighted_avg_ask_to_cap": 0.10},
            opp_ob={"best_ask": 0.88, "best_bid": 0.86, "spread": 0.02},
            model_prob=0.22,
            edge_after_fees=0.10,
            fee_per_share=0.0063,
            expected_cost=1.0,
        )
        gate = AdaptiveQualityGate(FakeDB()).evaluate(score_obj=score, signal=sig)
        self.assertFalse(gate.allowed)
        self.assertEqual(gate.reason, "quality_spread_too_wide")

    def test_quality_gate_allows_good_odds_signal(self):
        scorer = OpportunityScorer(FakeDB())
        sig = self.make_signal("odds_lag", 0.25)
        score = scorer.score(
            signal=sig,
            token_price=0.09,
            ob={"best_ask": 0.09, "best_bid": 0.08, "spread": 0.01, "ask_depth_to_cap": 30, "weighted_avg_ask_to_cap": 0.09},
            opp_ob={"best_ask": 0.90, "best_bid": 0.88, "spread": 0.02},
            model_prob=0.25,
            edge_after_fees=0.14,
            fee_per_share=0.006,
            expected_cost=1.0,
        )
        gate = AdaptiveQualityGate(FakeDB()).evaluate(score_obj=score, signal=sig)
        self.assertTrue(gate.allowed, gate)


if __name__ == "__main__":
    unittest.main()
