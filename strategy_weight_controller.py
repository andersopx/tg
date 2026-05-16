"""Strategy pause/weight helper for V14.

This lightweight controller reads existing asset_strategy_stats and returns a gate multiplier or pause reason.
It does not mutate balances, credentials, or order state.
"""
from __future__ import annotations
from typing import Tuple
from config import config


class StrategyWeightController:
    def __init__(self, db=None):
        self.db = db

    def status(self, asset: str, strategy_name: str) -> Tuple[bool, float, str]:
        if not self.db or not getattr(config, "AUTO_STRATEGY_WEIGHTING", True):
            return True, 1.0, "no_history"
        try:
            rows = self.db.get_asset_strategy_stats()
            for r in rows:
                if str(r.get("asset", "")).upper() == str(asset).upper() and str(r.get("strategy_name", "")).lower() == str(strategy_name).lower():
                    trades = int(r.get("trades") or 0)
                    if trades < int(config.STRATEGY_MIN_SAMPLES_FOR_GATE):
                        return True, 1.0, "sample_building"
                    loss_streak = int(r.get("loss_streak") or 0)
                    win_rate = float(r.get("win_rate") or 0.0)
                    weight = float(r.get("weight") or 1.0)
                    if loss_streak >= int(config.STRATEGY_PAUSE_LOSS_STREAK):
                        return False, max(0.10, weight * 0.5), "strategy_loss_streak_pause"
                    if win_rate < float(config.STRATEGY_MIN_RECENT_WIN_RATE):
                        return False, max(0.10, weight * 0.5), "strategy_win_rate_pause"
                    return True, max(0.25, min(1.50, weight)), "active"
        except Exception:
            return True, 1.0, "history_unavailable"
        return True, 1.0, "no_stats"
