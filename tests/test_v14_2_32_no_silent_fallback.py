import pytest

from trader import Trader
from execution_quality_guard import ExecutionQualityGuard
from config import config


def test_weighted_avg_partial_depth_does_not_fallback_to_best_ask():
    order_avg = Trader._order_weighted_avg_from_ob(
        {"best_ask": 0.13, "weighted_avg_ask_to_cap": 0.62, "asks": [["0.13", "1.0"]]},
        shares=7.69,
        max_price=0.63,
    )
    ok, reason, details = Trader._order_avg_gate_status(order_avg, 7.69, best_ask=0.13)

    assert not ok
    assert reason == "orderbook_depth_insufficient"
    assert details["weighted_avg"] is None
    assert details["partial_weighted_avg"] == pytest.approx(0.13)
    assert details["filled_shares"] == pytest.approx(1.0)
    assert details["target_shares"] == pytest.approx(7.69)


def test_weighted_avg_no_ask_inside_cap_skips_without_token_price_fallback():
    order_avg = Trader._order_weighted_avg_from_ob(
        {"best_ask": 0.13, "weighted_avg_ask_to_cap": 0.62, "asks": [["0.70", "20.0"]]},
        shares=7.69,
        max_price=0.63,
    )
    ok, reason, details = Trader._order_avg_gate_status(order_avg, 7.69, best_ask=0.13)

    assert not ok
    assert reason == "orderbook_too_thin_no_fill"
    assert details["weighted_avg"] is None
    assert details["filled_shares"] == 0.0


def test_opposite_book_missing_best_ask_is_explicit_skip_path():
    trader_source = open("trader.py", encoding="utf-8").read()

    assert "opposite_orderbook_missing_after_check" in trader_source
    assert 'float(opp_ob.get("best_ask") or 0)' not in trader_source
    assert 'opp_best_ask = float(opp_ob.get("best_ask"))' in trader_source


def test_risk_module_exception_is_not_silently_converted_to_zero(monkeypatch):
    class BadRisk:
        def available_trading_equity(self, balance):
            raise ValueError("boom")

    trader = Trader.__new__(Trader)
    trader.risk = BadRisk()
    monkeypatch.setattr(config, "AUTHORIZED_CAPITAL_USD", 10.0, raising=False)
    monkeypatch.setattr(config, "MODE", "small_live", raising=False)

    with pytest.raises(RuntimeError):
        trader.calc_bet_amount(type("Sig", (), {"confidence": 0.8})(), 10.0)


def test_execution_recheck_rejects_missing_order_size_avg_instead_of_book_cap_fallback():
    class PM:
        def get_orderbook_summary(self, token_id, max_price=None):
            return {
                "source": "polymarket_ws",
                "best_ask": 0.13,
                "best_bid": 0.12,
                "spread": 0.01,
                "age_sec": 0.01,
                "ask_depth_to_cap": 100.0,
                "weighted_avg_ask_to_cap": 0.13,
                "asks": [],
            }

    res = ExecutionQualityGuard(PM()).recheck_before_order(
        token_id="token-a",
        original_ob={"best_ask": 0.13},
        original_price=0.13,
        expected_cost=1.0,
        estimated_shares=7.69,
        max_token_price=0.45,
        max_spread=0.20,
        min_liquidity_multiplier=1.0,
        slippage_cap=0.50,
        max_weighted_avg_price=0.45,
        strategy_name="barrier_reclaim",
    )

    assert not res.allowed
    assert res.reason == "execution_depth_evaporated"
    assert res.details["available_shares"] == 0.0
