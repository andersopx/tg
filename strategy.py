"""
Multi-strategy probability engine for Polymarket BTC/ETH/SOL/XRP 5m and 15m markets.

Class name PinBarStrategy is kept for backward compatibility, but it no longer waits
only for Pin Bar shapes. It can emit several independent signal types:
- prob_edge: original higher-quality probability edge signal
- late_line_snipe: late-window high-probability line control signal
- momentum_follow: trend continuation signal
- odds_ev: pure expected-value candidate for cheap outcome tokens
- odds_reversal: main high-multiple mean-reversion signal for cheap 3c-25c tokens
- wick_rejection_odds: long wick rejection + cheap token + line-closing velocity
- lottery_reversal: small-budget very-low-price reversal signal
- barrier_reclaim: target-line fake-break/reclaim setup; ambush cheap UP/DOWN, then confirm near end

Trader still performs the final checks against Polymarket Price-to-Beat, CLOB orderbook,
fees, spread, depth, daily caps and DRY_RUN. Strategy only proposes candidates.
"""
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Optional, List, Tuple, Dict, Any

from binance_feed import BinanceFeed, Kline
from config import config

log = logging.getLogger(__name__)


@dataclass
class ReversalSignal:
    """A detected opportunity. Kept name for backward compatibility."""
    direction: str                  # "UP" or "DOWN" - side to buy
    confidence: float               # 0.0 to 1.0; same as target_probability after adjustments
    pattern: str
    btc_price: float                # legacy field; for ETH/SOL this is the asset price
    window_ts: int
    seconds_left: int
    target_probability: float = 0.0
    up_probability: float = 0.0
    down_probability: float = 0.0
    reference_price: float = 0.0
    distance_to_reference: float = 0.0
    sigma_remaining: float = 0.0
    indicators: dict = field(default_factory=dict)
    asset: str = "BTC"
    timeframe: str = "5m"
    market_interval_sec: int = 300
    adjusted_score: float = 0.0

    def signal_type(self) -> str:
        bucket = int(self.confidence * 10)
        return f"{self.pattern}_{bucket}"


class PinBarStrategy:
    """Multi-strategy 5m engine. Name kept so main.py/trader.py remain compatible."""

    def __init__(self, feed: BinanceFeed, db=None, asset: str = "BTC", timeframe: str = "5m"):
        self.feed = feed
        self.db = db
        self.asset = str(asset or "BTC").upper()
        self.timeframe = config.timeframe_slug(timeframe)
        self.market_interval_sec = config.timeframe_seconds(self.timeframe)
        self._last_log_ts = 0
        self.last_no_signal_reason = "not_evaluated"
        self.last_no_signal_details = {}

    # ---- public API ----

    def evaluate(self) -> Optional[ReversalSignal]:
        """Return the best signal for legacy callers."""
        signals = self.generate_signals()
        return signals[0] if signals else None

    def generate_signals(self) -> List[ReversalSignal]:
        self.last_no_signal_reason = "not_evaluated"
        self.last_no_signal_details = {}
        ctx = self._build_context()
        if not ctx:
            return []

        enabled = {s.strip().lower() for s in str(config.STRATEGIES_ENABLED).split(",") if s.strip()}
        signals: List[ReversalSignal] = []

        if "barrier_reclaim" in enabled or "reclaim" in enabled:
            sig = self._signal_barrier_reclaim(ctx)
            if sig:
                signals.append(sig)

        if "prob_edge" in enabled or "main" in enabled or "probability_edge" in enabled:
            sig = self._signal_prob_edge(ctx)
            if sig:
                signals.append(sig)
        if "odds_ev" in enabled or "ev" in enabled:
            sig = self._signal_odds_ev(ctx)
            if sig:
                signals.append(sig)
        if "odds_lag" in enabled or "lag" in enabled:
            sig = self._signal_odds_lag(ctx)
            if sig:
                signals.append(sig)
        if "odds_reversal" in enabled or "odds" in enabled:
            sig = self._signal_odds_reversal(ctx)
            if sig:
                signals.append(sig)
        if "wick_rejection_odds" in enabled or "wick" in enabled:
            sig = self._signal_wick_rejection_odds(ctx)
            if sig:
                signals.append(sig)
        if "late_line_snipe" in enabled or "snipe" in enabled:
            sig = self._signal_late_line_snipe(ctx)
            if sig:
                signals.append(sig)
        if "momentum_follow" in enabled or "momentum" in enabled:
            sig = self._signal_momentum_follow(ctx)
            if sig:
                signals.append(sig)
        if "lottery_reversal" in enabled or "lottery" in enabled:
            sig = self._signal_lottery_reversal(ctx)
            if sig:
                signals.append(sig)

        # Remove duplicate same-direction weak duplicates; keep the best score per pattern/direction.
        signals.sort(key=lambda s: -float(s.adjusted_score or s.confidence or 0))
        if not signals:
            self.last_no_signal_reason = "strategy_filters_no_match"
            self.last_no_signal_details = {
                "asset": self.asset,
                "timeframe": self.timeframe,
                "seconds_left": ctx.get("seconds_left"),
                "price": round(float(ctx.get("price") or 0), 6),
                "reference_price": round(float(ctx.get("reference_price") or 0), 6),
                "distance": round(float(ctx.get("distance") or 0), 6),
                "velocity_to_line": round(float(ctx.get("velocity_to_line") or 0), 6),
                "recent_change": round(float(ctx.get("recent_change") or 0), 8),
                "pin_kind": ctx.get("pin_kind"),
                "wick": ctx.get("wick") or {},
            }
        if signals:
            now = int(time.time())
            if now - self._last_log_ts > 5:
                log.info(
                    "📡 %s signals=%s left=%ss price=%.4f ref=%.4f dist=%.4f",
                    self.asset,
                    ", ".join([f"{s.pattern}:{s.direction}:{s.target_probability:.2f}" for s in signals]),
                    ctx["seconds_left"],
                    ctx["price"],
                    ctx["reference_price"],
                    ctx["distance"],
                )
                self._last_log_ts = now
        return signals

    def reprice_signal(self, signal: ReversalSignal, market_reference_price: float) -> Optional[ReversalSignal]:
        """
        Re-score with Gamma Price-to-Beat. Per-strategy thresholds are stored in
        signal.indicators, so low-price lottery signals are not incorrectly forced
        through the main strategy's 62% threshold.
        """
        price = self.feed.get_current_price()
        if price <= 0 or market_reference_price <= 0:
            return None

        proxy_mismatch = abs(signal.reference_price - market_reference_price)
        if proxy_mismatch > config.MAX_REFERENCE_MISMATCH_USD:
            log.warning(
                "Skip %s: proxy reference mismatch too large proxy=$%.4f market=$%.4f diff=$%.4f",
                self.asset, signal.reference_price, market_reference_price, proxy_mismatch,
            )
            return None

        distance = price - market_reference_price
        strategy_name = str(signal.indicators.get("strategy_name") or signal.pattern).lower()
        min_distance_mult = float(signal.indicators.get("min_distance_mult", config.MIN_DISTANCE_SIGMA_MULT) or config.MIN_DISTANCE_SIGMA_MULT)
        absolute_min_distance = float(signal.indicators.get("absolute_min_distance", config.asset_min_price_distance(self.asset)) or 0.0)
        min_distance = max(absolute_min_distance, signal.sigma_remaining * min_distance_mult)

        # Lottery reversal intentionally targets the losing side after strong movement back toward the line.
        if strategy_name not in {"lottery_reversal", "odds_reversal"} and abs(distance) < min_distance:
            return None

        drift = float(signal.indicators.get("expected_drift_usd", 0.0) or 0.0)
        expected_end = price + drift
        z = (expected_end - market_reference_price) / max(signal.sigma_remaining, 1.0)
        p_up = self._normal_cdf(z)
        p_down = 1.0 - p_up

        if strategy_name in {"lottery_reversal", "odds_reversal", "wick_rejection_odds", "odds_ev"}:
            # Keep the contrarian direction chosen by the initial signal, then re-score it.
            direction = signal.direction
            target_prob = p_up if direction == "UP" else p_down
            # If official Price-to-Beat changes the distance sign, the cheap side is no longer the same setup.
            if strategy_name in {"odds_reversal", "wick_rejection_odds", "odds_ev"} and ((direction == "DOWN" and distance <= 0) or (direction == "UP" and distance >= 0)):
                return None
            if strategy_name in {"odds_reversal", "wick_rejection_odds", "odds_ev"}:
                abs_distance = abs(distance)
                min_d = float(signal.indicators.get("absolute_min_distance", 0.0) or 0.0)
                max_d = max(signal.sigma_remaining * float(signal.indicators.get("max_distance_sigma_mult", config.ODDS_REVERSAL_MAX_DISTANCE_SIGMA_MULT) or config.ODDS_REVERSAL_MAX_DISTANCE_SIGMA_MULT), min_d)
                if abs_distance < min_d or abs_distance > max_d:
                    return None
        else:
            direction = "UP" if p_up >= p_down else "DOWN"
            target_prob = p_up if direction == "UP" else p_down

        min_model = float(signal.indicators.get("min_model_prob", config.MIN_MODEL_PROB) or config.MIN_MODEL_PROB)
        min_conf = float(signal.indicators.get("min_confidence_to_trade", min_model) or min_model)
        if strategy_name == "barrier_reclaim":
            # Barrier reclaim is a path strategy. During ambush, the current one-point
            # normal model may still look weak because price has not crossed back yet.
            # Preserve the path-derived probability, while updating official reference data.
            direction = signal.direction
            path_prob = float(signal.indicators.get("path_probability", signal.target_probability or 0.0) or 0.0)
            instant_prob = p_up if direction == "UP" else p_down
            if str(signal.indicators.get("reclaim_stage") or "") == "confirm":
                target_prob = max(path_prob, instant_prob)
            else:
                target_prob = max(path_prob, min(0.62, instant_prob + 0.10))
            if target_prob < min_model:
                return None
            signal.confidence = target_prob
            signal.target_probability = target_prob
            signal.up_probability = p_up
            signal.down_probability = p_down
            signal.reference_price = market_reference_price
            signal.distance_to_reference = distance
            signal.adjusted_score = self._score_signal("barrier_reclaim", target_prob, signal.indicators)
            signal.indicators.update({
                "reference_source": "gamma_price_to_beat",
                "proxy_reference_price": round(float(signal.indicators.get("reference_price", 0) or 0), 4),
                "proxy_market_mismatch_usd": round(proxy_mismatch, 4),
                "reference_price": round(market_reference_price, 4),
                "distance_to_reference": round(distance, 4),
                "up_probability": round(p_up, 4),
                "down_probability": round(p_down, 4),
                "raw_target_probability": round(target_prob, 4),
                "calibrated_probability": round(target_prob, 4),
                "barrier_reprice_note": "path_probability_preserved",
            })
            return signal
        if target_prob < min_model:
            return None

        pattern = signal.pattern
        raw_target_prob = target_prob
        if self.db:
            target_prob *= self.db.get_signal_weight(pattern)
            # V13.4: also apply per-asset/per-strategy weight after enough samples.
            if hasattr(self.db, "get_asset_strategy_weight"):
                strategy_name = str(signal.indicators.get("strategy_name") or signal.pattern).lower()
                target_prob *= self.db.get_asset_strategy_weight(self.asset, strategy_name)
            target_prob = self.db.calibrate_probability(target_prob, direction)
            target_prob = max(0.0, min(0.98, target_prob))
        if target_prob < min_conf:
            return None

        signal.direction = direction
        signal.confidence = target_prob
        signal.target_probability = target_prob
        signal.up_probability = p_up
        signal.down_probability = p_down
        signal.reference_price = market_reference_price
        signal.distance_to_reference = distance
        signal.asset = getattr(signal, "asset", self.asset)
        signal.adjusted_score = self._score_signal(pattern, target_prob, signal.indicators)
        signal.indicators.update({
            "reference_source": "gamma_price_to_beat",
            "proxy_reference_price": round(float(signal.indicators.get("reference_price", 0) or 0), 4),
            "proxy_market_mismatch_usd": round(proxy_mismatch, 4),
            "reference_price": round(market_reference_price, 4),
            "distance_to_reference": round(distance, 4),
            "up_probability": round(p_up, 4),
            "down_probability": round(p_down, 4),
            "min_distance": round(min_distance, 4),
            "raw_target_probability": round(raw_target_prob, 4),
            "calibrated_probability": round(target_prob, 4),
        })
        return signal

    def _tf_bounds(self, base_min: int, base_max: int) -> tuple:
        return config.strategy_time_bounds(base_min, base_max, self.timeframe)

    # ---- signal builders ----

    def _build_context(self) -> Optional[Dict[str, Any]]:
        now = int(time.time())
        window_ts = (now // self.market_interval_sec) * self.market_interval_sec
        window_end = window_ts + self.market_interval_sec
        seconds_left = window_end - now

        bounds = [
            self._tf_bounds(config.ENTRY_MIN_SECONDS_LEFT, config.ENTRY_MAX_SECONDS_LEFT),
            self._tf_bounds(config.LATE_SNIPE_MIN_SECONDS_LEFT, config.LATE_SNIPE_MAX_SECONDS_LEFT),
            self._tf_bounds(config.MOMENTUM_MIN_SECONDS_LEFT, config.MOMENTUM_MAX_SECONDS_LEFT),
            self._tf_bounds(config.LOTTERY_MIN_SECONDS_LEFT, config.LOTTERY_MAX_SECONDS_LEFT),
            self._tf_bounds(config.ODDS_REVERSAL_MIN_SECONDS_LEFT, config.ODDS_REVERSAL_MAX_SECONDS_LEFT),
            self._tf_bounds(config.ODDS_EV_MIN_SECONDS_LEFT, config.ODDS_EV_MAX_SECONDS_LEFT),
            self._tf_bounds(config.ODDS_LAG_MIN_SECONDS_LEFT, config.ODDS_LAG_MAX_SECONDS_LEFT),
            self._tf_bounds(config.WICK_MIN_SECONDS_LEFT, config.WICK_MAX_SECONDS_LEFT),
            self._tf_bounds(config.BARRIER_RECLAIM_CONFIRM_MIN_SECONDS_LEFT, config.BARRIER_RECLAIM_AMBUSH_MAX_SECONDS_LEFT),
        ]
        max_needed = max(b[1] for b in bounds)
        min_needed = min(b[0] for b in bounds)
        if not (min_needed <= seconds_left <= max_needed):
            self.last_no_signal_reason = "outside_strategy_time_window"
            self.last_no_signal_details = {"asset": self.asset, "timeframe": self.timeframe, "seconds_left": seconds_left, "min_needed": min_needed, "max_needed": max_needed, "window_ts": window_ts}
            return None

        klines = self.feed.get_window_klines(window_ts, self.market_interval_sec)
        if len(klines) < 2:
            self.last_no_signal_reason = "insufficient_window_klines"
            self.last_no_signal_details = {"asset": self.asset, "timeframe": self.timeframe, "window_ts": window_ts, "klines": len(klines), "seconds_left": seconds_left}
            return None
        price = self.feed.get_current_price()
        if price <= 0:
            self.last_no_signal_reason = "price_unavailable"
            self.last_no_signal_details = {"asset": self.asset, "timeframe": self.timeframe, "window_ts": window_ts, "seconds_left": seconds_left}
            return None

        reference_price = klines[0].open
        distance = price - reference_price
        returns = self._recent_returns(config.VOL_LOOKBACK_MIN)
        if len(returns) < 5:
            self.last_no_signal_reason = "insufficient_vol_history"
            self.last_no_signal_details = {"asset": self.asset, "returns": len(returns), "need": 5, "seconds_left": seconds_left}
            return None
        per_min_vol = statistics.pstdev(returns) or 0.0
        remaining_min = max(seconds_left / 60.0, 1.0 / 60.0)
        sigma_remaining = max(self._asset_sigma_floor(price), price * per_min_vol * math.sqrt(remaining_min))
        drift = self._momentum_drift(price, remaining_min)
        p_up, p_down = self._probabilities(price, reference_price, sigma_remaining, drift)
        pin_kind, pin_bonus = self._pinbar_bonus(klines, distance)
        wick = self._wick_metrics(klines[-1])
        velocity = self._line_closing_velocity(reference_price, price)
        recent_change = self._recent_price_change(2)
        path = self._barrier_path_metrics(klines, reference_price, price)

        return {
            "now": now,
            "window_ts": window_ts,
            "timeframe": self.timeframe,
            "market_interval_sec": self.market_interval_sec,
            "seconds_left": seconds_left,
            "klines": klines,
            "price": price,
            "reference_price": reference_price,
            "distance": distance,
            "returns": returns,
            "per_min_vol": per_min_vol,
            "remaining_min": remaining_min,
            "sigma_remaining": sigma_remaining,
            "drift": drift,
            "p_up": p_up,
            "p_down": p_down,
            "pin_kind": pin_kind,
            "pin_bonus": pin_bonus,
            "wick": wick,
            "velocity_to_line": velocity,
            "recent_change": recent_change,
            "barrier_path": path,
        }

    def _barrier_path_metrics(self, klines: List[Kline], reference_price: float, current_price: float) -> Dict[str, Any]:
        """Summarize current-window path around the target/Price-to-Beat line."""
        highs = [float(k.high or 0.0) for k in klines if float(k.high or 0.0) > 0]
        lows = [float(k.low or 0.0) for k in klines if float(k.low or 0.0) > 0]
        closes = [float(k.close or 0.0) for k in klines if float(k.close or 0.0) > 0]
        if not highs or not lows:
            return {}
        max_above = max(highs) - reference_price
        max_below = reference_price - min(lows)
        start_close = closes[0] if closes else current_price
        current_distance = current_price - reference_price
        up_retrace = 0.0
        if max_below > 0:
            up_retrace = (current_price - (reference_price - max_below)) / max(max_below, 1e-9)
        down_retrace = 0.0
        if max_above > 0:
            down_retrace = ((reference_price + max_above) - current_price) / max(max_above, 1e-9)
        return {
            "max_above": round(max_above, 6),
            "max_below": round(max_below, 6),
            "start_close": round(start_close, 6),
            "current_distance": round(current_distance, 6),
            "up_retrace_ratio": round(up_retrace, 4),
            "down_retrace_ratio": round(down_retrace, 4),
            "was_below": max_below > 0,
            "was_above": max_above > 0,
        }

    def _signal_barrier_reclaim(self, ctx: Dict[str, Any]) -> Optional[ReversalSignal]:
        """Target-line reclaim / false-break strategy.

        UP: price first went below the target line, making UP cheap, then starts
        reclaiming or has reclaimed the line. DOWN is the symmetric setup.
        """
        if not config.BARRIER_RECLAIM_ENABLED:
            return None
        seconds_left = int(ctx["seconds_left"])
        path = ctx.get("barrier_path") or {}
        if not path:
            return None

        distance = float(ctx["distance"] or 0.0)
        abs_distance = abs(distance)
        base_distance = max(
            float(config.asset_min_price_distance(self.asset) or 0.0) * float(config.BARRIER_RECLAIM_MIN_DISTANCE_FACTOR),
            float(ctx["sigma_remaining"] or 0.0) * 0.10,
        )
        max_distance = max(base_distance, float(config.asset_min_price_distance(self.asset) or 0.0) * float(config.BARRIER_RECLAIM_MAX_DISTANCE_FACTOR))
        min_retrace = float(config.BARRIER_RECLAIM_MIN_RETRACE_RATIO or 0.0)

        stage = ""
        direction = ""
        path_label = ""
        path_probability = 0.0
        min_token = (config.BARRIER_RECLAIM_DEEP_AMBUSH_MIN_TOKEN_PRICE if config.BARRIER_RECLAIM_DEEP_AMBUSH_ENABLED else config.BARRIER_RECLAIM_AMBUSH_MIN_TOKEN_PRICE)
        max_token = config.BARRIER_RECLAIM_AMBUSH_MAX_TOKEN_PRICE

        # Confirmed reclaim: already crossed back to the winning side near tail.
        if (config.BARRIER_RECLAIM_CONFIRM_ENABLED
            and config.BARRIER_RECLAIM_CONFIRM_MIN_SECONDS_LEFT <= seconds_left <= config.BARRIER_RECLAIM_CONFIRM_MAX_SECONDS_LEFT):
            if path.get("was_below") and distance >= base_distance:
                stage, direction = "confirm", "UP"
                path_label = "先跌破目标线，后重新站上"
                path_probability = config.BARRIER_RECLAIM_CONFIRM_MODEL_PROB
            elif path.get("was_above") and distance <= -base_distance:
                stage, direction = "confirm", "DOWN"
                path_label = "先涨破目标线，后重新跌回"
                path_probability = config.BARRIER_RECLAIM_CONFIRM_MODEL_PROB
            min_token = config.BARRIER_RECLAIM_CONFIRM_MIN_TOKEN_PRICE
            max_token = min(config.BARRIER_RECLAIM_CONFIRM_MAX_TOKEN_PRICE, config.BARRIER_RECLAIM_NO_BUY_ABOVE)

        # Ambush: cheap side while price is still on adverse side but reclaiming.
        if not direction and (config.BARRIER_RECLAIM_AMBUSH_ENABLED
            and config.BARRIER_RECLAIM_AMBUSH_MIN_SECONDS_LEFT <= seconds_left <= config.BARRIER_RECLAIM_AMBUSH_MAX_SECONDS_LEFT):
            up_retrace = float(path.get("up_retrace_ratio") or 0.0)
            down_retrace = float(path.get("down_retrace_ratio") or 0.0)
            if path.get("was_below") and distance < 0 and abs_distance <= max_distance and up_retrace >= min_retrace:
                stage, direction = "ambush", "UP"
                path_label = "先跌破目标线，正在向上回收"
                path_probability = config.BARRIER_RECLAIM_AMBUSH_MODEL_PROB
            elif path.get("was_above") and distance > 0 and abs_distance <= max_distance and down_retrace >= min_retrace:
                stage, direction = "ambush", "DOWN"
                path_label = "先涨破目标线，正在向下回收"
                path_probability = config.BARRIER_RECLAIM_AMBUSH_MODEL_PROB
            min_token = (config.BARRIER_RECLAIM_DEEP_AMBUSH_MIN_TOKEN_PRICE if config.BARRIER_RECLAIM_DEEP_AMBUSH_ENABLED else config.BARRIER_RECLAIM_AMBUSH_MIN_TOKEN_PRICE)
            max_token = config.BARRIER_RECLAIM_AMBUSH_MAX_TOKEN_PRICE

        if not direction:
            return None

        p_up, p_down = ctx["p_up"], ctx["p_down"]
        target = path_probability
        return self._finalize_signal(ctx, "barrier_reclaim", direction, target, p_up, p_down, {
            "strategy_name": "barrier_reclaim",
            "reclaim_stage": stage,
            "path_label": path_label,
            "path_probability": round(path_probability, 4),
            "min_model_prob": 0.05,
            "min_confidence_to_trade": 0.05,
            "min_edge_after_fees": config.BARRIER_RECLAIM_MIN_EDGE_AFTER_FEES,
            "min_token_price": min_token,
            "max_token_price": max_token,
            "max_spread": max(config.TARGET_SPREAD_MAX, config.BARRIER_RECLAIM_MAX_SPREAD),
            "custom_quality_gate": config.BARRIER_RECLAIM_USE_CUSTOM_QUALITY_GATE,
            "max_market_prob_gap": 0.80,
            "market_consensus_weight": 0.0,
            "bet_size_override": config.BARRIER_RECLAIM_BET_SIZE,
            "min_liquidity_multiplier": 1.0,
            "min_distance_mult": 0.0,
            "absolute_min_distance": 0.0,
            "barrier_reclaim": True,
            "barrier_path": path,
        })

    def _signal_prob_edge(self, ctx: Dict[str, Any]) -> Optional[ReversalSignal]:
        seconds_left = ctx["seconds_left"]
        min_s, max_s = self._tf_bounds(config.ENTRY_MIN_SECONDS_LEFT, config.ENTRY_MAX_SECONDS_LEFT)
        if not (min_s <= seconds_left <= max_s):
            return None

        p_up, p_down = ctx["p_up"], ctx["p_down"]
        pin_kind = ctx["pin_kind"]
        pin_bonus = ctx["pin_bonus"]
        distance = ctx["distance"]
        min_distance = max(config.asset_min_price_distance(self.asset), ctx["sigma_remaining"] * config.MIN_DISTANCE_SIGMA_MULT)
        if abs(distance) < min_distance:
            return None

        if pin_kind == "bullish_confirm":
            p_up = min(0.98, p_up + pin_bonus)
            p_down = 1.0 - p_up
        elif pin_kind == "bearish_confirm":
            p_down = min(0.98, p_down + pin_bonus)
            p_up = 1.0 - p_down
        elif pin_kind == "against_signal":
            return None

        direction = "UP" if p_up >= p_down else "DOWN"
        target = p_up if direction == "UP" else p_down
        return self._finalize_signal(ctx, "prob_edge", direction, target, p_up, p_down, {
            "strategy_name": "prob_edge",
            "min_model_prob": config.MIN_MODEL_PROB,
            "min_confidence_to_trade": config.MIN_CONFIDENCE_TO_TRADE,
            "min_edge_after_fees": config.MIN_EDGE_AFTER_FEES,
            "min_token_price": config.MIN_TOKEN_PRICE,
            "max_token_price": config.MAX_TOKEN_PRICE,
            "max_spread": config.MAX_SPREAD,
            "min_distance_mult": config.MIN_DISTANCE_SIGMA_MULT,
            "absolute_min_distance": config.asset_min_price_distance(self.asset),
            "pinbar": pin_kind,
        })

    def _signal_late_line_snipe(self, ctx: Dict[str, Any]) -> Optional[ReversalSignal]:
        if not config.LATE_LINE_SNIPE_ENABLED:
            return None
        seconds_left = ctx["seconds_left"]
        min_s, max_s = self._tf_bounds(config.LATE_SNIPE_MIN_SECONDS_LEFT, config.LATE_SNIPE_MAX_SECONDS_LEFT)
        if not (min_s <= seconds_left <= max_s):
            return None
        distance = ctx["distance"]
        min_distance = max(config.asset_min_price_distance(self.asset) * config.LATE_SNIPE_MIN_DISTANCE_FACTOR,
                           ctx["sigma_remaining"] * config.LATE_SNIPE_MIN_SIGMA_MULT)
        if abs(distance) < min_distance:
            return None
        direction = "UP" if distance >= 0 else "DOWN"
        target = ctx["p_up"] if direction == "UP" else ctx["p_down"]
        if target < config.LATE_SNIPE_MIN_MODEL_PROB:
            return None
        return self._finalize_signal(ctx, "late_line_snipe", direction, target, ctx["p_up"], ctx["p_down"], {
            "strategy_name": "late_line_snipe",
            "min_model_prob": config.LATE_SNIPE_MIN_MODEL_PROB,
            "min_confidence_to_trade": config.LATE_SNIPE_MIN_MODEL_PROB,
            "min_edge_after_fees": config.LATE_SNIPE_MIN_EDGE_AFTER_FEES,
            "min_token_price": config.LATE_SNIPE_MIN_TOKEN_PRICE,
            "max_token_price": config.LATE_SNIPE_MAX_TOKEN_PRICE,
            "max_spread": config.LATE_SNIPE_MAX_SPREAD,
            "max_market_prob_gap": config.LATE_SNIPE_MAX_MARKET_PROB_GAP,
            "market_consensus_weight": config.LATE_SNIPE_MARKET_CONSENSUS_WEIGHT,
            "min_distance_mult": config.LATE_SNIPE_MIN_SIGMA_MULT,
            "absolute_min_distance": config.asset_min_price_distance(self.asset) * config.LATE_SNIPE_MIN_DISTANCE_FACTOR,
            "pinbar": ctx["pin_kind"],
        })

    def _signal_momentum_follow(self, ctx: Dict[str, Any]) -> Optional[ReversalSignal]:
        if not config.MOMENTUM_FOLLOW_ENABLED:
            return None
        seconds_left = ctx["seconds_left"]
        min_s, max_s = self._tf_bounds(config.MOMENTUM_MIN_SECONDS_LEFT, config.MOMENTUM_MAX_SECONDS_LEFT)
        if not (min_s <= seconds_left <= max_s):
            return None
        distance = ctx["distance"]
        drift = ctx["drift"]
        if abs(drift) < ctx["sigma_remaining"] * config.MOMENTUM_MIN_DRIFT_SIGMA_MULT:
            return None
        # Trend should be moving farther into one side of Price-to-Beat, not chopping back and forth.
        if distance == 0 or (distance > 0 and drift <= 0) or (distance < 0 and drift >= 0):
            return None
        min_distance = max(config.asset_min_price_distance(self.asset) * config.MOMENTUM_MIN_DISTANCE_FACTOR,
                           ctx["sigma_remaining"] * config.MOMENTUM_MIN_SIGMA_MULT)
        if abs(distance) < min_distance:
            return None
        direction = "UP" if distance > 0 else "DOWN"
        target = ctx["p_up"] if direction == "UP" else ctx["p_down"]
        if target < config.MOMENTUM_MIN_MODEL_PROB:
            return None
        return self._finalize_signal(ctx, "momentum_follow", direction, target, ctx["p_up"], ctx["p_down"], {
            "strategy_name": "momentum_follow",
            "min_model_prob": config.MOMENTUM_MIN_MODEL_PROB,
            "min_confidence_to_trade": config.MOMENTUM_MIN_MODEL_PROB,
            "min_edge_after_fees": config.MOMENTUM_MIN_EDGE_AFTER_FEES,
            "min_token_price": config.MOMENTUM_MIN_TOKEN_PRICE,
            "max_token_price": config.MOMENTUM_MAX_TOKEN_PRICE,
            "max_spread": config.MOMENTUM_MAX_SPREAD,
            "max_market_prob_gap": config.MOMENTUM_MAX_MARKET_PROB_GAP,
            "market_consensus_weight": config.MOMENTUM_MARKET_CONSENSUS_WEIGHT,
            "min_distance_mult": config.MOMENTUM_MIN_SIGMA_MULT,
            "absolute_min_distance": config.asset_min_price_distance(self.asset) * config.MOMENTUM_MIN_DISTANCE_FACTOR,
            "pinbar": ctx["pin_kind"],
        })

    def _signal_odds_reversal(self, ctx: Dict[str, Any]) -> Optional[ReversalSignal]:
        """
        High-multiple 5m strategy:
        - buy DOWN when price first moved above Price-to-Beat and the cheap Down side starts recovering
        - buy UP when price first moved below Price-to-Beat and the cheap Up side starts recovering
        This is not Pin Bar dependent; it targets 3c-25c outcome tokens with measurable line-closing velocity.
        """
        if not config.ODDS_REVERSAL_ENABLED:
            return None
        seconds_left = ctx["seconds_left"]
        min_s, max_s = self._tf_bounds(config.ODDS_REVERSAL_MIN_SECONDS_LEFT, config.ODDS_REVERSAL_MAX_SECONDS_LEFT)
        if not (min_s <= seconds_left <= max_s):
            return None

        distance = ctx["distance"]
        abs_distance = abs(distance)
        min_distance = max(
            config.asset_min_price_distance(self.asset) * config.ODDS_REVERSAL_MIN_DISTANCE_FACTOR,
            ctx["sigma_remaining"] * 0.12,
        )
        max_distance = max(
            min_distance,
            ctx["sigma_remaining"] * config.ODDS_REVERSAL_MAX_DISTANCE_SIGMA_MULT,
        )
        if abs_distance < min_distance or abs_distance > max_distance:
            return None

        velocity_to_line = ctx["velocity_to_line"]
        min_velocity = max(
            config.asset_min_price_distance(self.asset) * config.ODDS_REVERSAL_MIN_LINE_CLOSING_FACTOR,
            ctx["sigma_remaining"] * config.ODDS_REVERSAL_MIN_LINE_CLOSING_SIGMA,
        )
        if velocity_to_line < min_velocity:
            return None

        # Recent move must be counter to the initial distance: above line should be falling; below line should be rising.
        recent_change = ctx.get("recent_change", 0.0)
        if distance > 0 and recent_change > -config.ODDS_REVERSAL_MIN_RECENT_COUNTER_MOVE:
            return None
        if distance < 0 and recent_change < config.ODDS_REVERSAL_MIN_RECENT_COUNTER_MOVE:
            return None

        direction = "DOWN" if distance > 0 else "UP"
        target = ctx["p_down"] if direction == "DOWN" else ctx["p_up"]
        if target < config.ODDS_REVERSAL_MIN_MODEL_PROB or target > config.ODDS_REVERSAL_MAX_MODEL_PROB:
            return None

        # Quality score favors setups where the line is being approached quickly while price is still cheap.
        shrink_ratio = velocity_to_line / max(abs_distance + velocity_to_line, 1e-9)
        return self._finalize_signal(ctx, "odds_reversal", direction, target, ctx["p_up"], ctx["p_down"], {
            "strategy_name": "odds_reversal",
            "min_model_prob": config.ODDS_REVERSAL_MIN_MODEL_PROB,
            "min_confidence_to_trade": config.ODDS_REVERSAL_MIN_MODEL_PROB,
            "min_edge_after_fees": config.ODDS_REVERSAL_MIN_EDGE_AFTER_FEES,
            "min_token_price": config.ODDS_REVERSAL_MIN_TOKEN_PRICE,
            "max_token_price": config.ODDS_REVERSAL_MAX_TOKEN_PRICE,
            "max_spread": config.ODDS_REVERSAL_MAX_SPREAD,
            "max_market_prob_gap": config.ODDS_REVERSAL_MAX_MARKET_PROB_GAP,
            "market_consensus_weight": config.ODDS_REVERSAL_MARKET_CONSENSUS_WEIGHT,
            "min_liquidity_multiplier": config.ODDS_REVERSAL_MIN_LIQUIDITY_MULTIPLIER,
            "bet_size_override": config.ODDS_REVERSAL_BET_SIZE,
            "min_distance_mult": 0.0,
            "absolute_min_distance": min_distance,
            "max_distance_sigma_mult": config.ODDS_REVERSAL_MAX_DISTANCE_SIGMA_MULT,
            "velocity_to_line": round(velocity_to_line, 4),
            "min_velocity_to_line": round(min_velocity, 4),
            "shrink_ratio": round(shrink_ratio, 4),
            "expected_multiple_min": round(1.0 / max(config.ODDS_REVERSAL_MAX_TOKEN_PRICE, 0.01), 2),
            "pinbar": ctx["pin_kind"],
        })

    def _signal_odds_ev(self, ctx: Dict[str, Any]) -> Optional[ReversalSignal]:
        """
        EV-first candidate. Strategy proposes cheap contrarian sides when the line is reachable.
        Trader performs the real EV test after it reads the live CLOB ask.
        """
        if not config.ODDS_EV_ENABLED:
            return None
        seconds_left = ctx["seconds_left"]
        min_s, max_s = self._tf_bounds(config.ODDS_EV_MIN_SECONDS_LEFT, config.ODDS_EV_MAX_SECONDS_LEFT)
        if not (min_s <= seconds_left <= max_s):
            return None
        distance = ctx["distance"]
        abs_distance = abs(distance)
        min_distance = max(config.asset_min_price_distance(self.asset) * 0.25, ctx["sigma_remaining"] * 0.08)
        max_distance = max(min_distance, ctx["sigma_remaining"] * 3.80)
        if abs_distance < min_distance or abs_distance > max_distance:
            return None
        velocity_to_line = ctx["velocity_to_line"]
        # EV strategy is allowed to be earlier than odds_reversal, but should not be moving away hard.
        if velocity_to_line < -ctx["sigma_remaining"] * 0.05:
            return None
        recent_change = ctx.get("recent_change", 0.0)
        if distance > 0 and recent_change > config.ODDS_REVERSAL_MIN_RECENT_COUNTER_MOVE * 0.5:
            return None
        if distance < 0 and recent_change < -config.ODDS_REVERSAL_MIN_RECENT_COUNTER_MOVE * 0.5:
            return None
        direction = "DOWN" if distance > 0 else "UP"
        target = ctx["p_down"] if direction == "DOWN" else ctx["p_up"]
        if target < config.ODDS_EV_MIN_MODEL_PROB:
            return None
        return self._finalize_signal(ctx, "odds_ev", direction, target, ctx["p_up"], ctx["p_down"], {
            "strategy_name": "odds_ev",
            "min_model_prob": config.ODDS_EV_MIN_MODEL_PROB,
            "min_confidence_to_trade": config.ODDS_EV_MIN_MODEL_PROB,
            "min_edge_after_fees": config.ODDS_EV_MIN_EXPECTED_VALUE,
            "min_token_price": config.ODDS_EV_MIN_TOKEN_PRICE,
            "max_token_price": config.ODDS_EV_MAX_TOKEN_PRICE,
            "max_spread": config.ODDS_EV_MAX_SPREAD,
            "max_market_prob_gap": config.ODDS_EV_MAX_MARKET_PROB_GAP,
            "market_consensus_weight": config.ODDS_EV_MARKET_CONSENSUS_WEIGHT,
            "min_liquidity_multiplier": config.ODDS_EV_MIN_LIQUIDITY_MULTIPLIER,
            "bet_size_override": config.ODDS_EV_BET_SIZE,
            "min_distance_mult": 0.0,
            "absolute_min_distance": min_distance,
            "max_distance_sigma_mult": 3.80,
            "velocity_to_line": round(velocity_to_line, 4),
            "ev_floor": round(config.ODDS_EV_MIN_EXPECTED_VALUE, 4),
            "pinbar": ctx["pin_kind"],
        })

    def _signal_odds_lag(self, ctx: Dict[str, Any]) -> Optional[ReversalSignal]:
        """Price is moving back toward Price-to-Beat but the cheap-side odds have not caught up.

        Uses local market_snapshots when available; otherwise falls back to price velocity only.
        Trader still performs final orderbook/EV/quality-gate checks.
        """
        if not config.ODDS_LAG_ENABLED:
            return None
        seconds_left = ctx["seconds_left"]
        min_s, max_s = self._tf_bounds(config.ODDS_LAG_MIN_SECONDS_LEFT, config.ODDS_LAG_MAX_SECONDS_LEFT)
        if not (min_s <= seconds_left <= max_s):
            return None
        distance = float(ctx["distance"])
        if abs(distance) < max(config.asset_min_price_distance(self.asset) * 0.25, ctx["sigma_remaining"] * 0.08):
            return None
        direction = "DOWN" if distance > 0 else "UP"
        target = ctx["p_down"] if direction == "DOWN" else ctx["p_up"]
        if target < config.ODDS_LAG_MIN_MODEL_PROB:
            return None
        velocity_to_line = float(ctx.get("velocity_to_line") or 0.0)
        min_move = max(config.asset_min_price_distance(self.asset) * config.ODDS_LAG_MIN_PRICE_MOVE_TO_LINE,
                       ctx["sigma_remaining"] * 0.05)
        if velocity_to_line < min_move:
            return None

        lag_hint = 0.0
        snap_count = 0
        if self.db and hasattr(self.db, "get_recent_market_snapshots"):
            try:
                snaps = self.db.get_recent_market_snapshots(self.asset, ctx["window_ts"], limit=6, timeframe=self.timeframe)
                snap_count = len(snaps)
                if len(snaps) >= config.ODDS_LAG_MIN_SNAPSHOT_COUNT:
                    newest, oldest = snaps[0], snaps[-1]
                    key = "down_best_ask" if direction == "DOWN" else "up_best_ask"
                    new_ask = float(newest.get(key) or 0.0)
                    old_ask = float(oldest.get(key) or 0.0)
                    if new_ask > 0 and old_ask > 0:
                        ask_rise_pct = (new_ask - old_ask) / old_ask
                        # Good lag: underlying moved toward the line while the target token did not already reprice hard.
                        if ask_rise_pct > config.ODDS_LAG_MAX_ASK_RISE_PCT:
                            return None
                        lag_hint = max(0.0, config.ODDS_LAG_MAX_ASK_RISE_PCT - ask_rise_pct)
            except Exception:
                pass
        return self._finalize_signal(ctx, "odds_lag", direction, target, ctx["p_up"], ctx["p_down"], {
            "strategy_name": "odds_lag",
            "min_model_prob": config.ODDS_LAG_MIN_MODEL_PROB,
            "min_confidence_to_trade": config.ODDS_LAG_MIN_MODEL_PROB,
            "min_edge_after_fees": 0.0,
            "min_token_price": config.ODDS_LAG_MIN_TOKEN_PRICE,
            "max_token_price": config.ODDS_LAG_MAX_TOKEN_PRICE,
            "max_spread": config.ODDS_LAG_MAX_SPREAD,
            "max_market_prob_gap": config.ODDS_LAG_MAX_MARKET_PROB_GAP,
            "market_consensus_weight": config.ODDS_LAG_MARKET_CONSENSUS_WEIGHT,
            "min_liquidity_multiplier": config.ODDS_LAG_MIN_LIQUIDITY_MULTIPLIER,
            "bet_size_override": config.ODDS_LAG_BET_SIZE,
            "min_distance_mult": 0.0,
            "absolute_min_distance": 0.0,
            "velocity_to_line": round(velocity_to_line, 6),
            "odds_lag_hint": round(lag_hint, 6),
            "snapshot_count": snap_count,
        })

    def _signal_wick_rejection_odds(self, ctx: Dict[str, Any]) -> Optional[ReversalSignal]:
        """Long wick rejection + cheap opposite side. Targets the exact long-tail setup seen on charts."""
        if not config.WICK_REJECTION_ENABLED:
            return None
        seconds_left = ctx["seconds_left"]
        min_s, max_s = self._tf_bounds(config.WICK_MIN_SECONDS_LEFT, config.WICK_MAX_SECONDS_LEFT)
        if not (min_s <= seconds_left <= max_s):
            return None
        distance = ctx["distance"]
        if abs(distance) < max(config.asset_min_price_distance(self.asset) * 0.30, ctx["sigma_remaining"] * 0.10):
            return None
        wick = ctx.get("wick", {}) or {}
        upper_body = float(wick.get("upper_wick_body_ratio") or 0.0)
        lower_body = float(wick.get("lower_wick_body_ratio") or 0.0)
        upper_range = float(wick.get("upper_wick_range_ratio") or 0.0)
        lower_range = float(wick.get("lower_wick_range_ratio") or 0.0)
        # Above line + long upper wick means rejected higher prices => buy cheap Down.
        bearish_reject = distance > 0 and upper_body >= config.WICK_MIN_WICK_BODY_RATIO and upper_range >= config.WICK_MIN_WICK_RANGE_RATIO
        # Below line + long lower wick means rejected lower prices => buy cheap Up.
        bullish_reject = distance < 0 and lower_body >= config.WICK_MIN_WICK_BODY_RATIO and lower_range >= config.WICK_MIN_WICK_RANGE_RATIO
        if not (bearish_reject or bullish_reject):
            return None
        velocity_to_line = ctx["velocity_to_line"]
        min_velocity = max(
            config.asset_min_price_distance(self.asset) * config.WICK_MIN_LINE_CLOSING_FACTOR,
            ctx["sigma_remaining"] * config.WICK_MIN_LINE_CLOSING_SIGMA,
        )
        if velocity_to_line < min_velocity:
            return None
        direction = "DOWN" if bearish_reject else "UP"
        target = ctx["p_down"] if direction == "DOWN" else ctx["p_up"]
        if target < config.WICK_MIN_MODEL_PROB:
            return None
        wick_strength = max(upper_body, lower_body) * max(upper_range, lower_range)
        return self._finalize_signal(ctx, "wick_rejection_odds", direction, target, ctx["p_up"], ctx["p_down"], {
            "strategy_name": "wick_rejection_odds",
            "min_model_prob": config.WICK_MIN_MODEL_PROB,
            "min_confidence_to_trade": config.WICK_MIN_MODEL_PROB,
            "min_edge_after_fees": config.WICK_MIN_EDGE_AFTER_FEES,
            "min_token_price": config.WICK_MIN_TOKEN_PRICE,
            "max_token_price": config.WICK_MAX_TOKEN_PRICE,
            "max_spread": config.WICK_MAX_SPREAD,
            "max_market_prob_gap": config.WICK_MAX_MARKET_PROB_GAP,
            "market_consensus_weight": config.WICK_MARKET_CONSENSUS_WEIGHT,
            "min_liquidity_multiplier": config.WICK_MIN_LIQUIDITY_MULTIPLIER,
            "bet_size_override": config.WICK_BET_SIZE,
            "min_distance_mult": 0.0,
            "absolute_min_distance": max(config.asset_min_price_distance(self.asset) * 0.30, ctx["sigma_remaining"] * 0.10),
            "max_distance_sigma_mult": 3.80,
            "velocity_to_line": round(velocity_to_line, 4),
            "min_velocity_to_line": round(min_velocity, 4),
            "wick_strength": round(wick_strength, 4),
            **wick,
        })

    def _signal_lottery_reversal(self, ctx: Dict[str, Any]) -> Optional[ReversalSignal]:
        if not config.LOTTERY_MODE:
            return None
        seconds_left = ctx["seconds_left"]
        min_s, max_s = self._tf_bounds(config.LOTTERY_MIN_SECONDS_LEFT, config.LOTTERY_MAX_SECONDS_LEFT)
        if not (min_s <= seconds_left <= max_s):
            return None
        distance = ctx["distance"]
        if abs(distance) < max(config.asset_min_price_distance(self.asset), ctx["sigma_remaining"] * 0.55):
            return None
        velocity_to_line = ctx["velocity_to_line"]
        if velocity_to_line < max(config.asset_min_price_distance(self.asset) * config.LOTTERY_MIN_LINE_CLOSING_FACTOR,
                                  ctx["sigma_remaining"] * config.LOTTERY_MIN_LINE_CLOSING_SIGMA):
            return None
        # Buy the cheap side only when price is moving back toward the line fast enough.
        direction = "DOWN" if distance > 0 else "UP"
        target = ctx["p_down"] if direction == "DOWN" else ctx["p_up"]
        if target < config.LOTTERY_MIN_MODEL_PROB or target > config.LOTTERY_MAX_MODEL_PROB:
            return None
        return self._finalize_signal(ctx, "lottery_reversal", direction, target, ctx["p_up"], ctx["p_down"], {
            "strategy_name": "lottery_reversal",
            "min_model_prob": config.LOTTERY_MIN_MODEL_PROB,
            "min_confidence_to_trade": config.LOTTERY_MIN_MODEL_PROB,
            "min_edge_after_fees": config.LOTTERY_MIN_EDGE_AFTER_FEES,
            "min_token_price": config.LOTTERY_MIN_TOKEN_PRICE,
            "max_token_price": config.LOTTERY_MAX_TOKEN_PRICE,
            "max_spread": config.LOTTERY_MAX_SPREAD,
            "max_market_prob_gap": config.LOTTERY_MAX_MARKET_PROB_GAP,
            "market_consensus_weight": config.LOTTERY_MARKET_CONSENSUS_WEIGHT,
            "min_liquidity_multiplier": config.LOTTERY_MIN_LIQUIDITY_MULTIPLIER,
            "bet_size_override": config.LOTTERY_BET_SIZE,
            "min_distance_mult": 0.0,
            "absolute_min_distance": 0.0,
            "velocity_to_line": round(velocity_to_line, 4),
            "pinbar": ctx["pin_kind"],
        })

    # ---- shared helpers ----

    def _finalize_signal(self, ctx: Dict[str, Any], base_pattern: str, direction: str, target_prob: float,
                         p_up: float, p_down: float, extra: Dict[str, Any]) -> Optional[ReversalSignal]:
        min_model = float(extra.get("min_model_prob", config.MIN_MODEL_PROB))
        min_conf = float(extra.get("min_confidence_to_trade", min_model))
        if target_prob < min_model:
            return None

        pattern = f"{base_pattern}_{direction.lower()}"
        raw_target_prob = target_prob
        if self.db:
            target_prob *= self.db.get_signal_weight(pattern)
            # V13.4: also apply per-asset/per-strategy weight after enough samples.
            if hasattr(self.db, "get_asset_strategy_weight"):
                strategy_name = str(extra.get("strategy_name") or base_pattern).lower()
                target_prob *= self.db.get_asset_strategy_weight(self.asset, strategy_name)
            target_prob = self.db.calibrate_probability(target_prob, direction)
            target_prob = max(0.0, min(0.98, target_prob))
        if target_prob < min_conf:
            return None

        indicators = {
            "reference_source": config.REFERENCE_PRICE_SOURCE,
            "reference_price": round(ctx["reference_price"], 4),
            "asset": self.asset,
            "timeframe": self.timeframe,
            "market_interval_sec": self.market_interval_sec,
            "asset_current": round(ctx["price"], 4),
            "btc_current": round(ctx["price"], 4),  # legacy key
            "distance_to_reference": round(ctx["distance"], 4),
            "sigma_remaining": round(ctx["sigma_remaining"], 4),
            "per_min_vol": round(ctx["per_min_vol"], 8),
            "expected_drift_usd": round(ctx["drift"], 4),
            "up_probability": round(p_up, 4),
            "down_probability": round(p_down, 4),
            "raw_target_probability": round(raw_target_prob, 4),
            "calibrated_probability": round(target_prob, 4),
            "recent_change_2m": round(ctx.get("recent_change", 0.0), 6),
            "velocity_to_line": round(ctx.get("velocity_to_line", 0.0), 4),
            **(ctx.get("wick") or {}),
        }
        indicators.update(extra)
        return ReversalSignal(
            direction=direction,
            confidence=target_prob,
            pattern=pattern,
            btc_price=ctx["price"],
            window_ts=ctx["window_ts"],
            seconds_left=ctx["seconds_left"],
            timeframe=self.timeframe,
            market_interval_sec=self.market_interval_sec,
            target_probability=target_prob,
            up_probability=p_up,
            down_probability=p_down,
            reference_price=ctx["reference_price"],
            distance_to_reference=ctx["distance"],
            sigma_remaining=ctx["sigma_remaining"],
            indicators=indicators,
            asset=self.asset,
            adjusted_score=self._score_signal(base_pattern, target_prob, indicators),
        )

    def _score_signal(self, pattern: str, target_prob: float, indicators: Dict[str, Any]) -> float:
        # Prefer high-probability main/snipe signals while still allowing lottery to be selected when no better signal exists.
        bonus = {
            "barrier_reclaim": 0.060,
            "late_line_snipe": 0.045,
            "prob_edge": 0.030,
            "momentum_follow": 0.015,
            "wick_rejection_odds": -0.005,
            "odds_ev": -0.010,
            "odds_lag": -0.006,
            "odds_reversal": -0.015,
            "lottery_reversal": -0.080,
        }.get(str(indicators.get("strategy_name") or pattern).lower(), 0.0)
        return float(target_prob or 0.0) + bonus

    def _probabilities(self, price: float, reference_price: float, sigma_remaining: float, drift: float) -> Tuple[float, float]:
        expected_end = price + drift
        z = (expected_end - reference_price) / max(sigma_remaining, 1.0)
        p_up = self._normal_cdf(z)
        return p_up, 1.0 - p_up

    def _asset_sigma_floor(self, price: float) -> float:
        # Prevent tiny sigma on quiet feeds from creating fake 99% probabilities.
        asset = self.asset.upper()
        if asset == "BTC":
            return max(1.0, price * 0.00008)
        if asset == "ETH":
            return max(0.20, price * 0.00012)
        if asset == "SOL":
            return max(0.02, price * 0.00018)
        if asset == "XRP":
            return max(0.0005, price * 0.00022)
        return max(1.0, price * 0.00010)

    def _recent_returns(self, minutes: int) -> List[float]:
        klines = self.feed.get_recent_klines(minutes + 1)
        closes = [k.close for k in klines if k.close > 0]
        if self.feed.current_kline and self.feed.current_kline.close > 0:
            closes.append(self.feed.current_kline.close)
        if len(closes) < 2:
            return []
        return [(closes[i] - closes[i - 1]) / closes[i - 1] for i in range(1, len(closes)) if closes[i - 1] > 0]

    def _recent_price_change(self, minutes: int) -> float:
        klines = self.feed.get_recent_klines(minutes + 1)
        closes = [k.close for k in klines if k.close > 0]
        if self.feed.current_kline and self.feed.current_kline.close > 0:
            closes.append(self.feed.current_kline.close)
        if len(closes) < 2:
            return 0.0
        return (closes[-1] - closes[0]) / closes[0] if closes[0] > 0 else 0.0

    def _line_closing_velocity(self, reference_price: float, current_price: float) -> float:
        """Positive USD value means price moved toward Price-to-Beat over the last ~2 minutes."""
        klines = self.feed.get_recent_klines(3)
        prices = [k.close for k in klines if k.close > 0]
        if self.feed.current_kline and self.feed.current_kline.close > 0:
            prices.append(self.feed.current_kline.close)
        if len(prices) < 2:
            return 0.0
        previous = prices[0]
        prev_dist = abs(previous - reference_price)
        curr_dist = abs(current_price - reference_price)
        return prev_dist - curr_dist

    def _momentum_drift(self, price: float, remaining_min: float) -> float:
        rets = self._recent_returns(config.MOMENTUM_LOOKBACK_MIN + 1)
        if not rets:
            return 0.0
        avg = statistics.mean(rets[-config.MOMENTUM_LOOKBACK_MIN:])
        # Clamp so one noisy candle does not dominate.
        avg = max(-config.MOMENTUM_DRIFT_CLAMP, min(config.MOMENTUM_DRIFT_CLAMP, avg))
        return price * avg * remaining_min * config.MOMENTUM_WEIGHT

    @staticmethod
    def _wick_metrics(k: Kline) -> Dict[str, float]:
        total = float(k.total_range or 0.0)
        body = float(k.body or (total * 0.05) or 0.0)
        if total <= 0:
            return {
                "upper_wick_body_ratio": 0.0,
                "lower_wick_body_ratio": 0.0,
                "upper_wick_range_ratio": 0.0,
                "lower_wick_range_ratio": 0.0,
                "body_range_ratio": 1.0,
            }
        return {
            "upper_wick_body_ratio": round(float(k.upper_wick or 0.0) / max(body, 1e-9), 4),
            "lower_wick_body_ratio": round(float(k.lower_wick or 0.0) / max(body, 1e-9), 4),
            "upper_wick_range_ratio": round(float(k.upper_wick or 0.0) / total, 4),
            "lower_wick_range_ratio": round(float(k.lower_wick or 0.0) / total, 4),
            "body_range_ratio": round(body / total, 4),
        }

    def _pinbar_bonus(self, klines: List[Kline], distance: float) -> Tuple[str, float]:
        latest = klines[-1]
        total_range = latest.total_range
        if total_range < config.asset_pin_bar_min_range(self.asset):
            return "none", 0.0
        body = latest.body or total_range * 0.05
        body_ratio = body / total_range if total_range > 0 else 1.0
        upper_wick_ratio = latest.upper_wick / body if body > 0 else 0
        lower_wick_ratio = latest.lower_wick / body if body > 0 else 0

        bearish_rejection = (
            upper_wick_ratio >= config.PIN_BAR_TAIL_RATIO and
            body_ratio <= config.PIN_BAR_BODY_MAX_RATIO and
            latest.upper_wick > latest.lower_wick * 1.5
        )
        bullish_rejection = (
            lower_wick_ratio >= config.PIN_BAR_TAIL_RATIO and
            body_ratio <= config.PIN_BAR_BODY_MAX_RATIO and
            latest.lower_wick > latest.upper_wick * 1.5
        )

        # Only confirm if rejection supports the side already favored by distance to reference.
        if distance > 0 and bullish_rejection:
            return "bullish_confirm", 0.035
        if distance < 0 and bearish_rejection:
            return "bearish_confirm", 0.035
        if (distance > 0 and bearish_rejection) or (distance < 0 and bullish_rejection):
            return "against_signal", 0.0
        return "none", 0.0

    @staticmethod
    def _normal_cdf(x: float) -> float:
        return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))

    def is_black_swan(self) -> bool:
        change = abs(self.feed.get_price_change_pct(5))
        return change > config.asset_black_swan_threshold(self.asset)
