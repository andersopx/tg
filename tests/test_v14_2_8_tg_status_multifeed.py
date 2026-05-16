from telegram_bot import TelegramBot


class FakeMultiAssetFeed:
    def __init__(self):
        self.calls = []
    def get_current_price(self, asset="BTC"):
        self.calls.append(asset)
        return 80750.25


def test_render_status_uses_multi_asset_feed_get_current_price():
    bot = TelegramBot.__new__(TelegramBot)
    bot.feed = FakeMultiAssetFeed()
    bot.risk = None
    bot.learner = None
    text = bot._render_status()
    assert "BTC: $80,750.25" in text
    assert bot.feed.calls == ["BTC"]
