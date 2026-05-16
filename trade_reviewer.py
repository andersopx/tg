"""
Intelligent post-trade review for high-odds 5m Polymarket strategies.

This module does not touch wallet/auth/relayer logic. It only analyzes resolved trades,
classifies why they won/lost, and records per-asset/per-strategy stats for later tuning.
"""
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple


@dataclass
class ReviewResult:
    trade_id: int
    asset: str
    strategy_name: str
    outcome: str
    pnl: float
    entry_price: float
    implied_multiple: float
    grade: str
    primary_reason: str
    tags: List[str]
    metrics: Dict[str, Any]
    recommendation: str
    note: str


class TradeReviewer:
    """Rule-based reviewer designed for odds/Price-to-Beat 5m setups."""

    def review(self, trade: dict) -> ReviewResult:
        sig = self._parse_json(trade.get("signal_data"))
        indicators = sig.get("indicators", {}) or {}
        asset = str(trade.get("asset") or sig.get("asset") or indicators.get("asset") or "BTC").upper()
        strategy_name = str(sig.get("strategy_name") or indicators.get("strategy_name") or sig.get("pattern") or "unknown")
        direction = str(trade.get("direction") or "")
        outcome = str(trade.get("outcome") or "")
        pnl = float(trade.get("pnl") or 0)
        entry = float(trade.get("avg_price") or trade.get("entry_price") or 0)
        multiple = (1.0 / entry) if entry > 0 else 0.0
        model_prob = float(trade.get("model_probability") or sig.get("model_probability") or sig.get("confidence") or 0)
        edge = float(trade.get("edge_after_fees") or sig.get("edge_after_fees") or 0)
        seconds_left = int(sig.get("seconds_left") or indicators.get("seconds_left") or 0)
        distance = self._float(indicators.get("distance_to_reference"), 0.0)
        velocity = self._float(indicators.get("velocity_to_line"), 0.0)
        sigma = self._float(indicators.get("sigma_remaining"), 0.0)
        shrink_ratio = self._float(indicators.get("shrink_ratio"), 0.0)
        upper_ratio = self._float(indicators.get("upper_wick_body_ratio"), 0.0)
        lower_ratio = self._float(indicators.get("lower_wick_body_ratio"), 0.0)
        wick_ratio = max(upper_ratio, lower_ratio)
        recent_change = self._float(indicators.get("recent_change_2m"), 0.0)
        market_mid = self._float(sig.get("market_mid_probability"), 0.0)
        gap = self._float(sig.get("model_market_gap"), 0.0)
        fee = self._float(sig.get("fee_per_share"), 0.0)

        tags: List[str] = []
        if entry <= 0:
            tags.append("bad_entry_price")
        elif entry <= 0.07:
            tags.append("lottery_price")
        elif entry <= 0.25:
            tags.append("high_multiple_price")
        elif entry >= 0.70:
            tags.append("high_price_low_multiple")

        if wick_ratio >= 1.3:
            tags.append("wick_rejection_present")
        if velocity > 0:
            tags.append("line_closing")
        else:
            tags.append("line_not_closing")
        if sigma and abs(distance) > sigma * 2.5:
            tags.append("far_from_line")
        if gap > 0.25:
            tags.append("model_book_gap_large")
        if seconds_left < 20:
            tags.append("late_entry")
        elif seconds_left > 220:
            tags.append("early_entry")

        if outcome == "win":
            primary = self._win_reason(strategy_name, wick_ratio, velocity, shrink_ratio, multiple)
            grade = self._grade_win(entry, pnl, edge, wick_ratio, velocity)
            rec = self._win_recommendation(strategy_name, entry, multiple, edge)
            icon = "✅"
        else:
            primary = self._loss_reason(strategy_name, entry, distance, velocity, sigma, wick_ratio, recent_change, seconds_left, gap, edge)
            grade = self._grade_loss(entry, edge, velocity, wick_ratio, gap)
            rec = self._loss_recommendation(primary, strategy_name)
            icon = "❌"

        metrics = {
            "model_probability": round(model_prob, 4),
            "edge_after_fees": round(edge, 4),
            "entry_price": round(entry, 4),
            "implied_multiple": round(multiple, 2),
            "seconds_left": seconds_left,
            "distance_to_reference": round(distance, 4),
            "sigma_remaining": round(sigma, 4),
            "velocity_to_line": round(velocity, 4),
            "shrink_ratio": round(shrink_ratio, 4),
            "wick_body_ratio": round(wick_ratio, 4),
            "recent_change_2m": round(recent_change, 6),
            "market_mid_probability": round(market_mid, 4),
            "model_market_gap": round(gap, 4),
            "fee_per_share": round(fee, 4),
        }

        note = (
            f"{icon} {asset} {strategy_name} {direction} | {outcome.upper()} | "
            f"入场 ${entry:.3f} / 理论倍率 {multiple:.2f}x | PnL ${pnl:+.2f} | "
            f"模型 {model_prob:.1%} / 费后边际 {edge:.1%} | "
            f"剩余 {seconds_left}s | 距线 ${distance:.2f} | 回归速度 ${velocity:.2f} | "
            f"原因：{primary} | 建议：{rec}"
        )

        return ReviewResult(
            trade_id=int(trade.get("id") or 0),
            asset=asset,
            strategy_name=strategy_name,
            outcome=outcome,
            pnl=pnl,
            entry_price=entry,
            implied_multiple=multiple,
            grade=grade,
            primary_reason=primary,
            tags=tags,
            metrics=metrics,
            recommendation=rec,
            note=note,
        )

    def _win_reason(self, strategy: str, wick_ratio: float, velocity: float, shrink_ratio: float, multiple: float) -> str:
        if "wick" in strategy and wick_ratio >= 1.3:
            return "长尾拒绝有效，价格按预期回归 Price-to-Beat"
        if "odds" in strategy and velocity > 0:
            return "赔率低位时价格已经向规则线回归，低价 token 成功兑现"
        if "snipe" in strategy:
            return "临近结算时价格控制规则线，抢线成功"
        if multiple >= 8:
            return "高倍率低价单命中"
        return "方向判断正确，持有到结算兑现"

    def _loss_reason(self, strategy: str, entry: float, distance: float, velocity: float, sigma: float,
                     wick_ratio: float, recent_change: float, seconds_left: int, gap: float, edge: float) -> str:
        if velocity <= 0 and ("odds" in strategy or "wick" in strategy):
            return "价格没有继续向 Price-to-Beat 回归，低价反转失败"
        if sigma and abs(distance) > sigma * 3:
            return "入场时离规则线过远，反转所需幅度偏大"
        if "wick" in strategy and wick_ratio < 1.3:
            return "长尾拒绝强度不足，形态质量不够"
        if seconds_left < 15:
            return "入场过晚，留给穿越规则线的时间不够"
        if seconds_left > 240:
            return "入场过早，后续噪音覆盖了初始判断"
        if gap > 0.25:
            return "模型与盘口分歧过大，市场可能更接近真实概率"
        if edge < 0.01:
            return "费后期望值太薄，盘口价格不够便宜"
        return "方向判断失败，价格未在结算前穿越/保持规则线"

    def _grade_win(self, entry: float, pnl: float, edge: float, wick_ratio: float, velocity: float) -> str:
        score = 0
        if entry and entry <= 0.20: score += 2
        if pnl > 2: score += 2
        if edge > 0.05: score += 1
        if wick_ratio >= 1.3: score += 1
        if velocity > 0: score += 1
        return "A" if score >= 5 else "B" if score >= 3 else "C"

    def _grade_loss(self, entry: float, edge: float, velocity: float, wick_ratio: float, gap: float) -> str:
        score = 0
        if entry and entry <= 0.12: score += 1  # cheap loss is acceptable for high-multiple systems
        if edge > 0.03: score += 1
        if velocity > 0: score += 1
        if wick_ratio >= 1.3: score += 1
        if gap <= 0.20: score += 1
        return "B" if score >= 4 else "C" if score >= 2 else "D"

    def _win_recommendation(self, strategy: str, entry: float, multiple: float, edge: float) -> str:
        if multiple >= 8:
            return "保留该类高倍率信号，继续按单笔小额执行"
        if edge > 0.05:
            return "该参数区间有效，继续观察样本扩大后的稳定性"
        return "胜单但边际一般，不建议因单笔胜利立刻放宽参数"

    def _loss_recommendation(self, reason: str, strategy: str) -> str:
        if "没有继续向" in reason:
            return "提高最小回归速度或要求连续两次距离收窄"
        if "离规则线过远" in reason:
            return "降低最大距离上限，避免追过远的低价 token"
        if "长尾拒绝强度不足" in reason:
            return "提高 WICK_MIN_WICK_BODY_RATIO / WICK_MIN_WICK_RANGE_RATIO"
        if "入场过晚" in reason:
            return "提高该策略最小剩余秒数，避免最后噪音"
        if "入场过早" in reason:
            return "缩短最大剩余秒数或要求更强盘口滞后"
        if "模型与盘口分歧" in reason:
            return "降低 MAX_MARKET_PROB_GAP 或提高盘口一致性要求"
        if "费后期望值" in reason:
            return "提高 ODDS_EV_MIN_EXPECTED_VALUE / MIN_EDGE_AFTER_FEES"
        return "继续保留样本，达到样本阈值后再自动降权"

    @staticmethod
    def _parse_json(raw) -> Dict[str, Any]:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                return {}
        return {}

    @staticmethod
    def _float(v, default=0.0) -> float:
        try:
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return default
            return float(v)
        except Exception:
            return default
