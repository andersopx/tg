"""
Public-data recorder for Polymarket BTC/ETH/SOL 5m markets. V6 adds price-history sampling.

This is the "big data" layer. It does not trade. It records:
- Gamma market metadata (Price-to-Beat, volume, liquidity)
- CLOB orderbook snapshots for Up/Down tokens
- public Data API trades when enabled

The learning layer uses the bot's own resolved trades for calibration; this recorder
builds a local dataset that can later be used for backtests and threshold tuning.
"""
import asyncio
import logging
import time

from config import config
from database import Database
from polymarket_client import PolymarketClient
from binance_feed import BinanceFeed, MultiAssetBinanceFeed

log = logging.getLogger(__name__)


class DataCollector:
    def __init__(self, db: Database, polymarket: PolymarketClient, feed: BinanceFeed):
        self.db = db
        self.polymarket = polymarket
        self.feed = feed
        self.running = False
        self._last_trade_fetch_by_slug = {}
        self._last_prune = 0

    async def run(self):
        self.running = True
        log.info("📚 Public data recorder started")
        while self.running:
            try:
                await self.collect_current_window()
                now = time.time()
                prune_interval = max(3600, int(getattr(config, "DATA_PRUNE_INTERVAL_HOURS", 6) or 6) * 3600)
                if now - self._last_prune > prune_interval:
                    await asyncio.to_thread(self.db.prune_public_data, config.DATA_KEEP_DAYS)
                    self._last_prune = now
                await asyncio.sleep(max(3, config.DATA_SNAPSHOT_INTERVAL_SEC))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("DataCollector error: %s", e)
                await asyncio.sleep(10)

    async def collect_current_window(self):
        now = int(time.time())
        for timeframe in config.timeframes_enabled:
            interval_sec = config.timeframe_seconds(timeframe)
            window_ts = (now // interval_sec) * interval_sec
            for asset in config.assets_enabled:
                market = await self.polymarket.find_updown_market(asset, window_ts, timeframe)
                if not market:
                    continue
                if market.get("closed") or market.get("archived"):
                    continue
                feed = self.feed.get_feed(asset) if hasattr(self.feed, "get_feed") else self.feed

                snapshot = await asyncio.to_thread(
                    self.polymarket.build_market_snapshot,
                    market,
                    feed.get_current_price(),
                )
                snapshot["asset"] = asset
                snapshot["timeframe"] = timeframe
                self.db.insert_market_snapshot(snapshot)

                slug = market.get("slug", "")
                last_fetch = self._last_trade_fetch_by_slug.get(slug, 0)
                if config.DATA_FETCH_TRADES and now - last_fetch >= 30:
                    trades = await self.polymarket.fetch_recent_public_trades(market, limit=100)
                    if trades:
                        for t in trades:
                            t.setdefault("asset", asset)
                            t.setdefault("timeframe", timeframe)
                        inserted = await asyncio.to_thread(self.db.insert_public_trade_samples, trades)
                        if inserted:
                            log.info("📥 Recorded %s public trade samples for %s %s %s", inserted, asset, timeframe, slug)

                    if config.DATA_FETCH_PRICE_HISTORY:
                        start_ts = max(window_ts, now - interval_sec)
                        for outcome, token_id in (("Up", market.get("up_token_id")), ("Down", market.get("down_token_id"))):
                            if not token_id:
                                continue
                            hist = await self.polymarket.fetch_prices_history(token_id, start_ts=start_ts, end_ts=now, fidelity=1)
                            if hist:
                                inserted = await asyncio.to_thread(self.db.insert_price_history_samples, slug, token_id, outcome, hist)
                                if inserted:
                                    log.info("📈 Recorded %s price-history samples for %s %s %s %s", inserted, asset, timeframe, slug, outcome)

                    self._last_trade_fetch_by_slug[slug] = now

    def stop(self):
        self.running = False
