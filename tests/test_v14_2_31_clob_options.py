import polymarket_client as pm
from config import config
from polymarket_client import PolymarketClient


class DummyDB:
    pass


def test_make_order_options_has_tick_size_attribute():
    client = PolymarketClient(DummyDB())
    opt = client._make_order_options(0.01, False)
    assert getattr(opt, "tick_size") == "0.01"
    assert getattr(opt, "neg_risk") is False


def test_place_buy_order_supports_sdk_that_requires_options_attributes(monkeypatch):
    calls = {}

    class FakeMarketOrderArgs:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeOrderType:
        FOK = "FOK"
        FAK = "FAK"
        GTC = "GTC"

    class FakeSdk:
        def create_market_order(self, order_args, options=None):
            # This mimics py-clob-client versions that read options.tick_size.
            calls["tick_size"] = options.tick_size
            return {"signed": True}

        def post_order(self, signed_order, order_type):
            return {"success": True, "orderID": "0xabc", "status": "matched"}

    monkeypatch.setattr(pm, "MarketOrderArgs", FakeMarketOrderArgs)
    monkeypatch.setattr(pm, "OrderType", FakeOrderType)
    monkeypatch.setattr(pm, "BUY", "BUY")

    old = (config.MODE, config.DRY_RUN, config.REAL_TRADING_ENABLED, config.OBSERVER_ONLY)
    try:
        config.MODE = "small_live"
        config.DRY_RUN = False
        config.REAL_TRADING_ENABLED = True
        config.OBSERVER_ONLY = False
        client = PolymarketClient(DummyDB())
        client.client = FakeSdk()
        resp = client.place_buy_order(
            token_id="token",
            price=0.25,
            shares=4.0,
            amount_usd=1.0,
            order_type="FOK",
            tick_size=0.01,
            neg_risk=False,
        )
    finally:
        config.MODE, config.DRY_RUN, config.REAL_TRADING_ENABLED, config.OBSERVER_ONLY = old

    assert calls["tick_size"] == "0.01"
    assert resp["success"] is True
    assert resp["orderID"] == "0xabc"


def test_place_buy_order_never_posts_when_real_orders_disabled():
    calls = {"posted": False}

    class FakeSdk:
        def create_market_order(self, *args, **kwargs):
            return {"signed": True}

        def post_order(self, *args, **kwargs):
            calls["posted"] = True
            return {"success": True, "orderID": "0xunsafe"}

    old = (config.MODE, config.DRY_RUN, config.REAL_TRADING_ENABLED, config.OBSERVER_ONLY)
    try:
        config.MODE = "paper"
        config.DRY_RUN = True
        config.REAL_TRADING_ENABLED = False
        config.OBSERVER_ONLY = False
        client = PolymarketClient(DummyDB())
        client.client = FakeSdk()
        resp = client.place_buy_order("token", 0.25, 4.0, 1.0, "FOK", 0.01, False)
    finally:
        config.MODE, config.DRY_RUN, config.REAL_TRADING_ENABLED, config.OBSERVER_ONLY = old

    assert resp["success"] is False
    assert resp["errorMsg"] == "real_orders_disabled"
    assert calls["posted"] is False
