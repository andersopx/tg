"""Execution flight recorder.

Records important decision/execution stages so failures are explainable later.
It uses local SQLite only and does not call external APIs.
"""
import time
from typing import Optional

from config import config


class ExecutionFlightRecorder:
    def __init__(self, db):
        self.db = db

    def record(self, stage: str, *, signal=None, market: Optional[dict] = None, token_id: str = "",
               outcome: str = "", reason: str = "", details: Optional[dict] = None,
               trade_id=None, order_id: str = "", event_type: str = "", message: str = "", error: str = ""):
        if not config.EXECUTION_FLIGHT_RECORDER_ENABLED:
            return
        try:
            self.db.record_execution_event(
                stage=str(stage or ""),
                window_ts=getattr(signal, "window_ts", None),
                asset=getattr(signal, "asset", (market or {}).get("asset", "BTC")),
                timeframe=getattr(signal, "timeframe", (market or {}).get("timeframe", "5m")),
                market_slug=(market or {}).get("slug", ""),
                token_id=token_id or "",
                direction=outcome or getattr(signal, "direction", ""),
                reason=reason or "",
                details=details or {},
                trade_id=trade_id if trade_id is not None else ((details or {}).get("trade_id") if isinstance(details, dict) else None),
                order_id=order_id or (((details or {}).get("order_id") or (details or {}).get("submitted_order_id") or "") if isinstance(details, dict) else ""),
                event_type=event_type or stage,
                message=message or "",
                error=error or (((details or {}).get("error") or "") if isinstance(details, dict) else ""),
            )
        except Exception:
            # Never let diagnostics break trading.
            pass

    def mark_latency(self, name: str, started_ts: float, **kwargs):
        details = dict(kwargs)
        details["latency_ms"] = round((time.time() - float(started_ts)) * 1000, 3)
        self.record(name, details=details)
