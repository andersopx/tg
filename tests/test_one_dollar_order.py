from one_dollar_order import choose_candidate, current_window_ts, estimate_market_buy_from_asks
from config import config


class FakePolymarket:
    def __init__(self, books):
        self.books = books

    def get_orderbook_summary(self, token_id, max_price=None):
        book = dict(self.books[token_id])
        asks = book.get("asks") or []
        if max_price is not None:
            asks = [level for level in asks if level[0] <= max_price]
        book["asks"] = asks
        return book

    @staticmethod
    def _round_to_tick(price, tick):
        return round(round(price / tick) * tick, 4)


def test_current_window_ts_uses_timeframe_seconds():
    assert current_window_ts(1710000123, "5m") == 1710000000
    assert current_window_ts(1710000123, "15m") == 1710000000


def test_estimate_market_buy_from_asks_caps_by_slippage_price():
    shares, cost, available = estimate_market_buy_from_asks(
        [(0.20, 2.0), (0.25, 4.0), (0.40, 100.0)],
        amount_usd=1.0,
        max_price=0.25,
    )
    assert round(shares, 6) == 4.4
    assert round(cost, 6) == 1.0
    assert round(available, 6) == 1.4


def test_choose_candidate_auto_picks_cheaper_side_with_one_dollar_liquidity(monkeypatch):
    monkeypatch.setattr(config, "MARKET_BUY_SLIPPAGE", 0.03, raising=False)
    pm = FakePolymarket({
        "up-token": {"best_ask": 0.40, "best_bid": 0.39, "asks": [(0.40, 10.0)]},
        "down-token": {"best_ask": 0.22, "best_bid": 0.21, "asks": [(0.22, 5.0)]},
    })
    market = {
        "slug": "btc-updown-5m-1710000000",
        "up_token_id": "up-token",
        "down_token_id": "down-token",
        "tick_size": 0.01,
        "neg_risk": False,
    }

    cand = choose_candidate(pm, market, asset="BTC", timeframe="5m", window_ts=1710000000, side="auto")

    assert cand.outcome == "Down"
    assert cand.estimated_cost == 1.0
    assert cand.estimated_shares > 4.5


def test_choose_candidate_rejects_insufficient_liquidity(monkeypatch):
    monkeypatch.setattr(config, "MARKET_BUY_SLIPPAGE", 0.01, raising=False)
    pm = FakePolymarket({
        "up-token": {"best_ask": 0.90, "best_bid": 0.89, "asks": [(0.90, 0.5)]},
        "down-token": {"best_ask": 0.91, "best_bid": 0.90, "asks": [(0.91, 0.5)]},
    })
    market = {
        "slug": "btc-updown-5m-1710000000",
        "up_token_id": "up-token",
        "down_token_id": "down-token",
        "tick_size": 0.01,
    }

    try:
        choose_candidate(pm, market, asset="BTC", timeframe="5m", window_ts=1710000000, side="auto")
    except RuntimeError as e:
        assert "insufficient ask liquidity" in str(e)
    else:
        raise AssertionError("expected insufficient liquidity error")
