import time

from config import config
from database import Database
from learner import Learner


def test_shadow_review_feeds_strategy_weight_used_before_real_samples(tmp_path):
    """Shadow settlements must review/learn before later real trading uses weights."""
    old = (
        config.ENABLE_INTELLIGENT_REVIEW,
        config.REVIEW_MIN_SAMPLES_FOR_WEIGHT,
        config.STRATEGY_WEIGHT_USE_SHADOW_DATA,
    )
    try:
        config.ENABLE_INTELLIGENT_REVIEW = False
        config.REVIEW_MIN_SAMPLES_FOR_WEIGHT = 2
        config.STRATEGY_WEIGHT_USE_SHADOW_DATA = True

        db = Database(str(tmp_path / "shadow_learning.db"))
        learner = Learner(db)
        now = int(time.time())
        signal_data = {
            "strategy_name": "shadow_strategy",
            "pattern": "shadow_strategy",
            "model_probability": 0.82,
            "edge_after_fees": 0.18,
            "reference_price": 100.0,
            "indicators": {"strategy_name": "shadow_strategy", "asset": "BTC"},
        }

        for i in range(2):
            trade_id = db.insert_trade({
                "timestamp": now + i,
                "window_ts": now - 300 + i,
                "asset": "BTC",
                "timeframe": "5m",
                "market_slug": f"shadow-{i}",
                "token_id": f"token-{i}",
                "direction": "Up",
                "entry_price": 0.50,
                "size": 1.0,
                "shares": 2.0,
                "cost": 1.0,
                "filled_shares": 2.0,
                "filled_cost": 1.0,
                "avg_price": 0.50,
                "model_probability": 0.82,
                "edge_after_fees": 0.18,
                "reference_price": 100.0,
                "is_shadow": 1,
                "shadow_reason": "test_shadow_learning",
                "signal_data": signal_data,
                "status": "shadow_open",
            })
            db.resolve_trade(
                trade_id,
                "win",
                payout=2.0,
                pnl=1.0,
                status="shadow_resolved",
                settlement_source="test",
                final_price=1.0,
            )
            review = learner.review_trade(db.get_trade_by_id(trade_id))
            assert review

        stats = db.get_asset_strategy_stats()
        row = next(r for r in stats if r["asset"] == "BTC" and r["strategy_name"] == "shadow_strategy")
        assert row["trades"] == 0
        assert row["shadow_trades"] == 2
        assert row["shadow_wins"] == 2
        assert row["shadow_pnl"] == 2.0

        # The same lookup used by Strategy during live signal scoring now sees Shadow data.
        assert db.get_asset_strategy_weight("BTC", "shadow_strategy") > 1.0
    finally:
        (
            config.ENABLE_INTELLIGENT_REVIEW,
            config.REVIEW_MIN_SAMPLES_FOR_WEIGHT,
            config.STRATEGY_WEIGHT_USE_SHADOW_DATA,
        ) = old
