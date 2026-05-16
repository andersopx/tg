from execution_quality_guard import ExecutionQualityGuard


def test_barrier_reclaim_custom_recheck_allows_thin_one_dollar_ticket():
    class PM:
        def get_orderbook_summary(self, token_id, max_price=None):
            return {
                "source": "polymarket_ws",
                "best_ask": 0.26,
                "best_bid": 0.25,
                "spread": 0.01,
                "age_sec": 0.05,
                "ask_depth_to_cap": 100,
                "weighted_avg_ask_to_cap": 0.338,
                "asks": [["0.26", "3.0"], ["0.34", "20.0"]],
            }

    guard = ExecutionQualityGuard(PM())
    res = guard.recheck_before_order(
        token_id="token-a",
        original_ob={"best_ask": 0.26},
        original_price=0.26,
        expected_cost=1.0,
        estimated_shares=3.0,
        max_token_price=0.45,
        max_spread=0.10,
        min_liquidity_multiplier=1.0,
        slippage_cap=0.22,
        max_weighted_avg_price=0.45,
        strategy_name="barrier_reclaim",
    )
    assert res.allowed


def test_barrier_reclaim_custom_recheck_blocks_excessive_weighted_avg():
    class PM:
        def get_orderbook_summary(self, token_id, max_price=None):
            return {
                "source": "polymarket_ws",
                "best_ask": 0.26,
                "best_bid": 0.25,
                "spread": 0.01,
                "age_sec": 0.05,
                "ask_depth_to_cap": 100,
                "weighted_avg_ask_to_cap": 0.62,
                "asks": [["0.62", "10.0"]],
            }

    guard = ExecutionQualityGuard(PM())
    res = guard.recheck_before_order(
        token_id="token-a",
        original_ob={"best_ask": 0.26},
        original_price=0.26,
        expected_cost=1.0,
        estimated_shares=3.0,
        max_token_price=0.45,
        max_spread=0.10,
        min_liquidity_multiplier=1.0,
        slippage_cap=0.50,
        max_weighted_avg_price=0.35,
        strategy_name="barrier_reclaim",
    )
    assert not res.allowed
    assert res.reason == "execution_weighted_avg_price_too_high"
