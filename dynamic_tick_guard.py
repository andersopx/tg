"""Dynamic tick-size guard for order preparation.

V14.2 keeps this deliberately narrow: it reads public market metadata and the
public WS orderbook cache only.  It does not touch wallet auth, balances,
allowances, user WebSocket, or order status logic.
"""
from dataclasses import dataclass
import time
from typing import Optional

from config import config


@dataclass
class TickDecision:
    tick_size: float
    source: str
    age_sec: Optional[float] = None
    rounded_price: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "tick_size": self.tick_size,
            "source": self.source,
            "age_sec": self.age_sec,
            "rounded_price": self.rounded_price,
        }


class DynamicTickGuard:
    """Resolve the safest currently-known tick size for a token."""

    def __init__(self, realtime_orderbook=None):
        self.realtime_orderbook = realtime_orderbook

    def resolve(self, token_id: str, market: Optional[dict] = None, fallback: Optional[float] = None) -> TickDecision:
        fallback = float(fallback or config.TICK_SIZE)
        token_id = str(token_id or "")

        if config.DYNAMIC_TICK_GUARD_ENABLED and self.realtime_orderbook and token_id:
            try:
                state = self.realtime_orderbook.get(token_id)
                if state and state.tick_size and state.tick_size > 0:
                    age = time.time() - float(state.last_update_ts or 0)
                    if age <= float(config.DYNAMIC_TICK_MAX_AGE_SEC):
                        return TickDecision(float(state.tick_size), "ws_tick_size_change", round(age, 4))
            except Exception:
                pass

        for key in ("tick_size", "minimum_tick_size", "min_tick_size", "orderMinTickSize"):
            try:
                value = (market or {}).get(key)
                if value is not None and float(value) > 0:
                    return TickDecision(float(value), f"market.{key}", None)
            except Exception:
                continue

        return TickDecision(float(fallback), "config.TICK_SIZE", None)

    @staticmethod
    def round_to_tick(price: float, tick: float) -> float:
        try:
            tick = float(tick)
            price = float(price)
        except Exception:
            return round(float(price or 0.0), 3)
        if tick <= 0:
            return round(price, 3)
        precision = max(0, min(6, len(str(tick).split(".")[-1].rstrip("0")))) if "." in str(tick) else 0
        units = round(price / tick)
        return round(units * tick, precision)

    def prepare_price(self, token_id: str, market: Optional[dict], desired_price: float) -> TickDecision:
        decision = self.resolve(token_id, market)
        decision.rounded_price = self.round_to_tick(float(desired_price), decision.tick_size)
        return decision
