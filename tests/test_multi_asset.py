import os
import tempfile
import unittest

from config import config
from binance_feed import MultiAssetBinanceFeed
from database import Database
from polymarket_client import PolymarketClient


class MultiAssetConfigTests(unittest.TestCase):
    def test_multi_asset_feed_symbols(self):
        feed = MultiAssetBinanceFeed(["BTC", "ETH", "SOL"], buffer_size=3)
        self.assertEqual(feed.get_feed("BTC").symbol, "BTCUSDT")
        self.assertEqual(feed.get_feed("ETH").symbol, "ETHUSDT")
        self.assertEqual(feed.get_feed("SOL").symbol, "SOLUSDT")

    def test_database_window_locks_are_per_asset(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        db = Database(path)
        db.mark_order_attempt(123456, "eth-updown-5m-123456", asset="ETH")
        self.assertTrue(db.has_recent_order_attempt(123456, 9999, asset="ETH"))
        self.assertFalse(db.has_recent_order_attempt(123456, 9999, asset="BTC"))
        os.unlink(path)

    def test_market_parser_maps_outcomes_to_tokens(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(path)
        client = PolymarketClient(Database(path))
        market = client._parse_market({
            "slug": "eth-updown-5m-1770000000",
            "question": "Ethereum Up or Down - May 1, 12:00PM ET",
            "description": "This market resolves using the Chainlink ETH/USD Data Stream.",
            "outcomes": '["Down", "Up"]',
            "clobTokenIds": '["down-token", "up-token"]',
            "outcomePrices": '["0.40", "0.60"]',
        })
        self.assertEqual(market["up_token_id"], "up-token")
        self.assertEqual(market["down_token_id"], "down-token")
        os.unlink(path)


if __name__ == "__main__":
    unittest.main()
