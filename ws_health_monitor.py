"""WebSocket health summarizer for Product Doctor."""
from typing import Optional

from config import config


class WSHealthMonitor:
    def __init__(self, market_ws=None):
        self.market_ws = market_ws

    def snapshot(self) -> dict:
        if not self.market_ws:
            enabled = bool(config.REALTIME_ORDERBOOK_ENABLED)
            return {
                "enabled": enabled,
                "available": False,
                "connected": False,
                "status": "not_initialized" if enabled else "disabled",
                "mode": "ws" if enabled else "rest_polling",
                "note": "Polymarket WS 未启用，当前使用 REST 盘口轮询。" if not enabled else "WS 已启用但尚未初始化。",
            }
        try:
            stats = dict(getattr(self.market_ws, "stats", {}) or {})
        except Exception:
            stats = {}
        stats.setdefault("enabled", bool(config.REALTIME_ORDERBOOK_ENABLED))
        stats["available"] = True
        age = stats.get("last_message_age_sec")
        if not stats.get("enabled"):
            stats["status"] = "disabled"
        elif not stats.get("connected"):
            stats["status"] = "disconnected"
        elif age is not None and float(age) > float(config.WS_HEALTH_MAX_MESSAGE_AGE_SEC):
            stats["status"] = "stale"
        else:
            stats["status"] = "ok"
        return stats
