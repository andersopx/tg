"""Decision coalescer for bursty market events.

Even before full WebSocket execution, this prevents the same asset/timeframe/window from being evaluated repeatedly within a short burst.
"""
import time
from config import config


class MarketEventCoalescer:
    def __init__(self):
        self._last = {}
        self._inflight = set()

    def should_process(self, key: str) -> bool:
        now = time.time()
        delay = max(0.05, float(config.MARKET_EVENT_COALESCE_MS) / 1000.0)
        last = self._last.get(key, 0.0)
        if now - last < delay:
            return False
        # In synchronous decision loops the caller may return early from many branches.
        # To avoid a stale infinite lock, V14 uses time-based coalescing only here.
        self._last[key] = now
        return True

    def done(self, key: str):
        self._inflight.discard(key)
