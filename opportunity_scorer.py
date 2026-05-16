"""V14 opportunity scoring.

A candidate is not traded because it exists; it must pass a live quality score.
The scorer blends win probability, odds EV, book quality, line reversion and historical strategy weight.
It is intentionally conservative and deterministic so Telegram can explain every pass/skip.
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Dict, Optional
import math

from config import config


HIGH_ODDS_STRATEGIES = {
    "odds_ev", "odds_lag", "odds_reversal", "wick_rejection_odds", "lottery_reversal", "barrier_reclaim"
}
MAIN_PROB_STRATEGIES = {"prob_edge", "late_line_snipe", "momentum_follow"}
PAIR_STRATEGIES = {"two_leg_hedge"}


@dataclass
class OpportunityScore:
    final_score: float
    estimated_win_prob: float
    token_price: float
    gross_multiple: float
    expected_value: float
    edge_after_fees: float
    book_quality_score: float
    line_reversion_score: float
    odds_lag_score: float
    strategy_history_score: float
    strategy_name: str
    category: str
    hard_reason: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        # keep Telegram compact
        for k, v in list(d.items()):
            if isinstance(v, float):
                d[k] = round(v, 6)
        return d


def clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except Exception:
        return lo


def strategy_name_from(signal) -> str:
    indicators = getattr(signal, "indicators", {}) or {}
    return str(indicators.get("strategy_name") or getattr(signal, "pattern", "") or "unknown").lower()


class OpportunityScorer:
    def __init__(self, db=None):
        self.db = db

    def score(self, *, signal, token_price: float, ob: Dict[str, Any], opp_ob: Optional[Dict[str, Any]],
              model_prob: float, edge_after_fees: float, fee_per_share: float,
              expected_cost: float = 1.0) -> OpportunityScore:
        strategy = strategy_name_from(signal)
        category = "odds" if strategy in HIGH_ODDS_STRATEGIES else ("pair" if strategy in PAIR_STRATEGIES else "main")
        token_price = max(0.0001, min(0.9999, float(token_price or 0)))
        model_prob = clamp(model_prob, 0.0, 0.98)
        fee_per_share = max(0.0, float(fee_per_share or 0.0))
        gross_multiple = 1.0 / token_price
        slippage_buffer = max(0.0, float(config.MARKET_BUY_SLIPPAGE or 0.0))
        expected_value = (model_prob * gross_multiple) - 1.0 - fee_per_share - slippage_buffer

        hard_reason = self._hard_reject_reason(token_price, ob, opp_ob, expected_cost)
        book_quality = self._book_quality(token_price, ob, opp_ob, expected_cost)
        line_score = self._line_reversion_score(signal)
        odds_lag = self._odds_lag_score(signal)
        hist = self._strategy_history_score(getattr(signal, "asset", "BTC"), strategy)

        # Main strategies need probability; odds strategies need EV + some probability.
        if category == "main":
            prob_score = clamp((model_prob - 0.50) / max(0.01, 0.40))  # 50%=>0, 90%=>1
            ev_score = clamp((edge_after_fees + 0.02) / 0.20)
            final = 100.0 * (0.58 * prob_score + 0.18 * ev_score + 0.12 * book_quality + 0.07 * line_score + 0.05 * hist)
        elif category == "pair":
            ev_score = clamp((expected_value + 0.05) / 0.50)
            final = 100.0 * (0.38 * ev_score + 0.28 * book_quality + 0.18 * line_score + 0.16 * hist)
        else:
            ev_score = clamp((expected_value - 0.02) / 0.80)  # high odds can have high EV with lower win rate
            prob_floor = clamp(model_prob / max(0.01, 0.30))
            final = 100.0 * (0.42 * ev_score + 0.18 * prob_floor + 0.17 * line_score + 0.13 * book_quality + 0.06 * odds_lag + 0.04 * hist)

        return OpportunityScore(
            final_score=round(final, 4),
            estimated_win_prob=model_prob,
            token_price=token_price,
            gross_multiple=gross_multiple,
            expected_value=expected_value,
            edge_after_fees=float(edge_after_fees or 0.0),
            book_quality_score=book_quality,
            line_reversion_score=line_score,
            odds_lag_score=odds_lag,
            strategy_history_score=hist,
            strategy_name=strategy,
            category=category,
            hard_reason=hard_reason,
        )

    def _hard_reject_reason(self, token_price: float, ob: Dict[str, Any], opp_ob: Optional[Dict[str, Any]], expected_cost: float) -> str:
        if not ob or ob.get("best_ask") is None:
            return "quality_orderbook_missing"
        spread = ob.get("spread")
        if spread is not None and float(spread) > float(config.QUALITY_MAX_SPREAD):
            return "quality_spread_too_wide"
        if opp_ob and opp_ob.get("best_ask") is not None:
            ask_sum = float(token_price) + float(opp_ob.get("best_ask") or 0.0)
            if not (float(config.QUALITY_MIN_BOOK_ASK_SUM) <= ask_sum <= float(config.QUALITY_MAX_BOOK_ASK_SUM)):
                return "quality_binary_ask_sum_bad"
        # $1 FOK needs enough ask depth at or near cap. If the CLOB summary cannot provide it,
        # keep neutral rather than rejecting; Trader will still run its deeper depth check.
        depth = float(ob.get("ask_depth_to_cap") or 0.0)
        if depth > 0:
            required_shares = max(0.0, float(expected_cost or 1.0) / max(0.01, token_price))
            if depth < required_shares * 0.80:
                return "quality_depth_too_low"
        weighted = ob.get("weighted_avg_ask_to_cap")
        if weighted is not None:
            impact = max(0.0, float(weighted) - float(token_price))
            if impact > float(config.QUALITY_MAX_PRICE_IMPACT):
                return "quality_price_impact_high"
        return ""

    def _book_quality(self, token_price: float, ob: Dict[str, Any], opp_ob: Optional[Dict[str, Any]], expected_cost: float) -> float:
        if not ob or ob.get("best_ask") is None:
            return 0.0
        spread = float(ob.get("spread") or 0.0)
        spread_score = clamp(1.0 - spread / max(0.001, float(config.QUALITY_MAX_SPREAD)))
        depth = float(ob.get("ask_depth_to_cap") or 0.0)
        req = max(0.0, float(expected_cost or 1.0) / max(0.01, token_price))
        depth_score = 0.55 if depth <= 0 else clamp(depth / max(0.01, req * 2.0))
        ask_sum_score = 0.65
        if opp_ob and opp_ob.get("best_ask") is not None:
            ask_sum = token_price + float(opp_ob.get("best_ask") or 0.0)
            center = 1.0
            width = max(0.01, float(config.QUALITY_MAX_BOOK_ASK_SUM) - center)
            ask_sum_score = clamp(1.0 - abs(ask_sum - center) / width)
        return clamp(0.45 * spread_score + 0.35 * depth_score + 0.20 * ask_sum_score)

    def _line_reversion_score(self, signal) -> float:
        ind = getattr(signal, "indicators", {}) or {}
        sigma = max(1e-9, float(getattr(signal, "sigma_remaining", 0.0) or ind.get("sigma_remaining", 0.0) or 1.0))
        velocity = float(ind.get("velocity_to_line", 0.0) or 0.0)
        distance = abs(float(getattr(signal, "distance_to_reference", 0.0) or 0.0))
        if distance <= 0:
            return 0.35
        vel_score = clamp(velocity / max(1e-9, sigma * 0.40))
        dist_score = clamp(1.0 - min(distance / max(1e-9, sigma * 4.0), 1.0))
        wick_strength = float(ind.get("wick_strength", 0.0) or 0.0)
        wick_score = clamp(wick_strength / 8.0)
        return clamp(0.55 * vel_score + 0.25 * dist_score + 0.20 * wick_score)

    def _odds_lag_score(self, signal) -> float:
        ind = getattr(signal, "indicators", {}) or {}
        if str(ind.get("strategy_name", "")).lower() == "odds_lag":
            return 0.85
        velocity = float(ind.get("velocity_to_line", 0.0) or 0.0)
        sigma = max(1e-9, float(getattr(signal, "sigma_remaining", 0.0) or 1.0))
        return clamp(velocity / max(1e-9, sigma * 0.55))

    def _strategy_history_score(self, asset: str, strategy: str) -> float:
        if not self.db or not getattr(config, "AUTO_STRATEGY_WEIGHTING", True):
            return 0.5
        try:
            rows = self.db.get_asset_strategy_stats()
            for r in rows:
                if str(r.get("asset", "")).upper() == str(asset).upper() and str(r.get("strategy_name", "")).lower() == str(strategy).lower():
                    trades = int(r.get("trades") or 0)
                    if trades < int(config.STRATEGY_MIN_SAMPLES_FOR_GATE):
                        return 0.5
                    win_rate = float(r.get("win_rate") or 0.0)
                    pnl = float(r.get("total_pnl") or 0.0)
                    base = clamp((win_rate - 0.20) / 0.55)
                    pnl_adj = 0.10 if pnl > 0 else (-0.10 if pnl < 0 else 0.0)
                    return clamp(base + pnl_adj)
        except Exception:
            pass
        return 0.5
