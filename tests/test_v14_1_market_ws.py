import time

from realtime_orderbook import RealtimeOrderbookCache
from polymarket_ws import PolymarketMarketWebSocket


def test_ws_cache_book_summary_is_trader_compatible():
    cache = RealtimeOrderbookCache()
    cache.apply_ws_message({
        "event_type": "book",
        "asset_id": "token-a",
        "market": "0xmarket",
        "bids": [{"price": "0.47", "size": "10"}, {"price": "0.48", "size": "5"}],
        "asks": [{"price": "0.52", "size": "4"}, {"price": "0.53", "size": "6"}],
        "timestamp": "1766789469958",
    })
    summary = cache.summary("token-a", max_price=0.53, max_age_sec=3)
    assert summary["source"] == "polymarket_ws"
    assert summary["best_bid"] == 0.48
    assert summary["best_ask"] == 0.52
    assert summary["spread"] == 0.04
    assert summary["ask_depth_to_cap"] == 10
    assert summary["weighted_avg_ask_to_cap"] == (0.52 * 4 + 0.53 * 6) / 10


def test_ws_cache_applies_price_change_and_zero_size_removes_level():
    cache = RealtimeOrderbookCache()
    cache.update_book("token-a", bids=[("0.48", "5")], asks=[("0.52", "4")])
    cache.apply_ws_message({
        "event_type": "price_change",
        "market": "0xmarket",
        "price_changes": [
            {"asset_id": "token-a", "price": "0.51", "size": "8", "side": "SELL", "best_bid": "0.48", "best_ask": "0.51"},
            {"asset_id": "token-a", "price": "0.52", "size": "0", "side": "SELL", "best_bid": "0.48", "best_ask": "0.51"},
        ],
        "timestamp": "1766789469958",
    })
    summary = cache.summary("token-a", max_age_sec=3)
    assert summary["best_ask"] == 0.51
    assert (0.52, 4.0) not in summary["asks"]
    assert (0.51, 8.0) in summary["asks"]


def test_ws_cache_best_bid_ask_without_full_book_is_usable_for_price():
    cache = RealtimeOrderbookCache()
    cache.apply_ws_message({
        "event_type": "best_bid_ask",
        "asset_id": "token-b",
        "market": "0xmarket",
        "best_bid": "0.73",
        "best_ask": "0.77",
        "spread": "0.04",
        "timestamp": "1766789469958",
    })
    summary = cache.summary("token-b", max_age_sec=3)
    assert summary["best_bid"] == 0.73
    assert summary["best_ask"] == 0.77
    assert summary["spread"] == 0.04


def test_ws_manager_handles_ping_pong_and_json_messages_without_network():
    cache = RealtimeOrderbookCache()
    ws = PolymarketMarketWebSocket(cache=cache)

    class DummyWS:
        def __init__(self):
            self.sent = []
        async def send(self, data):
            self.sent.append(data)

    import asyncio
    async def run():
        ws._ws = DummyWS()
        await ws._handle_raw("PING")
        await ws._handle_raw("PONG")
        await ws._handle_raw('{"event_type":"book","asset_id":"token-c","bids":[{"price":"0.1","size":"1"}],"asks":[{"price":"0.2","size":"2"}]}')

    asyncio.run(run())
    assert "PONG" in ws._ws.sent
    assert ws.last_pong_ts > 0
    assert cache.summary("token-c", max_age_sec=3)["best_ask"] == 0.2
