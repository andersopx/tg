import asyncio
import time

from config import config
from trader import Trader


class _NoMarketPolymarket:
    async def find_market_by_slug(self, slug):
        return None

    async def find_updown_market(self, *args, **kwargs):
        return None


def test_shadow_settlement_does_not_use_external_close_fallback_by_default(monkeypatch):
    async def run_check():
        old = getattr(config, "SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED", False)
        config.SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED = False
        try:
            trader = Trader.__new__(Trader)
            trader.polymarket = _NoMarketPolymarket()

            async def fail_if_called(*args, **kwargs):  # pragma: no cover - should not run
                raise AssertionError("external close fallback should be disabled by default")

            trader._fetch_external_close_price_for_window = fail_if_called
            now = int(time.time())
            trade = {
                "id": 1,
                "asset": "BTC",
                "timeframe": "5m",
                "window_ts": now - 3600,
                "direction": "Up",
                "reference_price": 100.0,
                "market_slug": "btc-updown-5m-test",
            }

            outcome, final_price, source = await Trader._resolve_shadow_outcome(trader, trade, now)

            assert outcome is None
            assert final_price == 0.0
            assert source == ""
        finally:
            config.SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED = old

    asyncio.run(run_check())


def test_platform_first_defaults_are_explicit():
    assert config.REFERENCE_PRICE_SOURCE == "polymarket_gamma"
    assert config.PRICE_TO_BEAT_FALLBACK_ENABLED is False
    assert config.SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED is False
