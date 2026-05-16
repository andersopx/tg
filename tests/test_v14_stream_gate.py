import os
import tempfile
import unittest
from types import SimpleNamespace

from adaptive_quality_gate import AdaptiveQualityGate
from database import Database
from config import config


class FakeFlowDB:
    def __init__(self, flow):
        self.flow = flow
    def get_today_trade_count(self):
        return 0
    def get_today_realized_pnl(self):
        return 0.0
    def get_consecutive_losses(self):
        return 0
    def get_recent_decision_flow_stats(self, lookback_sec=3600, limit=1000):
        return dict(self.flow)
    def get_asset_strategy_stats(self):
        return []


class V14StreamGateTests(unittest.TestCase):
    def make_score(self, final=76.0, prob=0.25, ev=0.80, category="odds"):
        return SimpleNamespace(
            final_score=final,
            estimated_win_prob=prob,
            expected_value=ev,
            category=category,
            hard_reason="",
            to_dict=lambda: {
                "final_score": final,
                "estimated_win_prob": prob,
                "expected_value": ev,
                "category": category,
            },
        )

    def test_database_recent_decision_flow_stats_reads_scores(self):
        with tempfile.TemporaryDirectory() as d:
            db = Database(os.path.join(d, "t.db"))
            for score in [60, 70, 80, 90]:
                db.record_decision(
                    window_ts=1,
                    action="skip",
                    reason="quality_score_below_dynamic_threshold",
                    details={"score": score, "threshold": 75},
                )
            db.record_decision(window_ts=1, action="quality_pass", reason="quality_gate_pass", details={"score": 88})
            stats = db.get_recent_decision_flow_stats(3600)
            self.assertEqual(stats["recent_decisions"], 5)
            self.assertEqual(stats["quality_pass"], 1)
            self.assertGreaterEqual(stats["score_p75"], 80)

    def test_crowded_stream_raises_threshold_without_top_list(self):
        old_min = config.FLOW_MIN_RECENT_DECISIONS
        old_high = config.FLOW_HIGH_CANDIDATES_PER_MIN
        old_very = config.FLOW_VERY_HIGH_CANDIDATES_PER_MIN
        try:
            config.FLOW_MIN_RECENT_DECISIONS = 10
            config.FLOW_HIGH_CANDIDATES_PER_MIN = 1.0
            config.FLOW_VERY_HIGH_CANDIDATES_PER_MIN = 3.0
            flow = {
                "lookback_sec": 3600,
                "recent_decisions": 240,
                "candidates_per_min": 4.0,
                "quality_pass": 80,
                "quality_pass_ratio": 0.3333,
                "orders": 0,
                "score_p75": 83.0,
            }
            gate = AdaptiveQualityGate(FakeFlowDB(flow)).evaluate(score_obj=self.make_score(final=76.0))
            self.assertFalse(gate.allowed)
            self.assertEqual(gate.reason, "quality_score_below_dynamic_threshold")
            self.assertGreater(gate.threshold, config.BASE_QUALITY_SCORE)
            self.assertEqual(gate.details["flow"]["reason"], "stream_pressure")
        finally:
            config.FLOW_MIN_RECENT_DECISIONS = old_min
            config.FLOW_HIGH_CANDIDATES_PER_MIN = old_high
            config.FLOW_VERY_HIGH_CANDIDATES_PER_MIN = old_very



if __name__ == "__main__":
    unittest.main()
