"""High-win-rate guard.

This layer is intentionally conservative: it cannot guarantee any fixed win rate,
but it blocks candidates that are inconsistent with a win-rate-first goal.
It combines the current model probability/edge with recent realized strategy
stats and loss streaks.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict

from config import config


@dataclass
class HighWinRateDecision:
    allowed: bool
    reason: str
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.details or {})
        out.update({"allowed": self.allowed, "reason": self.reason})
        return out


def wilson_lower_bound(wins: int, n: int, z: float = 1.2815515655446004) -> float:
    """Wilson lower confidence bound for a binomial win rate.

    z=1.28 is roughly an 80% one-sided guard; z=1.64 is stricter.  We use this
    instead of trusting tiny-sample raw win rates such as 3/3.
    """
    wins = int(wins or 0)
    n = int(n or 0)
    if n <= 0:
        return 0.0
    p = max(0.0, min(1.0, wins / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = p + z2 / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z2 / (4.0 * n)) / n)
    return max(0.0, min(1.0, (centre - margin) / denom))


class HighWinRateGuard:
    def __init__(self, db=None):
        self.db = db

    def evaluate(self, *, signal, strategy_name: str, model_prob: float,
                 token_price: float, edge_after_fees: float,
                 market_mid: float = 0.0) -> HighWinRateDecision:
        if not bool(getattr(config, "HIGH_WIN_RATE_MODE", True)):
            return HighWinRateDecision(True, "high_win_rate_guard_disabled", {"enabled": False})

        strategy = str(strategy_name or getattr(signal, "pattern", "") or "").lower()
        if strategy.startswith("barrier_reclaim") or "barrier_reclaim" in strategy:
            strategy = "barrier_reclaim"

        model_prob = max(0.0, min(0.99, float(model_prob or 0.0)))
        token_price = max(0.0, min(1.0, float(token_price or 0.0)))
        edge_after_fees = float(edge_after_fees or 0.0)
        target = float(getattr(config, "TARGET_WIN_RATE", 0.75) or 0.75)
        min_model = float(getattr(config, "HIGH_WIN_RATE_MIN_MODEL_PROB", 0.74) or 0.74)
        unknown_min_model = float(getattr(config, "HIGH_WIN_RATE_UNKNOWN_STRATEGY_MIN_MODEL_PROB", 0.78) or 0.78)
        min_edge = float(getattr(config, "HIGH_WIN_RATE_MIN_EDGE_AFTER_FEES", 0.035) or 0.035)
        max_token = float(getattr(config, "HIGH_WIN_RATE_MAX_TOKEN_PRICE", 0.92) or 0.92)
        min_samples = int(getattr(config, "HIGH_WIN_RATE_MIN_RECENT_SAMPLES", 8) or 8)
        reject_wr = float(getattr(config, "HIGH_WIN_RATE_REJECT_RECENT_WIN_RATE", 0.50) or 0.50)
        max_loss_streak = int(getattr(config, "HIGH_WIN_RATE_MAX_STRATEGY_LOSS_STREAK", 2) or 2)
        z = float(getattr(config, "HIGH_WIN_RATE_WILSON_Z", 1.2815515655446004) or 1.2815515655446004)
        min_wilson = float(getattr(config, "HIGH_WIN_RATE_MIN_WILSON_LOWER_BOUND", 0.55) or 0.55)

        details: Dict[str, Any] = {
            "enabled": True,
            "target_win_rate": target,
            "strategy_name": strategy,
            "model_probability": round(model_prob, 6),
            "token_price": round(token_price, 6),
            "market_mid": round(float(market_mid or 0.0), 6),
            "edge_after_fees": round(edge_after_fees, 6),
            "min_model_probability": min_model,
            "unknown_strategy_min_model_probability": unknown_min_model,
            "min_edge_after_fees": min_edge,
            "max_token_price": max_token,
        }

        stats = {}
        if self.db is not None and hasattr(self.db, "get_strategy_outcome_stats"):
            try:
                stats = self.db.get_strategy_outcome_stats(
                    asset=getattr(signal, "asset", ""),
                    timeframe=getattr(signal, "timeframe", ""),
                    strategy_name=strategy,
                    limit=int(getattr(config, "HIGH_WIN_RATE_STATS_LOOKBACK", 80) or 80),
                    include_shadow=bool(getattr(config, "HIGH_WIN_RATE_USE_SHADOW_STATS", True)),
                ) or {}
            except Exception as e:
                stats = {"error": str(e)}
        n = int(stats.get("trades") or 0)
        wins = int(stats.get("wins") or 0)
        win_rate = float(stats.get("win_rate") or 0.0)
        loss_streak = int(stats.get("loss_streak") or 0)
        wlb = wilson_lower_bound(wins, n, z=z)
        details["strategy_stats"] = {
            "trades": n,
            "wins": wins,
            "losses": int(stats.get("losses") or max(0, n - wins)),
            "win_rate": round(win_rate, 6),
            "wilson_lower_bound": round(wlb, 6),
            "loss_streak": loss_streak,
            "pnl": round(float(stats.get("pnl") or 0.0), 6),
            "avg_pnl": round(float(stats.get("avg_pnl") or 0.0), 6),
        }

        if token_price > max_token:
            return HighWinRateDecision(False, "high_win_rate_token_too_expensive", details)
        if edge_after_fees < min_edge:
            return HighWinRateDecision(False, "high_win_rate_edge_low", details)
        if loss_streak >= max_loss_streak and n >= max(2, min_samples // 2):
            return HighWinRateDecision(False, "high_win_rate_strategy_loss_streak", details)
        if n >= min_samples and win_rate < reject_wr:
            return HighWinRateDecision(False, "high_win_rate_recent_winrate_low", details)

        # Known strong buckets can be allowed with a little less raw model certainty,
        # but only if both raw and Wilson-adjusted stats are healthy and PnL is not negative.
        promoted_by_stats = (
            n >= min_samples
            and win_rate >= target
            and wlb >= min_wilson
            and float(stats.get("pnl") or 0.0) >= 0.0
        )
        details["promoted_by_stats"] = bool(promoted_by_stats)

        if promoted_by_stats and model_prob >= max(0.60, target - 0.10):
            return HighWinRateDecision(True, "high_win_rate_stats_promoted", details)

        # Unknown or weak-history buckets must meet a high raw probability.
        required_model = unknown_min_model if n < min_samples else min_model
        details["required_model_probability"] = required_model
        if model_prob < required_model:
            return HighWinRateDecision(False, "high_win_rate_model_prob_low", details)

        return HighWinRateDecision(True, "high_win_rate_model_pass", details)
