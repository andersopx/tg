"""
Real-time multi-asset price feed from Binance WebSocket.
Maintains in-memory rolling windows of 1-minute klines.
"""
import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional
import aiohttp
import websockets

from config import config

log = logging.getLogger(__name__)


class Kline:
    """A single 1-minute candlestick"""
    def __init__(self, open_time: int, o: float, h: float, l: float, c: float, v: float, closed: bool):
        self.open_time = open_time  # ms
        self.open = o
        self.high = h
        self.low = l
        self.close = c
        self.volume = v
        self.closed = closed

    @property
    def body(self) -> float:
        return abs(self.close - self.open)

    @property
    def total_range(self) -> float:
        return self.high - self.low

    @property
    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    @property
    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low

    @property
    def is_bullish(self) -> bool:
        return self.close > self.open

    @property
    def is_bearish(self) -> bool:
        return self.close < self.open

    def to_dict(self):
        return {
            "open_time": self.open_time,
            "open": self.open, "high": self.high,
            "low": self.low, "close": self.close,
            "volume": self.volume, "closed": self.closed
        }


class BinanceFeed:
    """
    Subscribes to one Binance 1-minute kline stream.
    Keeps rolling buffer of last N closed klines + current forming kline.
    """
    REST_URL = "https://api.binance.com/api/v3/klines"

    def __init__(self, symbol: str = "BTCUSDT", buffer_size: int = 60):
        self.symbol = str(symbol or "BTCUSDT").upper()
        self.ws_url = f"wss://stream.binance.com:9443/ws/{self.symbol.lower()}@kline_1m"
        self.buffer_size = buffer_size
        self.klines: deque = deque(maxlen=buffer_size)
        self.current_kline: Optional[Kline] = None
        self.last_price: float = 0.0
        self.last_update_ts: float = 0.0
        self.connected = False
        self._ws_task: Optional[asyncio.Task] = None

    async def bootstrap(self):
        """Pre-fill buffer with recent historical klines via REST"""
        try:
            async with aiohttp.ClientSession() as s:
                params = {
                    "symbol": self.symbol,
                    "interval": "1m",
                    "limit": self.buffer_size
                }
                async with s.get(self.REST_URL, params=params, timeout=10) as r:
                    data = await r.json()
                    for k in data:
                        kline = Kline(
                            open_time=k[0],
                            o=float(k[1]), h=float(k[2]),
                            l=float(k[3]), c=float(k[4]),
                            v=float(k[5]), closed=True
                        )
                        self.klines.append(kline)
                    if data:
                        self.last_price = float(data[-1][4])
                        self.last_update_ts = time.time()
                    log.info(f"✅ Binance feed {self.symbol} bootstrapped {len(self.klines)} klines, last price ${self.last_price:,.2f}")
        except Exception as e:
            log.error(f"Bootstrap failed: {e}")

    async def _ws_loop(self):
        """WebSocket connection with auto-reconnect"""
        while True:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20) as ws:
                    self.connected = True
                    log.info("✅ Binance feed %s running", self.symbol)
                    async for message in ws:
                        self._handle_message(json.loads(message))
            except Exception as e:
                self.connected = False
                log.error(f"Binance WS error: {e}, reconnecting in 5s...")
                await asyncio.sleep(5)

    def _handle_message(self, msg: dict):
        k = msg.get("k", {})
        if not k:
            return
        kline = Kline(
            open_time=k["t"],
            o=float(k["o"]), h=float(k["h"]),
            l=float(k["l"]), c=float(k["c"]),
            v=float(k["v"]), closed=k["x"]
        )
        self.last_price = kline.close
        self.last_update_ts = time.time()

        if kline.closed:
            self.klines.append(kline)
            self.current_kline = None
        else:
            self.current_kline = kline

    async def start(self):
        await self.bootstrap()
        self._ws_task = asyncio.create_task(self._ws_loop())

    async def stop(self):
        if self._ws_task:
            self._ws_task.cancel()

    # ============ Data accessors ============
    def get_recent_klines(self, n: int) -> list:
        """Get last N closed klines"""
        return list(self.klines)[-n:]

    def get_current_price(self) -> float:
        return self.last_price

    def is_stale(self, max_age_sec: int = 15) -> bool:
        if not self.last_update_ts:
            return True
        return (time.time() - self.last_update_ts) > max_age_sec

    def get_price_change_pct(self, minutes: int) -> float:
        """Price change % over last N minutes"""
        if len(self.klines) < minutes:
            return 0.0
        start = self.klines[-minutes].open
        end = self.last_price
        return (end - start) / start if start > 0 else 0.0

    def get_window_klines(self, window_start_ts: int, window_size_sec: int = 300) -> list:
        """Get klines that fall within a Polymarket crypto Up/Down window."""
        window_start_ms = window_start_ts * 1000
        window_end_ms = (window_start_ts + int(window_size_sec or 300)) * 1000
        result = [k for k in self.klines if window_start_ms <= k.open_time < window_end_ms]
        if self.current_kline and window_start_ms <= self.current_kline.open_time < window_end_ms:
            result.append(self.current_kline)
        return result


class MultiAssetBinanceFeed:
    """Run one BinanceFeed per enabled asset."""

    def __init__(self, assets=None, buffer_size: int = 60):
        self.assets = [a.strip().upper() for a in (assets or config.assets_enabled)]
        self.feeds = {}
        for asset in self.assets:
            self.feeds[asset] = BinanceFeed(symbol=config.asset_symbol(asset), buffer_size=buffer_size)

    async def start(self):
        await asyncio.gather(*[feed.start() for feed in self.feeds.values()])

    async def stop(self):
        await asyncio.gather(*[feed.stop() for feed in self.feeds.values()])

    def get_feed(self, asset: str) -> BinanceFeed:
        asset = str(asset or "BTC").upper()
        return self.feeds[asset]

    def get_current_price(self, asset: str = "BTC") -> float:
        return self.get_feed(asset).get_current_price()

    def is_stale(self, max_age_sec: int = 15, asset: str = "BTC") -> bool:
        return self.get_feed(asset).is_stale(max_age_sec)

    def get_price_change_pct(self, minutes: int, asset: str = "BTC") -> float:
        return self.get_feed(asset).get_price_change_pct(minutes)
