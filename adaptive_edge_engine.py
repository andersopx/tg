"""v14.3.2 Adaptive Edge Engine.

The goal is not a fixed 75% win-rate gate.  A binary prediction-market ticket
is profitable only when the calibrated chance of winning is high enough for the
entry price, fees, slippage, time remaining, volatility and recent rule health.

This module runs before Profit Rule Engine and writes an `adaptive_edge` object
into signal indicators / signal payloads.  Profit Rule Engine can then decide
whether the candidate is PROMOTE / SHADOW / OBSERVE / REJECT.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional

from config import config


@dataclass
class AdaptiveEdgeDecision:
    allowed: bool
    decision: str
    reason: str
    details: Dict[str, Any]

    def to_dict(self) -> Dict[str, Any]:
        out = dict(self.details or {})
        out.update({"allowed": self.allowed, "decision": self.decision, "reason": self.reason})
        return out


def _f(x: Any, default: float = 0.0) -> float:
    try:
        if x is None:
            return default
        return float(x)
    except Exception:
        return default


def _i(x: Any, default: int = 0) -> int:
    try:
        if x is None:
            return default
        return int(float(x))
    except Exception:
        return default


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 1.0 if x > 0 else 0.0


class AdaptiveEdgeEngine:
    def __init__(self, db=None):
        self.db = db

    def evaluate(
        self,
        *,
        signal: Any,
        asset: str,
        timeframe: str,
        direction: str,
        entry_price: float,
        model_prob: float,
        market_mid: float = 0.0,
        model_market_gap: float = 0.0,
        edge_after_fees: float = 0.0,
        fee_per_share: float = 0.0,
        spread: Optional[float] = None,
        mode: str = "shadow",
    ) -> AdaptiveEdgeDecision:
        if not bool(getattr(config, "ADAPTIVE_EDGE_ENGINE_ENABLED", True)):
            return AdaptiveEdgeDecision(True, "SHADOW", "adaptive_edge_disabled", {"enabled": False})

        indicators = getattr(signal, "indicators", {}) or {}
        direction = str(direction or getattr(signal, "direction", "") or "").capitalize()
        timeframe = str(timeframe or getattr(signal, "timeframe", "5m") or "5m").lower()
        entry = max(0.0001, min(0.9999, _f(entry_price)))
        model_prob = max(0.0, min(0.99, _f(model_prob)))
        market_mid = max(0.0, min(0.99, _f(market_mid)))
        gap = abs(_f(model_market_gap, abs(model_prob - market_mid)))
        fee = max(0.0, _f(fee_per_share))
        spread = max(0.0, _f(spread, 0.0))
        edge_after_fees = _f(edge_after_fees, model_prob - entry - fee)

        seconds_left = _i(getattr(signal, "seconds_left", 0) or indicators.get("seconds_left") or indicators.get("seconds_remaining"), 0)
        if seconds_left <= 0:
            seconds_left = _i(indicators.get("remaining_seconds") or indicators.get("seconds_to_close"), 0)
        min_left = int(getattr(config, "ADAPTIVE_EDGE_MIN_SECONDS_LEFT_15M", 45) if "15" in timeframe else getattr(config, "ADAPTIVE_EDGE_MIN_SECONDS_LEFT_5M", 20))
        max_left = int(getattr(config, "ADAPTIVE_EDGE_MAX_SECONDS_LEFT_15M", 600) if "15" in timeframe else getattr(config, "ADAPTIVE_EDGE_MAX_SECONDS_LEFT_5M", 240))

        dist = _f(getattr(signal, "distance_to_reference", 0.0) or indicators.get("distance_to_reference"), 0.0)
        sigma = abs(_f(getattr(signal, "sigma_remaining", 0.0) or indicators.get("sigma_remaining"), 0.0))
        # Use a floor so tiny sigma does not explode the z-score.
        sigma_floor = max(abs(_f(indicators.get("per_min_vol"), 0.0)) * max(seconds_left / 60.0, 1.0), 1e-9)
        sigma_eff = max(sigma, sigma_floor, 1e-9)
        z = dist / sigma_eff
        directional_z = z if direction == "Up" else -z

        # Settlement probability from distance to line.  It is intentionally
        # conservative and blended with raw model probability and market mid.
        settlement_prob = _sigmoid(0.95 * directional_z)
        raw_settlement = settlement_prob

        # Penalize bad timing.  Too early means more random walk; too late means the
        # market may already have repriced and execution can be stale.
        timing_penalty = 0.0
        if seconds_left and seconds_left < min_left:
            timing_penalty += min(0.18, (min_left - seconds_left) / max(min_left, 1) * 0.18)
        if seconds_left and seconds_left > max_left:
            timing_penalty += min(0.18, (seconds_left - max_left) / max(max_left, 1) * 0.12)

        # Directional velocity guard.  velocity_to_line is positive when moving toward
        # the reference in old strategy code; recent_change_2m is raw price change.
        velocity_to_line = _f(indicators.get("velocity_to_line"), 0.0)
        recent_change = _f(indicators.get("recent_change_2m") or indicators.get("recent_change"), 0.0)
        # For Up, sharply negative recent change is bad; for Down, sharply positive is bad.
        adverse_velocity = 0.0
        if direction == "Up" and recent_change < 0:
            adverse_velocity = min(1.0, abs(recent_change) / max(abs(dist), sigma_eff, 1e-9))
        elif direction == "Down" and recent_change > 0:
            adverse_velocity = min(1.0, abs(recent_change) / max(abs(dist), sigma_eff, 1e-9))
        velocity_penalty = adverse_velocity * float(getattr(config, "ADAPTIVE_EDGE_MAX_NEGATIVE_VELOCITY_PENALTY", 0.10))

        w_model = float(getattr(config, "ADAPTIVE_EDGE_PROB_BLEND_MODEL", 0.45))
        w_settle = float(getattr(config, "ADAPTIVE_EDGE_PROB_BLEND_SETTLEMENT", 0.40))
        w_market = float(getattr(config, "ADAPTIVE_EDGE_PROB_BLEND_MARKET", 0.15))
        w_sum = max(0.0001, w_model + w_settle + w_market)
        blended = (model_prob * w_model + settlement_prob * w_settle + market_mid * w_market) / w_sum
        calibrated_probability = max(0.0, min(0.99, blended - timing_penalty - velocity_penalty))

        # Dynamic required probability.  Break-even is roughly entry + taker fee.
        # Add risk buffers instead of using a fixed 75% threshold.
        base_buffer = float(getattr(config, "ADAPTIVE_EDGE_BASE_RISK_BUFFER", 0.06))
        spread_buffer = min(0.10, spread * 0.60)
        gap_buffer = min(0.16, gap * 0.22)
        time_buffer = timing_penalty * 0.80
        velocity_buffer = velocity_penalty
        required_probability = max(0.01, min(0.95, entry + fee + base_buffer + spread_buffer + gap_buffer + time_buffer + velocity_buffer))

        win_profit = max(0.0, (1.0 / entry) - 1.0)
        expected_value = calibrated_probability * win_profit - (1.0 - calibrated_probability)
        margin = calibrated_probability - required_probability

        # Quality is not the same as EV.  It captures direction confidence and rule safety.
        distance_score = max(0.0, min(1.0, (directional_z + 0.5) / 2.5))
        timing_score = 1.0 - min(1.0, timing_penalty / 0.24)
        velocity_score = 1.0 - min(1.0, velocity_penalty / max(float(getattr(config, "ADAPTIVE_EDGE_MAX_NEGATIVE_VELOCITY_PENALTY", 0.10)), 0.0001))
        ev_score = max(0.0, min(1.0, (expected_value + 0.20) / 1.20))
        quality_score = max(0.0, min(1.0, 0.30 * calibrated_probability + 0.25 * distance_score + 0.15 * timing_score + 0.15 * velocity_score + 0.15 * ev_score))

        min_ev_shadow = float(getattr(config, "ADAPTIVE_EDGE_MIN_EV_SHADOW", 0.08))
        min_ev_live = float(getattr(config, "ADAPTIVE_EDGE_MIN_EV_LIVE", 0.16))
        min_q_shadow = float(getattr(config, "ADAPTIVE_EDGE_MIN_QUALITY_SHADOW", 0.56))
        min_q_live = float(getattr(config, "ADAPTIVE_EDGE_MIN_QUALITY_LIVE", 0.70))

        decision = "OBSERVE"
        reason = "adaptive_edge_observe_only"
        allowed = False
        if seconds_left and seconds_left < max(5, min_left // 2):
            decision, reason = "REJECT", "adaptive_edge_too_late"
        elif expected_value <= 0:
            decision, reason = "REJECT", "adaptive_edge_ev_non_positive"
        elif margin < 0:
            decision, reason = "OBSERVE", "adaptive_edge_required_probability_not_met"
        elif mode == "live" and expected_value >= min_ev_live and quality_score >= min_q_live:
            decision, reason, allowed = "PROMOTE", "adaptive_edge_live_candidate", True
        elif expected_value >= min_ev_shadow and quality_score >= min_q_shadow:
            decision, reason, allowed = "SHADOW", "adaptive_edge_shadow_candidate", True
        else:
            decision, reason = "OBSERVE", "adaptive_edge_quality_low"

        details = {
            "enabled": True,
            "asset": asset,
            "timeframe": timeframe,
            "direction": direction,
            "entry_price": round(entry, 6),
            "raw_model_probability": round(model_prob, 6),
            "market_mid_probability": round(market_mid, 6),
            "raw_settlement_probability": round(raw_settlement, 6),
            "calibrated_probability": round(calibrated_probability, 6),
            "required_probability": round(required_probability, 6),
            "expected_value": round(expected_value, 6),
            "ev_margin": round(margin, 6),
            "quality_score": round(quality_score, 6),
            "win_profit_multiple": round(win_profit, 6),
            "seconds_left": seconds_left,
            "min_seconds_left": min_left,
            "max_seconds_left": max_left,
            "distance_to_reference": round(dist, 6),
            "sigma_remaining": round(sigma_eff, 6),
            "directional_z": round(directional_z, 6),
            "recent_change_2m": round(recent_change, 6),
            "velocity_to_line": round(velocity_to_line, 6),
            "timing_penalty": round(timing_penalty, 6),
            "velocity_penalty": round(velocity_penalty, 6),
            "spread_buffer": round(spread_buffer, 6),
            "gap_buffer": round(gap_buffer, 6),
            "fee_per_share": round(fee, 6),
            "model_market_gap": round(gap, 6),
        }
        return AdaptiveEdgeDecision(allowed, decision, reason, details)
