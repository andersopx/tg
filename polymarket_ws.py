"""Optional public Polymarket Market WebSocket client.

This module only consumes public market data. It intentionally avoids the user
channel, API credentials, balances, allowances, and order status streams.
"""
import asyncio
import json
import logging
import time
from collections import OrderedDict
from typing import Iterable, Optional, Set

try:
    import websockets
except Exception:  # pragma: no cover - exercised when dependency is missing in deployment
    websockets = None

from config import config
from realtime_orderbook import RealtimeOrderbookCache

log = logging.getLogger(__name__)


class PolymarketMarketWebSocket:
    """Public Polymarket market-channel reader with reconnect and PING/PONG."""

    def __init__(self, cache: Optional[RealtimeOrderbookCache] = None, url: Optional[str] = None):
        self.cache = cache or RealtimeOrderbookCache()
        self.url = url or config.REALTIME_ORDERBOOK_WS_URL
        # LRU registry of desired token subscriptions. Older builds used an
        # unbounded set, which leaked thousands of expired market tokens over
        # multi-day runs. OrderedDict preserves recency while giving set-like
        # membership checks.
        self.desired_assets: OrderedDict[str, float] = OrderedDict()
        self.active_assets: Set[str] = set()
        self._task = None
        self._stop_event = None
        self._wake_event = None
        self._send_lock = None
        self._ws = None
        self.connected = False
        self.last_message_ts = 0.0
        self.last_pong_ts = 0.0
        self.reconnects = 0
        self.messages_received = 0
        self.last_error = ""

    @property
    def stats(self) -> dict:
        return {
            "enabled": bool(config.REALTIME_ORDERBOOK_ENABLED),
            "connected": bool(self.connected),
            "subscribed_assets": len(self.desired_assets),
            "active_assets": len(self.active_assets),
            "messages_received": int(self.messages_received),
            "reconnects": int(self.reconnects),
            "last_message_age_sec": (time.time() - self.last_message_ts) if self.last_message_ts else None,
            "last_error": self.last_error,
        }

    async def start(self):
        if not config.REALTIME_ORDERBOOK_ENABLED:
            log.info("Polymarket market WebSocket disabled; REST orderbook fallback remains active")
            return
        if websockets is None:
            log.warning("websockets package not installed; Polymarket market WS disabled")
            return
        if self._task and not self._task.done():
            return
        self._stop_event = asyncio.Event()
        self._wake_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._task = asyncio.create_task(self._run(), name="polymarket-market-ws")
        log.info("Polymarket market WebSocket manager started")

    async def stop(self):
        if self._stop_event:
            self._stop_event.set()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
        self.connected = False

    def _desired_asset_set(self) -> Set[str]:
        return set(self.desired_assets.keys())

    def _trim_desired_assets_lru(self) -> list[str]:
        max_assets = max(1, int(getattr(config, "REALTIME_ORDERBOOK_MAX_SUBSCRIPTIONS", 200) or 200))
        removed = []
        while len(self.desired_assets) > max_assets:
            asset, _ = self.desired_assets.popitem(last=False)
            removed.append(asset)
        if removed:
            self.active_assets.difference_update(removed)
            log.warning(
                "Polymarket WS subscription LRU trimmed %s expired assets; total=%s cap=%s",
                len(removed), len(self.desired_assets), max_assets,
            )
        return removed

    async def subscribe_assets(self, asset_ids: Iterable[str]):
        clean = [str(a) for a in (asset_ids or []) if a]
        now = time.time()
        new_assets = []
        for asset in clean:
            if asset in self.desired_assets:
                self.desired_assets.move_to_end(asset)
                self.desired_assets[asset] = now
            else:
                self.desired_assets[asset] = now
                new_assets.append(asset)
        removed = self._trim_desired_assets_lru()
        if removed and self.connected and self._ws:
            await self._send_unsubscription(removed)
        if not new_assets:
            return
        if self._wake_event:
            self._wake_event.set()
        if self.connected and self._ws:
            await self._send_subscription(new_assets, initial=False)
        log.info("Polymarket WS subscribed desired assets +%s total=%s", len(new_assets), len(self.desired_assets))

    async def cleanup_expired_subscriptions(self, active_tokens: Set[str]):
        """Keep only active tokens and LRU-recent desired subscriptions.

        ``active_tokens`` should contain tokens for still-open trades/windows.
        Everything else can be removed so expired 5m markets do not accumulate.
        """
        keep = {str(t) for t in (active_tokens or set()) if t}
        current = self._desired_asset_set()
        to_remove = sorted(current - keep)
        if not to_remove:
            self._trim_desired_assets_lru()
            return 0
        for token in to_remove:
            self.desired_assets.pop(token, None)
        self.active_assets.difference_update(to_remove)
        if self.connected and self._ws:
            await self._send_unsubscription(to_remove)
        log.info("Polymarket WS cleanup removed %s expired subscriptions; total=%s", len(to_remove), len(self.desired_assets))
        return len(to_remove)

    async def wait_for_any_book(self, asset_ids: Iterable[str], timeout_sec: float = 0.35) -> bool:
        deadline = time.time() + max(0.0, float(timeout_sec or 0.0))
        ids = [str(a) for a in (asset_ids or []) if a]
        if not ids:
            return False
        while time.time() < deadline:
            if any(self.cache.is_fresh(a, config.REALTIME_ORDERBOOK_MAX_AGE_SEC) for a in ids):
                return True
            await asyncio.sleep(0.025)
        return any(self.cache.is_fresh(a, config.REALTIME_ORDERBOOK_MAX_AGE_SEC) for a in ids)

    async def _run(self):
        backoff = 1.0
        while not self._stopping():
            try:
                if not self.desired_assets:
                    if self._wake_event:
                        self._wake_event.clear()
                        try:
                            await asyncio.wait_for(self._wake_event.wait(), timeout=1.0)
                        except asyncio.TimeoutError:
                            pass
                    continue
                await self._connect_and_read()
                backoff = 1.0
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.connected = False
                self.last_error = str(e)
                self.reconnects += 1
                sleep_for = min(float(config.REALTIME_ORDERBOOK_RECONNECT_MAX_SEC), backoff)
                log.warning("Polymarket market WS reconnect in %.1fs after error: %s", sleep_for, e)
                await asyncio.sleep(sleep_for)
                backoff = min(float(config.REALTIME_ORDERBOOK_RECONNECT_MAX_SEC), backoff * 2.0)

    async def _connect_and_read(self):
        async with websockets.connect(self.url, ping_interval=None, close_timeout=3) as ws:
            self._ws = ws
            self.connected = True
            self.active_assets = set()
            await self._send_subscription(sorted(self.desired_assets.keys()), initial=True)
            heartbeat = asyncio.create_task(self._heartbeat_loop())
            try:
                async for raw in ws:
                    if self._stopping():
                        break
                    await self._handle_raw(raw)
                    extra = sorted(self._desired_asset_set() - self.active_assets)
                    if extra:
                        await self._send_subscription(extra, initial=False)
            finally:
                heartbeat.cancel()
                await asyncio.gather(heartbeat, return_exceptions=True)
                self.connected = False
                self._ws = None
                self.active_assets = set()

    async def _send_subscription(self, asset_ids, initial: bool):
        if not asset_ids or not self._ws:
            return
        msg = {
            "assets_ids": [str(a) for a in asset_ids if a],
            "custom_feature_enabled": True,
        }
        if initial:
            msg["type"] = "market"
        else:
            msg["operation"] = "subscribe"
        async with self._send_lock:
            await self._ws.send(json.dumps(msg))
        self.active_assets.update(str(a) for a in asset_ids if a)

    async def _send_unsubscription(self, asset_ids):
        if not asset_ids or not self._ws:
            return
        msg = {
            "operation": "unsubscribe",
            "assets_ids": [str(a) for a in asset_ids if a],
        }
        try:
            async with self._send_lock:
                await self._ws.send(json.dumps(msg))
        except Exception as e:
            self.last_error = str(e)
            log.warning("Polymarket WS unsubscribe failed for %s assets: %s", len(asset_ids), e)
        self.active_assets.difference_update(str(a) for a in asset_ids if a)

    async def _heartbeat_loop(self):
        while not self._stopping() and self._ws:
            try:
                await asyncio.sleep(float(config.REALTIME_ORDERBOOK_PING_SEC))
                if self._ws:
                    await self._ws.send("PING")
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = str(e)
                raise

    async def _handle_raw(self, raw):
        if raw is None:
            return
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="ignore")
        text = str(raw).strip()
        if not text:
            return
        if text.upper() == "PONG":
            self.last_pong_ts = time.time()
            return
        if text.upper() == "PING":
            if self._ws:
                await self._ws.send("PONG")
            return
        try:
            payload = json.loads(text)
        except Exception:
            log.debug("Ignoring non-json Polymarket WS message: %r", text[:120])
            return
        self.last_message_ts = time.time()
        if isinstance(payload, list):
            for item in payload:
                self._apply(item)
        else:
            self._apply(payload)

    def _apply(self, msg):
        if not isinstance(msg, dict):
            return
        self.messages_received += 1
        try:
            self.cache.apply_ws_message(msg)
        except Exception as e:
            self.last_error = str(e)
            log.warning("Polymarket WS message parse failed: %s payload=%s", e, str(msg)[:300])

    def _stopping(self) -> bool:
        return bool(self._stop_event and self._stop_event.is_set())
