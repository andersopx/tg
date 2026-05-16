"""Final execution-quality checks before dry-run or live submission.

This module protects execution quality only. It does not place orders, read
balances, derive credentials, or change strategy decisions.
"""
from dataclasses import dataclass, field
from typing import Optional
import time

from config import config


@dataclass
class ExecutionCheck:
    allowed: bool
    reason: str
    ob: Optional[dict] = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "ob": self._compact_ob(self.ob),
            **(self.details or {}),
        }

    @staticmethod
    def _compact_ob(ob):
        if not isinstance(ob, dict):
            return None
        return {
            "source": ob.get("source"),
            "best_ask": ob.get("best_ask"),
            "best_bid": ob.get("best_bid"),
            "spread": ob.get("spread"),
            "age_sec": ob.get("age_sec"),
            "ask_depth_to_cap": ob.get("ask_depth_to_cap"),
            "weighted_avg_ask_to_cap": ob.get("weighted_avg_ask_to_cap"),
            "last_event_type": ob.get("last_event_type"),
        }


class ExecutionQualityGuard:
    """Recheck public orderbook immediately before execution."""

    def __init__(self, polymarket):
        self.polymarket = polymarket


    @staticmethod
    def _weighted_avg_for_order(asks, target_shares: float, max_price: float | None = None):
        """Return weighted average price for the *actual intended order size*.

        ``weighted_avg`` is set only when the visible asks can satisfy the target
        size. Partial/no-fill books are explicit depth failures; callers must not
        fall back to best_ask or weighted_avg_ask_to_cap.
        """
        try:
            need = float(target_shares or 0.0)
        except Exception:
            need = 0.0
        base = {
            "weighted_avg": None,
            "partial_weighted_avg": None,
            "filled_shares": 0.0,
            "target_shares": max(0.0, need),
            "notional": 0.0,
            "complete": False,
            "depth_exhausted": True,
        }
        if need <= 0 or not asks:
            return base
        rows = []
        for row in asks or []:
            try:
                price, size = row
                price = float(price)
                size = float(size)
            except Exception:
                continue
            if max_price is not None and price > float(max_price):
                continue
            if price <= 0 or size <= 0:
                continue
            rows.append((price, size))
        rows.sort(key=lambda x: x[0])
        remaining = need
        filled = 0.0
        notional = 0.0
        for price, size in rows:
            take = min(size, remaining)
            if take <= 0:
                continue
            filled += take
            notional += take * price
            remaining -= take
            if remaining <= 1e-9:
                break
        partial_avg = (notional / filled) if filled > 0 else None
        complete = filled + 1e-9 >= need
        return {
            "weighted_avg": partial_avg if complete else None,
            "partial_weighted_avg": partial_avg,
            "filled_shares": filled,
            "target_shares": need,
            "notional": notional,
            "complete": complete,
            "depth_exhausted": not complete,
        }

    def recheck_before_order(self, *, token_id: str, original_ob: dict, original_price: float,
                             expected_cost: float, estimated_shares: float,
                             max_token_price: float, max_spread: float,
                             min_liquidity_multiplier: float,
                             slippage_cap: Optional[float] = None,
                             max_weighted_avg_price: Optional[float] = None,
                             strategy_name: str = "") -> ExecutionCheck:
        if not config.EXECUTION_RECHECK_ENABLED:
            return ExecutionCheck(True, "execution_recheck_disabled", ob=original_ob)

        effective_slippage_cap = float(config.EXECUTION_GUARD_SLIPPAGE_CAP if slippage_cap is None else slippage_cap)
        cap = min(0.99, float(max_token_price) + effective_slippage_cap)
        started = time.time()
        fresh_ob = self.polymarket.get_orderbook_summary(token_id, max_price=cap)
        latency_ms = round((time.time() - started) * 1000, 3)

        if not fresh_ob or fresh_ob.get("best_ask") is None:
            return ExecutionCheck(False, "execution_recheck_orderbook_missing", details={"latency_ms": latency_ms})

        source = str(fresh_ob.get("source") or "")
        age = fresh_ob.get("age_sec")
        if source == "polymarket_ws" and age is not None and float(age) > float(config.EXECUTION_MAX_BOOK_AGE_SEC):
            return ExecutionCheck(False, "execution_recheck_orderbook_stale", ob=fresh_ob, details={"latency_ms": latency_ms})

        old_price = float(original_price or 0.0)
        try:
            new_price = float(fresh_ob.get("best_ask"))
        except Exception:
            return ExecutionCheck(False, "execution_recheck_orderbook_missing", ob=fresh_ob, details={"latency_ms": latency_ms, "best_ask": fresh_ob.get("best_ask")})
        if new_price <= 0:
            return ExecutionCheck(False, "execution_recheck_orderbook_missing", ob=fresh_ob, details={"latency_ms": latency_ms, "best_ask": fresh_ob.get("best_ask")})
        max_move = float(config.EXECUTION_MAX_PRICE_MOVE)
        if old_price > 0 and (new_price - old_price) > max_move:
            return ExecutionCheck(False, "execution_price_moved_up", ob=fresh_ob, details={
                "old_best_ask": old_price,
                "new_best_ask": new_price,
                "move": round(new_price - old_price, 6),
                "max_move": max_move,
                "latency_ms": latency_ms,
            })

        if new_price > float(max_token_price):
            return ExecutionCheck(False, "execution_price_above_strategy_cap", ob=fresh_ob, details={
                "new_best_ask": new_price,
                "max_token_price": float(max_token_price),
                "latency_ms": latency_ms,
            })

        spread = fresh_ob.get("spread")
        if spread is not None and float(spread) > float(max_spread):
            return ExecutionCheck(False, "execution_spread_widened", ob=fresh_ob, details={
                "spread": float(spread),
                "max_spread": float(max_spread),
                "latency_ms": latency_ms,
            })

        # Use the average price for the intended order size, not the average of
        # every ask up to the cap. The latter incorrectly rejected many $1
        # barrier_reclaim setups because thin, far-away asks distorted the mean.
        order_avg = self._weighted_avg_for_order(fresh_ob.get("asks") or [], float(estimated_shares or 0.0), cap)
        if not order_avg or order_avg.get("weighted_avg") is None or not order_avg.get("complete"):
            return ExecutionCheck(False, "execution_depth_evaporated", ob=fresh_ob, details={
                "available_shares": float((order_avg or {}).get("filled_shares") or 0.0),
                "required_shares": float((order_avg or {}).get("target_shares") or estimated_shares or 0.0),
                "partial_weighted_avg": (order_avg or {}).get("partial_weighted_avg"),
                "depth_exhausted": bool((order_avg or {}).get("depth_exhausted", True)),
                "latency_ms": latency_ms,
                "strategy": strategy_name,
            })
        weighted_avg_f = float(order_avg.get("weighted_avg"))
        order_details = {
            "weighted_avg": weighted_avg_f,
            "weighted_avg_source": "order_size",
            "target_shares": float(order_avg.get("target_shares") or estimated_shares or 0.0),
            "filled_shares_for_avg": float(order_avg.get("filled_shares") or 0.0),
            "notional_for_avg": float(order_avg.get("notional") or 0.0),
        }
        if max_weighted_avg_price is not None and weighted_avg_f > float(max_weighted_avg_price):
            return ExecutionCheck(False, "execution_weighted_avg_price_too_high", ob=fresh_ob, details={
                **order_details,
                "best_ask": new_price,
                "max_weighted_avg_price": float(max_weighted_avg_price),
                "slippage_cap": effective_slippage_cap,
                "strategy": strategy_name,
                "latency_ms": latency_ms,
            })
        if weighted_avg_f > new_price + effective_slippage_cap:
            return ExecutionCheck(False, "execution_slippage_cap_exceeded", ob=fresh_ob, details={
                **order_details,
                "best_ask": new_price,
                "slippage_cap": effective_slippage_cap,
                "strategy": strategy_name,
                "latency_ms": latency_ms,
            })

        required_depth = max(0.0, float(estimated_shares or 0.0) * float(min_liquidity_multiplier or 1.0))
        depth = float(fresh_ob.get("ask_depth_to_cap") or 0.0)
        if depth < required_depth:
            return ExecutionCheck(False, "execution_depth_evaporated", ob=fresh_ob, details={
                "depth": depth,
                "required_depth": required_depth,
                "latency_ms": latency_ms,
            })

        return ExecutionCheck(True, "execution_recheck_pass", ob=fresh_ob, details={
            "latency_ms": latency_ms,
            "old_best_ask": old_price,
            "new_best_ask": new_price,
            "source": source,
            "age_sec": age,
            "expected_cost": float(expected_cost or 0.0),
            "weighted_avg": weighted_avg_f,
            "weighted_avg_source": "order_size",
            "target_shares": float(order_avg.get("target_shares") or estimated_shares or 0.0),
        })
