import os
import tempfile
from types import SimpleNamespace

from database import Database
from product_doctor import ProductDoctor
from dynamic_tick_guard import DynamicTickGuard
from execution_quality_guard import ExecutionQualityGuard
from realtime_orderbook import RealtimeOrderbookCache


def test_dynamic_tick_guard_prefers_fresh_ws_tick_size():
    cache = RealtimeOrderbookCache()
    cache.apply_ws_message({
        "event_type": "tick_size_change",
        "asset_id": "token-a",
        "new_tick_size": "0.001",
        "timestamp": "1766789469958",
    })
    guard = DynamicTickGuard(cache)
    decision = guard.prepare_price("token-a", {"tick_size": 0.01}, 0.1234)
    assert decision.tick_size == 0.001
    assert decision.source == "ws_tick_size_change"
    assert decision.rounded_price == 0.123


def test_execution_quality_guard_blocks_price_jump():
    class PM:
        def get_orderbook_summary(self, token_id, max_price=None):
            return {
                "source": "polymarket_ws",
                "best_ask": 0.13,
                "best_bid": 0.12,
                "spread": 0.01,
                "age_sec": 0.1,
                "ask_depth_to_cap": 100,
                "weighted_avg_ask_to_cap": 0.13,
            }
    guard = ExecutionQualityGuard(PM())
    res = guard.recheck_before_order(
        token_id="token-a",
        original_ob={"best_ask": 0.10},
        original_price=0.10,
        expected_cost=1.0,
        estimated_shares=10.0,
        max_token_price=0.20,
        max_spread=0.05,
        min_liquidity_multiplier=1.0,
    )
    assert not res.allowed
    assert res.reason == "execution_price_moved_up"


def test_product_doctor_reports_execution_cluster_without_api_touch():
    with tempfile.TemporaryDirectory() as d:
        db = Database(os.path.join(d, "t.db"))
        for _ in range(3):
            db.record_execution_event(stage="execution_recheck", reason="execution_price_moved_up", details={})
        doctor = ProductDoctor(db, market_ws=None)
        report = doctor.diagnose(lookback_sec=3600)
        codes = [i.code for i in report.issues]
        assert "execution_quality_cluster" in codes
