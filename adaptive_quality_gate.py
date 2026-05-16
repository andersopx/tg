"""V14 adaptive stream quality gate.

The gate does not pre-select an all-day top list. It evaluates each opportunity when it
appears, then adapts the pass threshold according to live opportunity pressure, account
trade pace, realized PnL and loss streak.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from typing import Any, Dict

from config import config


@dataclass
class GateDecision:
    allowed: bool
    reason: str
    threshold: float
    score: float
    pace_ratio: float
    target_so_far: float
    trades_so_far: int
    pnl_today: float
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 6)
        return d


class AdaptiveQualityGate:
    def __init__(self, db=None):
        self.db = db

    def _flow_adjustment(self) -> Dict[str, Any]:
        if not getattr(config, "OPPORTUNITY_FLOW_GATE_ENABLED", True) or not self.db:
            return {"enabled": False, "penalty": 0.0}
        try:
            flow = self.db.get_recent_decision_flow_stats(config.FLOW_LOOKBACK_SEC)
        except Exception:
            return {"enabled": True, "penalty": 0.0, "error": "flow_stats_unavailable"}

        recent = int(flow.get("recent_decisions") or 0)
        if recent < int(config.FLOW_MIN_RECENT_DECISIONS):
            flow.update({"enabled": True, "penalty": 0.0, "reason": "insufficient_recent_flow"})
            return flow

        cpm = float(flow.get("candidates_per_min") or 0.0)
        high = max(0.01, float(config.FLOW_HIGH_CANDIDATES_PER_MIN))
        very_high = max(high + 0.01, float(config.FLOW_VERY_HIGH_CANDIDATES_PER_MIN))
        if cpm <= high:
            pressure_penalty = 0.0
        else:
            pressure = min(1.0, (cpm - high) / max(0.01, very_high - high))
            pressure_penalty = pressure * float(config.FLOW_PRESSURE_MAX_PENALTY)

        pass_ratio = float(flow.get("quality_pass_ratio") or 0.0)
        pass_penalty = 0.0
        if pass_ratio > float(config.FLOW_PASS_RATIO_TARGET):
            excess = min(1.0, (pass_ratio - float(config.FLOW_PASS_RATIO_TARGET)) / max(0.01, 1.0 - float(config.FLOW_PASS_RATIO_TARGET)))
            pass_penalty = excess * float(config.FLOW_PASS_RATIO_MAX_PENALTY)

        # If recent scores are very strong, allow a fraction of their percentile to pull the
        # threshold up. This is a rolling stream percentile, not a daily top-N selector.
        percentile_bonus = 0.0
        score_p75 = float(flow.get("score_p75") or 0.0)
        if score_p75 > float(config.BASE_QUALITY_SCORE):
            percentile_bonus = min(
                float(config.FLOW_PRESSURE_MAX_PENALTY),
                (score_p75 - float(config.BASE_QUALITY_SCORE)) * float(config.FLOW_SCORE_PERCENTILE_WEIGHT),
            )

        penalty = max(0.0, min(float(config.FLOW_PRESSURE_MAX_PENALTY) + float(config.FLOW_PASS_RATIO_MAX_PENALTY),
                               pressure_penalty + pass_penalty + percentile_bonus))
        flow.update({
            "enabled": True,
            "penalty": round(penalty, 4),
            "pressure_penalty": round(pressure_penalty, 4),
            "pass_ratio_penalty": round(pass_penalty, 4),
            "percentile_penalty": round(percentile_bonus, 4),
            "reason": "stream_pressure" if penalty > 0 else "normal_flow",
        })
        return flow

    def current_threshold(self) -> Dict[str, Any]:
        now = datetime.now(timezone.utc)
        seconds_today = now.hour * 3600 + now.minute * 60 + now.second
        hours_elapsed = max(0.05, seconds_today / 3600.0)
        max_daily = max(1, int(config.MAX_TRADES_PER_DAY))
        target_hourly = float(config.TARGET_TRADES_PER_HOUR or (max_daily / 24.0))
        target_so_far = min(float(max_daily), max(1.0, hours_elapsed * target_hourly))
        trades = 0
        pnl = 0.0
        losses = 0
        if self.db:
            try:
                trades = int(self.db.get_today_trade_count())
                pnl = float(self.db.get_today_realized_pnl())
                losses = int(self.db.get_consecutive_losses())
            except Exception:
                pass
        pace_ratio = trades / max(1.0, target_so_far)
        threshold = float(config.BASE_QUALITY_SCORE)
        adjustments = []

        # Ahead of pace: be more picky. Behind pace: relax, but never below MIN_QUALITY_SCORE.
        if pace_ratio > 1.25:
            delta = min(float(config.QUALITY_AHEAD_PENALTY), (pace_ratio - 1.0) * 8.0)
            threshold += delta
            adjustments.append({"name": "ahead_of_pace", "delta": round(delta, 4)})
        elif pace_ratio < 0.55:
            delta = -min(float(config.QUALITY_BEHIND_RELAX), (0.75 - pace_ratio) * 6.0)
            threshold += delta
            adjustments.append({"name": "behind_pace", "delta": round(delta, 4)})
        if pnl < 0:
            delta = min(float(config.QUALITY_DAILY_LOSS_PENALTY), abs(pnl) / max(1.0, float(config.TEST_BANKROLL)) * 50.0)
            threshold += delta
            adjustments.append({"name": "daily_loss", "delta": round(delta, 4)})
        if losses > 0:
            delta = min(12.0, losses * float(config.QUALITY_LOSS_STREAK_PENALTY))
            threshold += delta
            adjustments.append({"name": "loss_streak", "delta": round(delta, 4)})
        if pnl > 0 and pace_ratio < 1.10:
            delta = -min(float(config.QUALITY_WINNING_BONUS), pnl / max(1.0, float(config.TEST_BANKROLL)) * 10.0)
            threshold += delta
            adjustments.append({"name": "winning_bonus", "delta": round(delta, 4)})

        flow = self._flow_adjustment()
        flow_penalty = float(flow.get("penalty") or 0.0)
        if flow_penalty:
            threshold += flow_penalty
            adjustments.append({"name": "stream_pressure", "delta": round(flow_penalty, 4)})


        threshold = max(float(config.MIN_QUALITY_SCORE), min(float(config.MAX_QUALITY_SCORE), threshold))
        return {
            "threshold": round(threshold, 4),
            "pace_ratio": round(pace_ratio, 4),
            "target_so_far": round(target_so_far, 2),
            "trades_so_far": trades,
            "pnl_today": round(pnl, 4),
            "consecutive_losses": losses,
            "hours_elapsed": round(hours_elapsed, 2),
            "flow": flow,
            "adjustments": adjustments,
        }

    def evaluate(self, *, score_obj, signal=None) -> GateDecision:
        state = self.current_threshold()
        threshold = float(state["threshold"])
        score = float(getattr(score_obj, "final_score", 0.0) or 0.0)
        details = {"score": score_obj.to_dict() if hasattr(score_obj, "to_dict") else {}}
        details.update(state)
        if not getattr(config, "QUALITY_GATE_ENABLED", True):
            return GateDecision(True, "quality_gate_disabled", threshold, score, state["pace_ratio"], state["target_so_far"], state["trades_so_far"], state["pnl_today"], details)
        if getattr(score_obj, "hard_reason", ""):
            return GateDecision(False, getattr(score_obj, "hard_reason"), threshold, score, state["pace_ratio"], state["target_so_far"], state["trades_so_far"], state["pnl_today"], details)
        category = getattr(score_obj, "category", "main")
        ev = float(getattr(score_obj, "expected_value", 0.0) or 0.0)
        prob = float(getattr(score_obj, "estimated_win_prob", 0.0) or 0.0)
        if category == "main" and prob < float(config.QUALITY_MAIN_MIN_WIN_PROB):
            return GateDecision(False, "quality_win_prob_too_low", threshold, score, state["pace_ratio"], state["target_so_far"], state["trades_so_far"], state["pnl_today"], details)
        if category == "odds":
            if prob < float(config.QUALITY_ODDS_MIN_MODEL_PROB):
                return GateDecision(False, "quality_odds_prob_too_low", threshold, score, state["pace_ratio"], state["target_so_far"], state["trades_so_far"], state["pnl_today"], details)
            if ev < float(config.QUALITY_MIN_EXPECTED_VALUE):
                return GateDecision(False, "quality_ev_too_low", threshold, score, state["pace_ratio"], state["target_so_far"], state["trades_so_far"], state["pnl_today"], details)
        if score < threshold:
            return GateDecision(False, "quality_score_below_dynamic_threshold", threshold, score, state["pace_ratio"], state["target_so_far"], state["trades_so_far"], state["pnl_today"], details)
        return GateDecision(True, "quality_gate_pass", threshold, score, state["pace_ratio"], state["target_so_far"], state["trades_so_far"], state["pnl_today"], details)
