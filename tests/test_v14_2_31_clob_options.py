import polymarket_client as pm
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

    assert calls["tick_size"] == "0.01"
    assert resp["success"] is True
    assert resp["orderID"] == "0xabc"
