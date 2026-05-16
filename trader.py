"""
Trader - orchestrates strategy + risk + Polymarket execution.

V6 execution changes:
- Reprices signal with Gamma Price-to-Beat when available.
- Uses market BUY amount for FOK/FAK and confirms fills when possible.
- Handles delayed order status instead of silently losing track.
- Calls Learner after settlement so review notes are actually written.
- Blends model probability with live market consensus, logs every decision, and performs binary-book sanity checks.
"""
import asyncio
import logging
import time
from typing import Optional, Callable

from strategy import PinBarStrategy, ReversalSignal
from polymarket_client import PolymarketClient
from risk_manager import RiskManager
from binance_feed import BinanceFeed, MultiAssetBinanceFeed
from database import Database
from learner import Learner
from config import config
from http_utils import client_session
from opportunity_scorer import OpportunityScorer, strategy_name_from
from adaptive_quality_gate import AdaptiveQualityGate
from strategy_weight_controller import StrategyWeightController
from market_event_coalescer import MarketEventCoalescer
from execution_quality_guard import ExecutionQualityGuard
from dynamic_tick_guard import DynamicTickGuard
from execution_flight_recorder import ExecutionFlightRecorder
from high_win_rate_guard import HighWinRateGuard
from adaptive_edge_engine import AdaptiveEdgeEngine

try:
    from profit_rule_engine import profit_rule_decision, record_trade_result as profit_rule_record_trade
except Exception:
    profit_rule_decision = None
    profit_rule_record_trade = None

log = logging.getLogger(__name__)


class Trader:
    # Legacy reason marker: barrier_reclaim_price_impact_high
    def __init__(self, strategy, polymarket: PolymarketClient,
                 risk: RiskManager, feed, db: Database,
                 notify: Optional[Callable] = None, learner: Optional[Learner] = None):
        # strategy may be one PinBarStrategy (legacy) or {asset: PinBarStrategy}.
        self.strategy = strategy
        self.strategies = strategy if isinstance(strategy, dict) else {getattr(strategy, "asset", "BTC"): strategy}
        self.polymarket = polymarket
        self.risk = risk
        self.feed = feed
        self.db = db
        self.notify = notify or (lambda *a, **k: None)
        self.learner = learner
        self.running = False
        self._last_observe_log = {}
        self.scorer = OpportunityScorer(db)
        self.quality_gate = AdaptiveQualityGate(db)
        self.strategy_weight_controller = StrategyWeightController(db)
        self.event_coalescer = MarketEventCoalescer()
        self.execution_guard = ExecutionQualityGuard(polymarket)
        self.tick_guard = DynamicTickGuard(getattr(polymarket, "realtime_orderbook", None))
        self.flight_recorder = ExecutionFlightRecorder(db)
        self.high_win_rate_guard = HighWinRateGuard(db)
        self.adaptive_edge_engine = AdaptiveEdgeEngine(db)
        # v14.2.29: only in-memory guard for a currently running post_order call.
        # Persistent order_attempts must represent real API submission results,
        # not pre-submit checks, otherwise windows get false-locked.
        self._inflight_submit_keys = set()

    def _feed_for_asset(self, asset: str):
        if hasattr(self.feed, "get_feed"):
            return self.feed.get_feed(asset)
        return self.feed

    # ============ Position sizing ============

    def calc_bet_amount(self, signal: ReversalSignal, balance: float) -> float:
        # 每笔金额 is the only canonical small-live stake.
        # If capital isolation is enabled, size from authorized trading equity
        # only. Protected wallet reserve is never treated as available capital.
        available = float(balance or 0.0)
        risk = getattr(self, "risk", None)
        if risk and config.capital_isolation_enabled:
            try:
                available = risk.available_trading_equity(balance)
            except Exception as e:
                log.error("risk.available_trading_equity failed: %s", e, exc_info=True)
                raise RuntimeError(f"risk_available_equity_failed: {e}") from e
        if config.MODE in {"paper", "small_live"}:
            return min(config.effective_per_order_amount, available)
        return config.calc_bet_size(signal.confidence, available)

    @staticmethod
    def estimate_shares(amount_usd: float, price: float) -> float:
        if price <= 0:
            return 0.0
        return max(0.0, amount_usd / price)


    @staticmethod
    def _order_weighted_avg_from_ob(ob: dict, shares: float, max_price: float | None = None):
        """Return order-size weighted-average data without silent fallback.

        ``weighted_avg`` is populated only when the visible asks can satisfy the
        requested share size inside ``max_price``.  If the book is empty or only
        partially fills the target size, callers must reject the signal instead
        of falling back to ``best_ask`` / ``weighted_avg_ask_to_cap``.
        """
        try:
            need = float(shares or 0.0)
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
        asks = (ob or {}).get("asks") or []
        if need <= 0 or not asks:
            return base
        rows = []
        for row in asks:
            try:
                price, size = row
                price = float(price)
                size = float(size)
            except Exception:
                continue
            if max_price is not None and price > float(max_price):
                continue
            if price > 0 and size > 0:
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

    @staticmethod
    def _order_avg_gate_status(order_avg: dict | None, target_shares: float, *, min_fill_ratio: float = 0.95,
                               best_ask: float | None = None) -> tuple[bool, str, dict]:
        """Validate order-size weighted average data before any impact math."""
        data = order_avg or {}
        try:
            target = float(data.get("target_shares") or target_shares or 0.0)
        except Exception:
            target = 0.0
        try:
            filled = float(data.get("filled_shares") or 0.0)
        except Exception:
            filled = 0.0
        fill_ratio = (filled / target) if target > 0 else 0.0
        weighted_avg = data.get("weighted_avg")
        details = {
            "weighted_avg": weighted_avg,
            "partial_weighted_avg": data.get("partial_weighted_avg"),
            "filled_shares": filled,
            "target_shares": target,
            "fill_ratio": round(fill_ratio, 6),
            "depth_exhausted": bool(data.get("depth_exhausted", True)),
            "complete": bool(data.get("complete", False)),
        }
        if best_ask is not None:
            details["best_ask"] = best_ask
        if weighted_avg is None:
            reason = "orderbook_too_thin_no_fill" if filled <= 0 else "orderbook_depth_insufficient"
            return False, reason, details
        if target <= 0 or fill_ratio < float(min_fill_ratio):
            return False, "orderbook_depth_insufficient", details
        return True, "", details

    def _record_decision(self, signal: ReversalSignal, *, market: Optional[dict] = None, action: str = "skip",
                         reason: str = "", token_price: float = 0.0, edge_after_fees: float = 0.0,
                         details: Optional[dict] = None):
        try:
            self.db.record_decision(
                window_ts=getattr(signal, "window_ts", None),
                market_slug=(market or {}).get("slug", ""),
                direction=getattr(signal, "direction", ""),
                action=action,
                reason=reason,
                model_probability=float(getattr(signal, "target_probability", 0) or 0),
                token_price=float(token_price or 0),
                edge_after_fees=float(edge_after_fees or 0),
                details=details or {},
                asset=getattr(signal, "asset", "BTC"),
                timeframe=getattr(signal, "timeframe", "5m"),
            )
        except Exception as e:
            log.debug("decision log failed: %s", e)

    def _skip(self, signal: ReversalSignal, reason: str, *, market: Optional[dict] = None,
              token_price: float = 0.0, edge_after_fees: float = 0.0, details: Optional[dict] = None) -> bool:
        self._record_decision(signal, market=market, action="skip", reason=reason,
                              token_price=token_price, edge_after_fees=edge_after_fees, details=details)
        return False

    def _record_observation(self, asset: str, strategy_obj, window_ts: int):
        """Write low-rate no-signal diagnostics so Telegram can explain quiet periods."""
        try:
            now = int(time.time())
            asset = str(asset or "BTC").upper()
            key = (asset, int(window_ts or 0), getattr(strategy_obj, "last_no_signal_reason", "no_signal"))
            last = self._last_observe_log.get(key, 0)
            if now - last < max(5, int(config.OBSERVATION_LOG_INTERVAL_SEC)):
                return
            self._last_observe_log[key] = now
            if len(self._last_observe_log) > 1000:
                sorted_items = sorted(self._last_observe_log.items(), key=lambda x: x[1])
                self._last_observe_log = dict(sorted_items[-500:])
                log.info("Observation log LRU compacted to %s keys", len(self._last_observe_log))
            dummy = ReversalSignal(
                direction="", confidence=0.0, pattern="observe", btc_price=0.0,
                window_ts=int(window_ts or 0), seconds_left=0, asset=asset,
            )
            self.db.record_decision(
                window_ts=int(window_ts or 0), market_slug="", asset=asset, timeframe=getattr(strategy_obj, "timeframe", "5m"), direction="",
                action="observe", reason=getattr(strategy_obj, "last_no_signal_reason", "no_strategy_signal"),
                model_probability=0.0, token_price=0.0, edge_after_fees=0.0,
                details=getattr(strategy_obj, "last_no_signal_details", {}) or {},
            )
        except Exception as e:
            log.debug("observation log failed: %s", e)

    # ============ Trade execution ============

    async def try_execute_signal(self, signal: ReversalSignal) -> bool:
        asset = str(getattr(signal, "asset", "BTC") or "BTC").upper()
        timeframe = config.timeframe_slug(getattr(signal, "timeframe", "5m") or "5m")
        feed = self._feed_for_asset(asset)
        if isinstance(self.strategies, dict):
            strategy_obj = self.strategies.get(f"{asset}:{timeframe}") or self.strategies.get(asset) or next(iter(self.strategies.values()))
        else:
            strategy_obj = self.strategy
        strategy_params = getattr(signal, "indicators", {}) or {}

        strategy_name_for_gate = strategy_name_from(signal)
        # V14: coalesce exact duplicate strategy/direction events only. The previous market-level
        # key suppressed lower-ranked signals in the same 5m market after the first skip, which
        # contradicted the intended "try more candidates than actual order cap" behavior.
        market_key = f"{asset}:{timeframe}:{int(signal.window_ts)}:{strategy_name_for_gate}:{getattr(signal, 'direction', '')}"
        if not self.event_coalescer.should_process(market_key):
            return self._skip(signal, "decision_cycle_coalesced", details={"coalesce_key": market_key})
        active, hist_weight, hist_reason = self.strategy_weight_controller.status(asset, strategy_name_for_gate)
        if not active:
            return self._skip(signal, hist_reason, details={"strategy_name": strategy_name_for_gate, "history_weight": hist_weight})

        if self.db.has_trade_for_window(signal.window_ts, asset=asset, timeframe=timeframe):
            log.info("⚠️  Skip %s: already traded this 5m window", asset)
            return self._skip(signal, "already_traded_window")

        if hasattr(feed, "is_stale") and feed.is_stale(config.STALE_FEED_MAX_SEC):
            log.info("⚠️  Skip %s: Binance proxy feed is stale", asset)
            return self._skip(signal, "stale_proxy_feed")

        # In live-only builds, do not spam private balance endpoints before TG credentials
        # are saved and the client is reconnected. This is a protective skip, not a strategy failure.
        if config.real_orders_enabled and not config.has_polymarket_creds:
            return self._skip(signal, "credentials_not_configured", details={"reason": "telegram_trade_keys_missing"})
        if config.real_orders_enabled and not getattr(self.polymarket, "connected", False):
            return self._skip(signal, "client_not_authenticated", details={"reason": "click_reconnect_auth_in_tg"})

        balance = await self._get_balance()
        if balance is None:
            return self._skip(signal, "balance_unavailable", details={"reason": "balance_read_failed"})
        try:
            initial_bet = self.calc_bet_amount(signal, balance)
        except Exception as e:
            log.error("risk/capital sizing failed before trade checks: %s", e, exc_info=True)
            return self._skip(signal, "risk_module_exception", details={"error": str(e)})
        if initial_bet <= 0:
            return self._skip(signal, "invalid_bet_amount", details={"balance": balance, "initial_bet": initial_bet})
        risk_check = self.risk.can_trade_asset(asset, balance, initial_bet, feed=feed)
        if not risk_check.can_trade:
            log.info(f"⚠️  Skip signal: {risk_check.reason}")
            # Only notify once when entering halted state
            if risk_check.severity == "critical" and "Bot halted" in risk_check.reason:
                # Check if we already notified for this halt reason
                last_halt_notify = self.db.get_state("last_halt_notify")
                if last_halt_notify != risk_check.reason:
                    await self.notify("risk_critical", {"reason": risk_check.reason, "balance": balance or 0.0})
                    self.db.set_state("last_halt_notify", risk_check.reason)
            return self._skip(signal, "risk_block", details={"reason": risk_check.reason, "balance": balance})

        market = await self.polymarket.find_updown_market(asset, signal.window_ts, timeframe, allow_nearby=config.ALLOW_NEARBY_MARKET_FOR_TRADING)
        if not market:
            log.warning(f"No Polymarket {asset} market found for window {signal.window_ts}")
            return self._skip(signal, "market_not_found")

        if not market.get("enable_order_book", True) or not market.get("active", True) or market.get("closed") or market.get("archived"):
            log.info("⚠️  Skip: market not tradable active=%s closed=%s", market.get("active"), market.get("closed"))
            return self._skip(signal, "market_not_tradable", market=market, details={"active": market.get("active"), "closed": market.get("closed"), "archived": market.get("archived")})

        if market.get("slug_window_ts") and int(market["slug_window_ts"]) != int(signal.window_ts):
            log.warning("Skip: exact window mismatch signal=%s market=%s slug=%s", signal.window_ts, market.get("slug_window_ts"), market.get("slug"))
            return self._skip(signal, "market_window_mismatch", market=market, details={"signal_window_ts": signal.window_ts, "slug_window_ts": market.get("slug_window_ts")})

        if config.STRICT_CHAINLINK_RULE_TEXT and not market.get("rules_chainlink_ok"):
            log.warning("Skip: market metadata did not confirm Chainlink/%s rules", asset)
            return self._skip(signal, "chainlink_rule_text_missing", market=market)

        submit_key = (asset, timeframe, int(signal.window_ts))
        try:
            self.db.cleanup_stale_pre_submit_attempts(stale_sec=15)
        except Exception as e:
            log.warning("cleanup_stale_pre_submit_attempts failed; continuing without cleanup: %s", e)
        if submit_key in self._inflight_submit_keys:
            log.warning("Skip: in-memory post_order is already in flight for this window")
            return self._skip(signal, "recent_order_attempt_lock", market=market, details={"source": "in_memory_submit_inflight"})
        if self.db.has_recent_order_attempt(signal.window_ts, config.ORDER_ATTEMPT_LOCK_SEC, asset=asset, timeframe=timeframe):
            log.warning("Skip: recent order attempt lock is active for this window")
            return self._skip(signal, "recent_order_attempt_lock", market=market)

        price_to_beat_source = "gamma" if market.get("price_to_beat") else "missing"
        if config.REQUIRE_MARKET_PRICE_TO_BEAT and not market.get("price_to_beat"):
            fallback_price = float(getattr(signal, "reference_price", 0.0) or 0.0)
            if getattr(config, "PRICE_TO_BEAT_FALLBACK_ENABLED", True) and fallback_price > 0:
                market["price_to_beat"] = fallback_price
                market["price_to_beat_source"] = "signal_reference_fallback"
                price_to_beat_source = "signal_reference_fallback"
                log.warning(
                    "⚠️  Gamma Price-to-Beat missing; using signal reference fallback %.4f for %s %s %s. "
                    "Orderbook/edge/slippage checks still apply.",
                    fallback_price, asset, timeframe, market.get("slug", "")
                )
                self.flight_recorder.record(
                    "price_to_beat_fallback", signal=signal, market=market, reason="signal_reference_fallback",
                    details={"fallback_price_to_beat": fallback_price, "fallback_source": "signal.reference_price"}
                )
            else:
                log.info("⚠️  Skip: missing Gamma Price-to-Beat and no safe reference fallback")
                return self._skip(signal, "missing_price_to_beat", market=market, details={"fallback_available": fallback_price > 0})

        # If Gamma exposes the official Price-to-Beat, use it instead of Binance proxy open.
        # If Gamma omits it, V14.2.16 uses signal.reference_price as a fallback so the bot
        # can still reach the live orderbook/edge/slippage guards instead of skipping early.
        if config.USE_MARKET_PRICE_TO_BEAT and market.get("price_to_beat"):
            adjusted = strategy_obj.reprice_signal(signal, float(market["price_to_beat"]))
            if not adjusted:
                log.info("⚠️  Skip: no edge after repricing against Gamma Price-to-Beat")
                return self._skip(signal, "repricing_removed_edge", market=market, details={"price_to_beat": market.get("price_to_beat"), "price_to_beat_source": price_to_beat_source})
            signal = adjusted

        if signal.direction == "DOWN":
            target_token = market["down_token_id"]
            target_outcome = "Down"
            model_prob = signal.target_probability
            resolved_price_key = "down_outcome_price"
        else:
            target_token = market["up_token_id"]
            target_outcome = "Up"
            model_prob = signal.target_probability
            resolved_price_key = "up_outcome_price"

        model_prob = max(0.0, min(0.98, float(model_prob or 0)))
        min_model_prob = float(strategy_params.get("min_model_prob", config.MIN_MODEL_PROB) or config.MIN_MODEL_PROB)
        if model_prob < min_model_prob:
            log.info("⚠️  Skip: model probability %.2f below %.2f", model_prob, min_model_prob)
            return self._skip(signal, "model_probability_low", market=market, details={"model_prob": model_prob, "min_model_prob": min_model_prob})

        if not target_token:
            log.warning("Token ID not available")
            return self._skip(signal, "token_id_missing", market=market)

        if config.REALTIME_ORDERBOOK_ENABLED:
            try:
                await self.polymarket.subscribe_market_orderbook(market)
                await self.polymarket.wait_for_realtime_orderbook([target_token], config.REALTIME_ORDERBOOK_WARMUP_SEC)
            except Exception as e:
                log.warning("Realtime orderbook warmup failed; continuing with REST summary guard: %s", e)

        is_barrier_reclaim = bool(strategy_params.get("barrier_reclaim")) or str(strategy_params.get("strategy_name") or signal.pattern or "").lower().startswith("barrier_reclaim") or str(strategy_name_from(signal)).lower() == "barrier_reclaim"
        min_token_price = float(strategy_params.get("min_token_price", config.MIN_TOKEN_PRICE) or config.MIN_TOKEN_PRICE)
        max_token_price = float(strategy_params.get("max_token_price", config.MAX_TOKEN_PRICE) or config.MAX_TOKEN_PRICE)
        max_spread = float(strategy_params.get("max_spread", config.MAX_SPREAD) or config.MAX_SPREAD)
        if is_barrier_reclaim:
            max_spread = max(max_spread, float(getattr(config, "BARRIER_RECLAIM_MAX_SPREAD", max_spread) or max_spread))
        # v14.2.19: $1 live mode can tolerate normal 5m binary-market spread;
        # TARGET_SPREAD_MAX acts as a minimum tolerance so overly tight per-strategy
        # defaults do not block every otherwise-strong candidate.
        max_spread = max(max_spread, float(getattr(config, "TARGET_SPREAD_MAX", max_spread) or max_spread))
        max_market_prob_gap = float(strategy_params.get("max_market_prob_gap", config.MAX_MARKET_PROB_GAP) or config.MAX_MARKET_PROB_GAP)
        market_consensus_weight = float(strategy_params.get("market_consensus_weight", config.MARKET_CONSENSUS_WEIGHT) or 0.0)
        min_edge_after_fees = float(strategy_params.get("min_edge_after_fees", config.MIN_EDGE_AFTER_FEES) or config.MIN_EDGE_AFTER_FEES)
        min_liquidity_multiplier = float(strategy_params.get("min_liquidity_multiplier", config.MIN_LIQUIDITY_MULTIPLIER) or config.MIN_LIQUIDITY_MULTIPLIER)

        preliminary_cap = max_token_price + config.MARKET_BUY_SLIPPAGE
        ob = self.polymarket.get_orderbook_summary(target_token, max_price=preliminary_cap)
        if not ob or ob["best_ask"] is None:
            log.warning("No orderbook depth for token=%s cap=%s", target_token, preliminary_cap)
            return self._skip(signal, "orderbook_missing", market=market, details={"token_id": target_token, "cap": preliminary_cap, "realtime_enabled": bool(config.REALTIME_ORDERBOOK_ENABLED)})

        token_price = ob["best_ask"]
        self.flight_recorder.record("orderbook_initial", signal=signal, market=market, token_id=target_token, outcome=target_outcome, reason="initial_orderbook_ok", details={"orderbook": {k: ob.get(k) for k in ("source", "best_ask", "best_bid", "spread", "age_sec", "ask_depth_to_cap", "last_event_type")}})
        if not (min_token_price <= token_price <= max_token_price):
            # V14.2.20: high-confidence override for $1 small-live orders.
            # Some trend signals legitimately trade 0.70-0.90 tokens; older
            # strategy-specific ranges were too tight and caused zero orders.
            # This override never allows extreme 0.99 tickets and still keeps
            # all spread, edge, quality, balance, capital and FOK checks.
            fee_guess = config.taker_fee_per_share(token_price)
            edge_guess = float(model_prob or 0.0) - float(token_price or 0.0) - float(fee_guess or 0.0)
            override_allowed = (
                bool(getattr(config, "HIGH_CONFIDENCE_TOKEN_RANGE_OVERRIDE_ENABLED", True))
                and float(model_prob or 0.0) >= float(getattr(config, "HIGH_CONFIDENCE_TOKEN_RANGE_MIN_PROB", 0.90))
                and edge_guess >= float(getattr(config, "HIGH_CONFIDENCE_TOKEN_RANGE_MIN_EDGE", 0.060))
                and float(token_price or 0.0) <= float(getattr(config, "HIGH_CONFIDENCE_TOKEN_RANGE_MAX_TOKEN_PRICE", 0.92))
                and float(token_price or 0.0) >= float(config.MIN_TOKEN_PRICE)
            )
            if override_allowed:
                old_max = max_token_price
                max_token_price = max(max_token_price, min(float(getattr(config, "HIGH_CONFIDENCE_TOKEN_RANGE_MAX_TOKEN_PRICE", 0.92)), float(token_price) + float(config.MARKET_BUY_SLIPPAGE)))
                log.info("✅ Token range override: ask=%.3f model=%.3f edge≈%.3f old_range=[%.2f, %.2f] new_max=%.3f", token_price, model_prob, edge_guess, min_token_price, old_max, max_token_price)
                self._record_decision(signal, market=market, action="candidate", reason="token_range_override_high_confidence", token_price=token_price, edge_after_fees=edge_guess, details={"old_min": min_token_price, "old_max": old_max, "new_max": max_token_price, "model_prob": model_prob, "edge_guess": edge_guess, "strategy": signal.pattern})
            else:
                log.info("⚠️  Skip: token price $%.3f outside [%.2f, %.2f] for %s", token_price, min_token_price, max_token_price, signal.pattern)
                return self._skip(signal, "token_price_outside_range", market=market, token_price=token_price, details={"min": min_token_price, "max": max_token_price, "strategy": signal.pattern, "model_prob": model_prob, "edge_guess": edge_guess})

        if ob.get("spread") is not None and ob["spread"] > max_spread:
            log.info("⚠️  Skip: spread %.3f > %.3f", ob["spread"], max_spread)
            return self._skip(signal, "target_spread_high", market=market, token_price=token_price, details={"spread": ob.get("spread"), "max_spread": max_spread})

        if config.ENABLE_BOOK_SANITY_CHECK:
            opposite_token = market.get("up_token_id") if target_outcome == "Down" else market.get("down_token_id")
            opp_ob = self.polymarket.get_orderbook_summary(opposite_token, max_price=max(config.MAX_TOKEN_PRICE, max_token_price) + config.MARKET_BUY_SLIPPAGE) if opposite_token else None
            if not opp_ob or opp_ob.get("best_ask") is None:
                return self._skip(signal, "opposite_orderbook_missing", market=market, token_price=token_price)
            try:
                opp_best_ask = float(opp_ob.get("best_ask"))
            except Exception:
                return self._skip(signal, "opposite_orderbook_missing_after_check", market=market, token_price=token_price, details={"opposite_token": opposite_token, "opposite_best_ask": opp_ob.get("best_ask")})
            if opp_best_ask <= 0:
                return self._skip(signal, "opposite_orderbook_missing_after_check", market=market, token_price=token_price, details={"opposite_token": opposite_token, "opposite_best_ask": opp_ob.get("best_ask")})
            total_ask_sum = float(token_price) + opp_best_ask
            if not (config.MIN_BOOK_ASK_SUM <= total_ask_sum <= config.MAX_BOOK_ASK_SUM):
                log.info("⚠️  Skip: binary ask sum %.3f outside [%.3f, %.3f]", total_ask_sum, config.MIN_BOOK_ASK_SUM, config.MAX_BOOK_ASK_SUM)
                return self._skip(signal, "binary_book_sanity_failed", market=market, token_price=token_price, details={"total_ask_sum": total_ask_sum, "opposite_best_ask": opp_ob.get("best_ask")})
            if opp_ob.get("spread") is not None and opp_ob.get("spread") > config.MAX_OPPOSITE_SPREAD:
                log.info("⚠️  Skip: opposite spread %.3f > %.3f", opp_ob.get("spread"), config.MAX_OPPOSITE_SPREAD)
                return self._skip(signal, "opposite_spread_high", market=market, token_price=token_price, details={"opposite_spread": opp_ob.get("spread")})

        # Market consensus is itself useful data. If our local model is wildly far from the book,
        # assume our model is stale/wrong and skip instead of fighting the whole book.
        market_mid = token_price
        if ob.get("best_bid") is not None:
            market_mid = (float(ob["best_bid"]) + float(token_price)) / 2.0
        model_market_gap = abs(model_prob - market_mid)
        # v14.2.39: Shadow audit guardrails for barrier_reclaim.
        # The raw 56%-60% confidence is not a calibrated probability; extremely cheap
        # tickets and extreme model/book gaps were loss-only buckets in Shadow data.
        if is_barrier_reclaim and bool(getattr(config, "BARRIER_RECLAIM_GUARDRAILS_ENABLED", True)):
            min_entry_guard = float(getattr(config, "BARRIER_RECLAIM_MIN_ENTRY_PRICE_GUARD", 0.10) or 0.10)
            max_entry_guard = float(getattr(config, "BARRIER_RECLAIM_MAX_ENTRY_PRICE_GUARD", 0.50) or 0.50)
            min_model_prob_guard = float(getattr(config, "BARRIER_RECLAIM_MIN_MODEL_PROB_GUARD", 0.75) or 0.75)
            max_gap_guard = float(getattr(config, "BARRIER_RECLAIM_MAX_MODEL_MARKET_GAP_GUARD", 0.50) or 0.50)
            raw_model_prob_for_guard = float(model_prob or 0.0)
            entry_for_guard = float(token_price or 0.0)
            if raw_model_prob_for_guard < min_model_prob_guard and not bool(getattr(config, "PROFIT_RULE_ENGINE_ENABLED", False)):
                log.info("⚠️  Skip: barrier_reclaim model probability %.3f < guard %.3f", raw_model_prob_for_guard, min_model_prob_guard)
                return self._skip(signal, "barrier_reclaim_model_prob_too_low", market=market, token_price=token_price,
                                  details={"model_prob": raw_model_prob_for_guard, "min_model_prob_guard": min_model_prob_guard, "shadow_audit": "50-60% confidence bucket is not precise enough for current goal"})
            elif raw_model_prob_for_guard < min_model_prob_guard:
                log.info("🧠 Profit Rule Engine will decide low raw probability %.3f < %.3f", raw_model_prob_for_guard, min_model_prob_guard)
            if entry_for_guard < min_entry_guard:
                log.info("⚠️  Skip: barrier_reclaim entry %.3f < guard %.3f", token_price, min_entry_guard)
                return self._skip(signal, "barrier_reclaim_entry_too_low", market=market, token_price=token_price,
                                  details={"entry_price": token_price, "min_entry_guard": min_entry_guard, "shadow_audit": "entry below profitable/reliable range"})
            if entry_for_guard > max_entry_guard:
                log.info("⚠️  Skip: barrier_reclaim entry %.3f > guard %.3f", token_price, max_entry_guard)
                return self._skip(signal, "barrier_reclaim_entry_too_high", market=market, token_price=token_price,
                                  details={"entry_price": token_price, "max_entry_guard": max_entry_guard, "min_net_profit_multiple": round((1.0 / max_entry_guard) - 1.0, 4), "shadow_audit": "entry above 0.50 gives less than 1x net profit"})
            if float(model_market_gap or 0.0) >= max_gap_guard:
                log.info("⚠️  Skip: barrier_reclaim model/book gap %.3f >= guard %.3f", model_market_gap, max_gap_guard)
                return self._skip(signal, "barrier_reclaim_model_gap_guard", market=market, token_price=token_price,
                                  details={"gap": model_market_gap, "market_mid": market_mid, "max_gap_guard": max_gap_guard, "shadow_audit": "gap>=0.50 loss-only bucket"})
        if model_market_gap > max_market_prob_gap:
            log.info("⚠️  Skip: model/book gap %.3f > %.3f (model=%.3f market_mid=%.3f)",
                     model_market_gap, max_market_prob_gap, model_prob, market_mid)
            return self._skip(signal, "model_book_gap_high", market=market, token_price=token_price, details={"gap": model_market_gap, "market_mid": market_mid, "max_gap": max_market_prob_gap})
        if market_consensus_weight > 0:
            w = max(0.0, min(0.60, float(market_consensus_weight)))
            model_prob = model_prob * (1 - w) + market_mid * w

        fee_per_share = config.taker_fee_per_share(token_price)
        effective_cost_prob = token_price + fee_per_share
        edge_after_fees = model_prob - effective_cost_prob
        if edge_after_fees < min_edge_after_fees:
            log.info(
                "⚠️  Skip: edge %.3f < %.3f (model=%.3f ask=%.3f fee/share=%.3f)",
                edge_after_fees, min_edge_after_fees, model_prob, token_price, fee_per_share,
            )
            return self._skip(signal, "fee_adjusted_edge_low", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"model_prob": model_prob, "fee_per_share": fee_per_share, "min_edge": min_edge_after_fees})

        # v14.3.2 + v15.1.0: Adaptive Edge Engine.
        # Shadow mode remains the free learning lane; live mode uses stricter EV/quality
        # thresholds before any real order can reach Profit Rule / CLOB submission.
        adaptive_mode = "shadow"
        adaptive_submit_guard_reason = ""
        if not bool(getattr(config, "DRY_RUN", True)):
            try:
                adaptive_submit_guard_reason = self.polymarket.real_submit_guard_reason()
            except Exception as e:
                adaptive_submit_guard_reason = "real_submit_guard_exception"
                log.warning("real submit guard precheck failed: %s", e)
            adaptive_mode = "shadow" if adaptive_submit_guard_reason else "live"

        adaptive_decision = self.adaptive_edge_engine.evaluate(
            signal=signal,
            asset=asset,
            timeframe=timeframe,
            direction=target_outcome,
            entry_price=token_price,
            model_prob=model_prob,
            market_mid=market_mid,
            model_market_gap=model_market_gap,
            edge_after_fees=edge_after_fees,
            fee_per_share=fee_per_share,
            spread=ob.get("spread"),
            mode=adaptive_mode,
        )
        adaptive_details = adaptive_decision.to_dict()
        try:
            if isinstance(getattr(signal, "indicators", None), dict):
                signal.indicators["adaptive_edge"] = adaptive_details
                # Prefer the adaptive calibrated probability for learned-rule bucketing.
                signal.indicators["settlement_probability"] = adaptive_details.get("raw_settlement_probability")
                signal.indicators["calibrated_probability"] = adaptive_details.get("calibrated_probability")
                signal.indicators["expected_value"] = adaptive_details.get("expected_value")
        except Exception:
            pass
        if not adaptive_decision.allowed:
            log.info("🧮 Adaptive Edge blocked %s %s %s entry=%.4f prob=%.3f req=%.3f ev=%.3f q=%.3f reason=%s",
                     asset, timeframe, target_outcome, token_price,
                     float(adaptive_details.get("calibrated_probability") or 0),
                     float(adaptive_details.get("required_probability") or 0),
                     float(adaptive_details.get("expected_value") or 0),
                     float(adaptive_details.get("quality_score") or 0), adaptive_decision.reason)
            return self._skip(signal, adaptive_decision.reason, market=market, token_price=token_price,
                              edge_after_fees=edge_after_fees, details=adaptive_details)
        log.info("🧮 Adaptive Edge allowed %s %s %s entry=%.4f prob=%.3f req=%.3f ev=%.3f q=%.3f decision=%s",
                 asset, timeframe, target_outcome, token_price,
                 float(adaptive_details.get("calibrated_probability") or 0),
                 float(adaptive_details.get("required_probability") or 0),
                 float(adaptive_details.get("expected_value") or 0),
                 float(adaptive_details.get("quality_score") or 0), adaptive_decision.decision)
        self._record_decision(signal, market=market, action="candidate", reason=adaptive_decision.reason,
                              token_price=token_price, edge_after_fees=edge_after_fees, details=adaptive_details)

        # Legacy fixed high-win-rate guard is now optional/off by default.
        hwr_details = {"enabled": False, "reason": "disabled_by_adaptive_edge_engine"}
        if bool(getattr(config, "HIGH_WIN_RATE_MODE", False)):
            hwr_decision = self.high_win_rate_guard.evaluate(
                signal=signal,
                strategy_name=strategy_name_from(signal),
                model_prob=model_prob,
                token_price=token_price,
                edge_after_fees=edge_after_fees,
                market_mid=market_mid,
            )
            hwr_details = hwr_decision.to_dict()
            if not hwr_decision.allowed:
                log.info("🎯 High-win-rate guard rejected %s %s %s reason=%s details=%s",
                         asset, timeframe, strategy_name_from(signal), hwr_decision.reason, hwr_details)
                return self._skip(signal, hwr_decision.reason, market=market, token_price=token_price,
                                  edge_after_fees=edge_after_fees, details=hwr_details)
            self._record_decision(signal, market=market, action="candidate", reason=hwr_decision.reason,
                                  token_price=token_price, edge_after_fees=edge_after_fees, details=hwr_details)

        # V14: adaptive stream quality gate. This is the real-time filter that prevents
        # thousands of candidates from becoming trades. It scores the opportunity now,
        # then compares it with a dynamic threshold based on trade pace and PnL.
        estimated_cost_for_score = max(config.MIN_MARKET_ORDER_USD, initial_bet)
        score_obj = self.scorer.score(
            signal=signal, token_price=token_price, ob=ob, opp_ob=locals().get("opp_ob"),
            model_prob=model_prob, edge_after_fees=edge_after_fees, fee_per_share=fee_per_share,
            expected_cost=estimated_cost_for_score,
        )
        if is_barrier_reclaim and bool(getattr(config, "BARRIER_RECLAIM_USE_CUSTOM_QUALITY_GATE", True)):
            score_shares = self.estimate_shares(estimated_cost_for_score, token_price)
            if score_shares <= 0:
                return self._skip(signal, "invalid_share_estimate", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"amount": estimated_cost_for_score, "price": token_price})
            gate_avg = self._order_weighted_avg_from_ob(ob, score_shares, token_price + float(getattr(config, "BARRIER_RECLAIM_DEEP_EXECUTION_SLIPPAGE_CAP", 0.50)))
            gate_ok, gate_reason, gate_details = self._order_avg_gate_status(gate_avg, score_shares, best_ask=token_price)
            reclaim_stage = str((getattr(signal, "indicators", {}) or {}).get("reclaim_stage") or "")
            if float(token_price or 0.0) < float(getattr(config, "BARRIER_RECLAIM_AMBUSH_MIN_TOKEN_PRICE", 0.20)):
                reclaim_tier = "deep_ambush"
                min_edge_custom = float(getattr(config, "BARRIER_RECLAIM_DEEP_AMBUSH_MIN_EDGE_AFTER_FEES", 0.20))
                impact_cap = float(getattr(config, "BARRIER_RECLAIM_DEEP_MAX_PRICE_IMPACT", 0.16))
            elif reclaim_stage == "confirm" or float(token_price or 0.0) >= float(getattr(config, "BARRIER_RECLAIM_AMBUSH_MAX_TOKEN_PRICE", 0.45)):
                reclaim_tier = "confirm"
                min_edge_custom = float(getattr(config, "BARRIER_RECLAIM_CUSTOM_MIN_EDGE_AFTER_FEES", 0.08))
                impact_cap = float(getattr(config, "BARRIER_RECLAIM_CONFIRM_MAX_PRICE_IMPACT", 0.08))
            else:
                reclaim_tier = "standard_ambush"
                min_edge_custom = float(getattr(config, "BARRIER_RECLAIM_CUSTOM_MIN_EDGE_AFTER_FEES", 0.08))
                impact_cap = float(getattr(config, "BARRIER_RECLAIM_MAX_PRICE_IMPACT", 0.12))

            q_details = {
                "allowed": True,
                "reason": "barrier_reclaim_custom_gate_pass",
                "score": score_obj.to_dict(),
                "tier": reclaim_tier,
                "stage": reclaim_stage,
                "token_price": token_price,
                "edge_after_fees": edge_after_fees,
                "min_edge": min_edge_custom,
                "book_quality_score": score_obj.book_quality_score,
                "min_book_quality": float(getattr(config, "BARRIER_RECLAIM_MIN_BOOK_QUALITY", 0.45)),
                "price_impact": None,
                "weighted_avg_source": "order_size",
                "impact_cap": impact_cap,
                "spread": ob.get("spread"),
                "max_spread": float(getattr(config, "BARRIER_RECLAIM_MAX_SPREAD", max_spread)),
                **gate_details,
            }
            if not gate_ok:
                q_details["allowed"] = False
                q_details["reason"] = gate_reason
                log.info("⚠️  Skip: barrier reclaim orderbook depth gate failed reason=%s details=%s", gate_reason, gate_details)
                return self._skip(signal, gate_reason, market=market, token_price=token_price, edge_after_fees=edge_after_fees, details=q_details)
            weighted_for_gate = float(gate_details["weighted_avg"])
            impact = max(0.0, weighted_for_gate - float(token_price or 0.0))
            q_details["price_impact"] = impact
            if edge_after_fees < min_edge_custom:
                q_details["allowed"] = False
                q_details["reason"] = "barrier_reclaim_edge_low"
                return self._skip(signal, "barrier_reclaim_edge_low", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details=q_details)
            if score_obj.book_quality_score < float(getattr(config, "BARRIER_RECLAIM_MIN_BOOK_QUALITY", 0.45)):
                q_details["allowed"] = False
                q_details["reason"] = "barrier_reclaim_book_quality_low"
                return self._skip(signal, "barrier_reclaim_book_quality_low", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details=q_details)
            if impact > impact_cap:
                q_details["allowed"] = False
                q_details["reason"] = "barrier_reclaim_impact_too_high"
                return self._skip(signal, "barrier_reclaim_impact_too_high", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details=q_details)
            self._record_decision(signal, market=market, action="quality_pass", reason="barrier_reclaim_custom_gate_pass",
                                  token_price=token_price, edge_after_fees=edge_after_fees, details=q_details)
        else:
            gate_decision = self.quality_gate.evaluate(score_obj=score_obj, signal=signal)
            q_details = gate_decision.to_dict()
            if not gate_decision.allowed:
                log.info("⚠️  Quality gate rejected %s %s %s score=%.1f threshold=%.1f reason=%s",
                         asset, timeframe, strategy_name_from(signal), gate_decision.score, gate_decision.threshold, gate_decision.reason)
                return self._skip(signal, gate_decision.reason, market=market, token_price=token_price, edge_after_fees=edge_after_fees, details=q_details)
            self._record_decision(signal, market=market, action="quality_pass", reason="quality_gate_pass",
                                  token_price=token_price, edge_after_fees=edge_after_fees, details=q_details)


        bet_override = strategy_params.get("bet_size_override")
        try:
            if bet_override is not None:
                bet_usd = max(config.MIN_MARKET_ORDER_USD, min(float(bet_override), balance))
            else:
                bet_usd = max(config.MIN_MARKET_ORDER_USD, self.calc_bet_amount(signal, balance))
        except Exception as e:
            log.error("bet amount calculation failed: %s", e, exc_info=True)
            return self._skip(signal, "risk_module_exception", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"error": str(e), "bet_override": bet_override})
        expected_cost = min(bet_usd, balance)
        if expected_cost <= 0:
            return self._skip(signal, "invalid_bet_amount", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"bet_usd": bet_usd, "balance": balance})

        asset_risk = self.risk.can_trade_asset(asset, balance, expected_cost, feed=feed)
        if not asset_risk.can_trade:
            log.info("⚠️  Skip %s signal: %s", asset, asset_risk.reason)
            return self._skip(signal, "asset_risk_block", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"reason": asset_risk.reason, "balance": balance, "expected_cost": expected_cost})
        valid, msg = self.risk.validate_order(expected_cost, balance)
        if not valid:
            log.warning(f"Order validation failed: {msg}")
            return self._skip(signal, "order_validation_failed", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"message": msg, "balance": balance, "expected_cost": expected_cost})

        # Pre-check with intended order size. Do not use the all-depth-to-cap
        # average, which overstates impact for small $1 barrier reclaim orders.
        precheck_shares = self.estimate_shares(expected_cost, token_price)
        if precheck_shares <= 0:
            return self._skip(signal, "invalid_share_estimate", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"amount": expected_cost, "price": token_price})
        pre_avg = self._order_weighted_avg_from_ob(ob, precheck_shares, token_price + float(getattr(config, "BARRIER_RECLAIM_DEEP_EXECUTION_SLIPPAGE_CAP", 0.50)))
        pre_ok, pre_reason, pre_details = self._order_avg_gate_status(pre_avg, precheck_shares, best_ask=token_price)
        if not pre_ok:
            log.info("⚠️  Skip: pre-submit orderbook depth failed reason=%s details=%s", pre_reason, pre_details)
            return self._skip(signal, pre_reason, market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"stage": "pre_submit_weighted_avg", **pre_details})
        weighted_avg = float(pre_details["weighted_avg"])
        slippage_cap = float(config.EXECUTION_GUARD_SLIPPAGE_CAP)
        max_weighted_avg_price = None
        reclaim_exec_tier = None
        if is_barrier_reclaim:
            # v14.2.27: barrier_reclaim has its own execution caps.  The ordinary
            # 3.5% cap blocked real $1 reclaim setups like best_ask=0.26 with
            # weighted_avg≈0.34, even though the ticket still has ~3x payout if it wins.
            tp = float(token_price or 0.0)
            reclaim_stage = str((getattr(signal, "indicators", {}) or {}).get("reclaim_stage") or "")
            if tp < float(getattr(config, "BARRIER_RECLAIM_AMBUSH_MIN_TOKEN_PRICE", 0.20)):
                reclaim_exec_tier = "deep_ambush"
                slippage_cap = max(slippage_cap, float(getattr(config, "BARRIER_RECLAIM_DEEP_EXECUTION_SLIPPAGE_CAP", 0.50)))
                max_weighted_avg_price = float(getattr(config, "BARRIER_RECLAIM_DEEP_MAX_WEIGHTED_AVG_PRICE", 0.35))
            elif reclaim_stage == "confirm" or tp >= float(getattr(config, "BARRIER_RECLAIM_AMBUSH_MAX_TOKEN_PRICE", 0.45)):
                reclaim_exec_tier = "confirm"
                slippage_cap = max(slippage_cap, float(getattr(config, "BARRIER_RECLAIM_CONFIRM_EXECUTION_SLIPPAGE_CAP", 0.18)))
                max_weighted_avg_price = float(getattr(config, "BARRIER_RECLAIM_CONFIRM_MAX_WEIGHTED_AVG_PRICE", 0.90))
            else:
                reclaim_exec_tier = "standard_ambush"
                slippage_cap = max(slippage_cap, float(getattr(config, "BARRIER_RECLAIM_EXECUTION_SLIPPAGE_CAP", 0.22)))
                max_weighted_avg_price = float(getattr(config, "BARRIER_RECLAIM_MAX_WEIGHTED_AVG_PRICE", 0.45))
        if max_weighted_avg_price is not None and float(weighted_avg) > float(max_weighted_avg_price):
            log.info("⚠️  Skip: barrier reclaim weighted avg %.3f > max %.3f tier=%s", weighted_avg, max_weighted_avg_price, reclaim_exec_tier)
            return self._skip(signal, "barrier_reclaim_weighted_avg_too_high", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"weighted_avg": weighted_avg, "weighted_avg_source": "order_size", "max_weighted_avg_price": max_weighted_avg_price, "slippage_cap": slippage_cap, "tier": reclaim_exec_tier})
        if float(weighted_avg) > float(token_price) + float(slippage_cap):
            log.info("⚠️  Skip: weighted avg ask %.3f exceeds slippage cap %.3f", weighted_avg, slippage_cap)
            return self._skip(signal, "slippage_cap_exceeded", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"weighted_avg": weighted_avg, "weighted_avg_source": "order_size", "slippage_cap": slippage_cap, "tier": reclaim_exec_tier})

        estimated_shares = self.estimate_shares(expected_cost, weighted_avg)
        if estimated_shares <= 0:
            return self._skip(signal, "invalid_share_estimate", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"amount": expected_cost, "weighted_avg": weighted_avg})
        required_depth = estimated_shares * min_liquidity_multiplier
        if ob.get("ask_depth_to_cap", 0) < required_depth:
            log.info("⚠️  Skip: insufficient depth %.2f < %.2f", ob.get("ask_depth_to_cap", 0), required_depth)
            self.flight_recorder.record("execution_block", signal=signal, market=market, token_id=target_token, outcome=target_outcome, reason="insufficient_depth", details={"depth": ob.get("ask_depth_to_cap", 0), "required_depth": required_depth})
            return self._skip(signal, "insufficient_depth", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"depth": ob.get("ask_depth_to_cap", 0), "required_depth": required_depth})

        # V14.2: final public-orderbook recheck just before dry-run/live submission.
        # This catches stale WS books, price jumps, spread widening, and depth evaporation.
        exec_check = self.execution_guard.recheck_before_order(
            token_id=target_token,
            original_ob=ob,
            original_price=token_price,
            expected_cost=expected_cost,
            estimated_shares=estimated_shares,
            max_token_price=max_token_price,
            max_spread=max_spread,
            min_liquidity_multiplier=min_liquidity_multiplier,
            slippage_cap=slippage_cap,
            max_weighted_avg_price=max_weighted_avg_price,
            strategy_name=("barrier_reclaim" if is_barrier_reclaim else strategy_name_from(signal)),
        )
        if not exec_check.allowed:
            self.flight_recorder.record("execution_recheck", signal=signal, market=market, token_id=target_token, outcome=target_outcome, reason=exec_check.reason, details=exec_check.to_dict())
            log.info("⚠️  Skip: execution recheck failed reason=%s details=%s", exec_check.reason, exec_check.details)
            return self._skip(signal, exec_check.reason, market=market, token_price=token_price, edge_after_fees=edge_after_fees, details=exec_check.to_dict())

        # Use the freshest book for actual limit cap math. Recompute edge if the ask moved.
        if not exec_check.ob or exec_check.ob.get("best_ask") is None:
            log.warning("execution recheck passed without best_ask; refusing to submit")
            return self._skip(signal, "execution_recheck_no_best_ask", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details=exec_check.to_dict())
        ob = exec_check.ob
        token_price = float(ob.get("best_ask"))
        recheck_shares = self.estimate_shares(expected_cost, token_price)
        if recheck_shares <= 0:
            return self._skip(signal, "invalid_share_estimate", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"amount": expected_cost, "price": token_price, "stage": "execution_recheck"})
        recheck_avg = self._order_weighted_avg_from_ob(ob, recheck_shares, token_price + float(slippage_cap))
        recheck_ok, recheck_reason, recheck_details = self._order_avg_gate_status(recheck_avg, recheck_shares, best_ask=token_price)
        if not recheck_ok:
            details = exec_check.to_dict()
            details.update({"stage": "execution_recheck_weighted_avg", **recheck_details})
            self.flight_recorder.record("execution_recheck", signal=signal, market=market, token_id=target_token, outcome=target_outcome, reason=recheck_reason, details=details)
            return self._skip(signal, recheck_reason, market=market, token_price=token_price, edge_after_fees=edge_after_fees, details=details)
        weighted_avg = float(recheck_details["weighted_avg"])
        estimated_shares = self.estimate_shares(expected_cost, weighted_avg)
        if estimated_shares <= 0:
            return self._skip(signal, "invalid_share_estimate", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"amount": expected_cost, "weighted_avg": weighted_avg, "stage": "execution_recheck"})
        fee_per_share = config.taker_fee_per_share(token_price)
        effective_cost_prob = token_price + fee_per_share
        edge_after_fees = model_prob - effective_cost_prob
        if edge_after_fees < min_edge_after_fees:
            details = exec_check.to_dict()
            details.update({"model_prob": model_prob, "fee_per_share": fee_per_share, "min_edge": min_edge_after_fees})
            self.flight_recorder.record("execution_recheck", signal=signal, market=market, token_id=target_token, outcome=target_outcome, reason="execution_edge_evaporated", details=details)
            return self._skip(signal, "execution_edge_evaporated", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details=details)

        tick_decision = self.tick_guard.prepare_price(target_token, market, token_price + config.MARKET_BUY_SLIPPAGE)
        tick_price = getattr(tick_decision, "rounded_price", None)
        if tick_price is None:
            tick_price = getattr(tick_decision, "price", None)
        if tick_price is None:
            tick_price = token_price
        tick_price = float(tick_price)
        self.flight_recorder.record("execution_recheck", signal=signal, market=market, token_id=target_token, outcome=target_outcome, reason="execution_recheck_pass", details={"execution": exec_check.to_dict(), "tick": tick_decision.to_dict(), "tick_price": tick_price})

        log.info(
            "🎯 %s Execute BUY %s %.4f est shares @ %.3f cost=$%.2f model=%.1f%% edge=%.1f%% tick=%s(%s) ref=$%.2f dist=$%.2f",
            asset,
            target_outcome, estimated_shares, token_price, expected_cost, model_prob * 100,
            edge_after_fees * 100, tick_decision.tick_size, tick_decision.source, signal.reference_price, signal.distance_to_reference,
        )

        if config.DRY_RUN:
            log.warning("DRY_RUN=true: order not submitted")
            self.db.mark_order_attempt(signal.window_ts, market.get("slug", ""), status="dry_run", reason="dry_run_not_submitted", asset=asset, timeframe=timeframe)
            self._record_decision(signal, market=market, action="dry_run_signal", reason="dry_run_not_submitted", token_price=token_price, edge_after_fees=edge_after_fees, details={"estimated_shares": estimated_shares, "expected_cost": expected_cost})
            return True

        signal_payload = {
            "asset": asset,
            "timeframe": timeframe,
            "pattern": signal.pattern,
            "confidence": signal.confidence,
            "model_probability": model_prob,
            "edge_after_fees": edge_after_fees,
            "fee_per_share": fee_per_share,
            "market_mid_probability": market_mid,
            "model_market_gap": model_market_gap,
            "strategy_name": strategy_params.get("strategy_name", signal.pattern),
            "indicators": signal.indicators,
            "btc_price": signal.btc_price,
            "reference_price": signal.reference_price,
            "seconds_left": signal.seconds_left,
            "market_price_to_beat": market.get("price_to_beat"),
            "price_to_beat_source": market.get("price_to_beat_source") or price_to_beat_source,
            "resolved_price_key": resolved_price_key,
            "quality_gate": q_details if "q_details" in locals() else {},
            "high_win_rate_guard": hwr_details if "hwr_details" in locals() else {},
            "adaptive_edge": adaptive_details if "adaptive_details" in locals() else {},
        }
        # v15.1.0 Profit Rule Engine safety gate.
        # This point is reached only after DRY_RUN=false. Treat it as LIVE unless
        # the Polymarket client guard would force the order into Shadow. This
        # preserves Adaptive Edge learning while blocking unpromoted buckets from
        # real order submission.
        guard_reason = locals().get("adaptive_submit_guard_reason", "")
        if not guard_reason:
            try:
                guard_reason = self.polymarket.real_submit_guard_reason()
            except Exception as e:
                log.warning("real submit guard check failed: %s", e)
                guard_reason = "real_submit_guard_exception"
        profit_rule_mode = "shadow" if guard_reason else "live"

        if bool(getattr(config, "PROFIT_RULE_ENGINE_ENABLED", True)) and profit_rule_decision is not None:
            try:
                _pr_allowed, _pr_reason, _pr_info = profit_rule_decision(
                    self.db, signal_payload, asset, timeframe, target_outcome, weighted_avg, mode=profit_rule_mode
                )
                signal_payload["profit_rule"] = _pr_info
                signal_payload["profit_rule_reason"] = _pr_reason
                if not _pr_allowed:
                    log.info(
                        "🧠 Profit Rule blocked %s %s %s entry=%.4f prob=%.3f gap=%.3f mult=%.2f reason=%s rating=%s",
                        asset, timeframe, target_outcome,
                        float(_pr_info.get("entry_price") or 0), float(_pr_info.get("model_probability") or 0),
                        float(_pr_info.get("model_market_gap") or 0), float(_pr_info.get("net_profit_multiple") or 0),
                        _pr_reason, _pr_info.get("rule_rating") or _pr_info.get("decision"),
                    )
                    return self._skip(signal, _pr_reason, market=market, token_price=token_price,
                                      edge_after_fees=edge_after_fees, details=_pr_info)
                log.info(
                    "🧠 Profit Rule allowed %s %s %s entry=%.4f prob=%.3f gap=%.3f mult=%.2f reason=%s rating=%s",
                    asset, timeframe, target_outcome,
                    float(_pr_info.get("entry_price") or 0), float(_pr_info.get("model_probability") or 0),
                    float(_pr_info.get("model_market_gap") or 0), float(_pr_info.get("net_profit_multiple") or 0),
                    _pr_reason, _pr_info.get("rule_rating") or _pr_info.get("decision"),
                )
            except Exception as _pr_e:
                log.error("Profit Rule Engine check failed; refusing candidate for safety: %s", _pr_e)
                return self._skip(signal, "profit_rule_engine_error", market=market, token_price=token_price,
                                  edge_after_fees=edge_after_fees, details={"error": str(_pr_e)})

        if guard_reason:
            if config.SHADOW_TRADING_ENABLED and config.SHADOW_TRADING_ON_SIGNER_GUARD:
                self._insert_shadow_trade(
                    signal=signal, market=market, asset=asset, timeframe=timeframe,
                    target_token=target_token, target_outcome=target_outcome, token_price=token_price,
                    weighted_avg=weighted_avg, expected_cost=expected_cost, estimated_shares=estimated_shares,
                    model_prob=model_prob, edge_after_fees=edge_after_fees, fee_per_share=fee_per_share,
                    market_mid=market_mid, model_market_gap=model_market_gap, resolved_price_key=resolved_price_key,
                    price_to_beat_source=price_to_beat_source, q_details=q_details if "q_details" in locals() else {},
                    strategy_params=strategy_params, tick_price=tick_price, shadow_reason=guard_reason,
                )
                return True
            return self._skip(signal, guard_reason, market=market, token_price=token_price, edge_after_fees=edge_after_fees,
                              details={"guard_reason": guard_reason, "no_polymarket_post_order": True})

        now_attempt = int(time.time())
        trade_id = self.db.insert_trade({
            "window_ts": signal.window_ts,
            "asset": asset,
            "timeframe": timeframe,
            "market_slug": market["slug"],
            "market_question": market.get("question", ""),
            "token_id": target_token,
            "direction": target_outcome,
            "entry_price": token_price,
            "size": expected_cost,
            "shares": estimated_shares,
            "cost": expected_cost,
            "filled_shares": 0,
            "filled_cost": 0,
            "avg_price": 0,
            "order_id": "",
            "order_status": "attempted",
            "model_probability": model_prob,
            "edge_after_fees": edge_after_fees,
            "reference_price": signal.reference_price,
            "signal_data": signal_payload,
            "status": "attempted",
            "attempting_at": now_attempt,
        })

        # v15.1.4 hard safety: real_orders_enabled=false => local SHADOW only
        # FIX vs v15.1.3:
        #   - Set is_shadow=1 so get_shadow_open_trades() finds these rows
        #   - Set shadow_reason for review/learning
        #   - Set submitted_order_id so settlement and TG lifecycle work
        #   - Use getattr(signal,...) instead of signal.get(...) (ReversalSignal is a dataclass)
        #   - Use expected_cost directly (config.BET_SIZE does not exist)
        if not bool(getattr(config, "real_orders_enabled", False)):
            try:
                _shadow_order_id = f"shadow_local_{trade_id}_{int(time.time()*1000)}"
                import sqlite3 as _sqlite3
                with _sqlite3.connect(config.DB_PATH) as _conn:
                    _conn.execute(
                        """
                        UPDATE trades SET
                          status=?,
                          is_shadow=1,
                          shadow_reason=?,
                          submitted_at=?,
                          submitted_order_id=?,
                          filled_shares=?,
                          filled_cost=?,
                          avg_price=?
                        WHERE id=?
                        """,
                        (
                            "shadow_open",
                            "real_orders_disabled_shadow_mode",
                            now_attempt,
                            _shadow_order_id,
                            float(estimated_shares or 0.0),
                            float(expected_cost or 0.0),
                            float(token_price or 0.0),
                            trade_id,
                        ),
                    )
            except Exception as _e:
                log.warning("failed to mark local shadow trade open: %s", _e)

            try:
                _asset = str(asset or getattr(signal, "asset", "") or "")
                _timeframe = str(timeframe or getattr(signal, "timeframe", "") or "")
                _direction = str(target_outcome or getattr(signal, "direction", "") or "")
                _entry = float(token_price or 0.0)
                _size = float(expected_cost or 0.0)
                _shares = float(estimated_shares or 0.0)
                log.info(
                    "📝 SHADOW BUY %s %s %s @ %.4f shares=%.4f cost=$%.2f reason=real_orders_disabled_shadow_mode",
                    _asset, _timeframe, _direction, _entry, _shares, _size,
                )
            except Exception:
                log.info("📝 SHADOW BUY trade_id=%s reason=real_orders_disabled_shadow_mode", trade_id)

            # Record decision so TG / decision_logs see the shadow entry
            try:
                self._record_decision(
                    signal, market=market, action="shadow_submit",
                    reason="real_orders_disabled_shadow_mode",
                    token_price=token_price, edge_after_fees=edge_after_fees,
                    details={
                        "trade_id": trade_id,
                        "shadow_order_id": _shadow_order_id,
                        "shares": float(estimated_shares or 0.0),
                        "filled_cost": float(expected_cost or 0.0),
                        "avg_price": float(token_price or 0.0),
                        "no_polymarket_post_order": True,
                        "hard_safety_path": True,
                    },
                )
            except Exception:
                pass

            return True

        # v14.2.30: do not set the in-memory submit lock until the actual post_order call.
        # This avoids a stale in-flight lock if pre-submit logging/recording raises.
        self._record_decision(
            signal, market=market, action="order_attempt", reason="submit_started",
            token_price=token_price, edge_after_fees=edge_after_fees,
            details={
                "trade_id": trade_id,
                "estimated_shares": estimated_shares,
                "expected_cost": expected_cost,
                "limit_price": token_price,
                "tick_price": tick_price,
                "order_type": config.ORDER_TYPE,
                "note": "entered_polymarket_submit_path",
            },
        )
        self.flight_recorder.record(
            "order_submission", signal=signal, market=market, token_id=target_token,
            outcome=target_outcome, reason="submit_started", trade_id=trade_id, event_type="order_submit_start",
            message="开始调用 Polymarket post_order",
            details={"trade_id": trade_id, "amount": expected_cost, "price": token_price, "tick_price": tick_price, "shares": estimated_shares, "order_type": config.ORDER_TYPE, "timeout_sec": config.ORDER_SUBMIT_TIMEOUT_SEC},
        )

        submit_started = int(time.time())
        self.db.update_trade_lifecycle(trade_id, attempting_at=submit_started, last_status_check_at=submit_started)
        self._inflight_submit_keys.add(submit_key)
        log.info("🔒 post_order in-flight lock set market=%s key=%s trade_id=%s", market.get("slug", ""), submit_key, trade_id)
        try:
            order_response = await asyncio.wait_for(
                asyncio.to_thread(
                    self.polymarket.place_buy_order,
                    target_token,
                    token_price,
                    estimated_shares,
                    expected_cost,
                    config.ORDER_TYPE,
                    tick_decision.tick_size,
                    market.get("neg_risk", False),
                ),
                timeout=max(1, int(config.ORDER_SUBMIT_TIMEOUT_SEC or 8)),
            )
        except asyncio.TimeoutError:
            self._inflight_submit_keys.discard(submit_key)
            log.info("🔓 post_order in-flight lock cleared after timeout key=%s trade_id=%s", submit_key, trade_id)
            err = f"post_order_timeout_after_{int(config.ORDER_SUBMIT_TIMEOUT_SEC or 8)}s; order_id_unknown"
            log.error("Order placement timeout: %s", err)
            self.db.update_trade_lifecycle(
                trade_id, status="failed", submission_error=err, last_status_check_at=int(time.time())
            )
            self.db.mark_order_attempt(signal.window_ts, market.get("slug", ""), status="submit_timeout_unknown", reason="order_submit_timeout", asset=asset, timeframe=timeframe)
            self.flight_recorder.record(
                "order_submission", signal=signal, market=market, token_id=target_token, outcome=target_outcome,
                reason="order_submit_timeout", trade_id=trade_id, event_type="order_submit_timeout", error=err,
                message="提交订单超时，未拿到 Polymarket order_id；本窗口短暂冷却以避免重复单",
                details={"trade_id": trade_id, "error": err, "timeout_sec": config.ORDER_SUBMIT_TIMEOUT_SEC},
            )
            return self._skip(signal, "order_submit_timeout", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"trade_id": trade_id, "error": err})
        except Exception as e:
            self._inflight_submit_keys.discard(submit_key)
            log.info("🔓 post_order in-flight lock cleared after exception key=%s trade_id=%s", submit_key, trade_id)
            err = str(e)
            log.exception("Order placement exception: %s", e)
            self.db.update_trade_lifecycle(
                trade_id, status="failed", submission_error=err, last_status_check_at=int(time.time())
            )
            self.db.mark_order_attempt(signal.window_ts, market.get("slug", ""), status="failed", reason="order_placement_failed", asset=asset, timeframe=timeframe)
            self.flight_recorder.record("order_submission", signal=signal, market=market, token_id=target_token, outcome=target_outcome, reason="order_placement_failed", trade_id=trade_id, event_type="order_submit_exception", error=err, message="提交订单异常，未拿到 order_id", details={"trade_id": trade_id, "error": err})
            return self._skip(signal, "order_placement_failed", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"trade_id": trade_id, "error": err})

        if not order_response or not order_response.get("success"):
            self._inflight_submit_keys.discard(submit_key)
            log.info("🔓 post_order in-flight lock cleared after failed response key=%s trade_id=%s", submit_key, trade_id)
            log.error(f"Order placement failed: {order_response}")
            self.db.update_trade_lifecycle(
                trade_id,
                status="failed",
                submission_response_json=order_response or {},
                submission_error=str((order_response or {}).get("errorMsg") or (order_response or {}).get("error") or (order_response or {}).get("message") or "post_order returned success=false"),
                last_status_check_at=int(time.time()),
            )
            self.db.mark_order_attempt(signal.window_ts, market.get("slug", ""), status="failed", reason="order_placement_failed", asset=asset, timeframe=timeframe)
            self.flight_recorder.record("order_submission", signal=signal, market=market, token_id=target_token, outcome=target_outcome, reason="order_placement_failed", trade_id=trade_id, event_type="order_submit_failed", error=str((order_response or {}).get("errorMsg") or (order_response or {}).get("error") or (order_response or {}).get("message") or "post_order returned success=false"), message="提交订单失败，未被 Polymarket 接收", details={"trade_id": trade_id, "order_response": order_response})
            return self._skip(signal, "order_placement_failed", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"trade_id": trade_id, "order_response": order_response})

        status = str(order_response.get("status") or "").lower()
        order_id = order_response.get("orderID") or order_response.get("id") or ""
        filled_cost_resp = float(order_response.get("filled_cost") or 0.0)
        filled_shares_resp = float(order_response.get("filled_shares") or 0.0)
        if not order_id and not (status == "matched" and filled_cost_resp > 0 and filled_shares_resp > 0):
            self._inflight_submit_keys.discard(submit_key)
            log.info("🔓 post_order in-flight lock cleared after no-order-id response key=%s trade_id=%s", submit_key, trade_id)
            err = "post_order_success_without_order_id"
            log.error("Order placement returned success but no order_id: %s", order_response)
            self.db.update_trade_lifecycle(
                trade_id,
                status="failed",
                submission_response_json=order_response,
                submission_error=err,
                order_status=status or "no_order_id",
                last_status_check_at=int(time.time()),
            )
            self.db.mark_order_attempt(signal.window_ts, market.get("slug", ""), status="failed", reason=err, asset=asset, timeframe=timeframe)
            self.flight_recorder.record(
                "order_submission", signal=signal, market=market, token_id=target_token, outcome=target_outcome,
                reason=err, trade_id=trade_id, event_type="order_submit_no_order_id", error=err,
                message="Polymarket response had success=true but no order_id; treated as failed, not as submitted",
                details={"trade_id": trade_id, "order_response": order_response},
            )
            return self._skip(signal, "order_placement_failed", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"trade_id": trade_id, "error": err, "order_response": order_response})
        submitted_at = int(time.time())
        lifecycle_status = "submitted" if status not in {"matched", "filled", "unmatched", "failed", "cancelled"} else status
        self.db.update_trade_lifecycle(
            trade_id,
            status=lifecycle_status,
            submitted_at=submitted_at,
            submitted_order_id=order_id,
            submission_response_json=order_response,
            order_id=order_id,
            order_status=status or "submitted",
            last_status_check_at=submitted_at,
        )
        self.db.mark_order_attempt(signal.window_ts, market.get("slug", ""), order_id=order_id, status=status or "submitted", reason="post_order_response", asset=asset, timeframe=timeframe)
        self._inflight_submit_keys.discard(submit_key)
        log.info("🔓 post_order in-flight lock cleared after response key=%s trade_id=%s order_id=%s", submit_key, trade_id, order_id)
        self.flight_recorder.record("order_submission", signal=signal, market=market, token_id=target_token, outcome=target_outcome, reason="post_order_response", trade_id=trade_id, order_id=order_id, event_type="order_submit_response", message="Polymarket 已返回订单响应", details={"trade_id": trade_id, "order_id": order_id, "status": status, "order_response": order_response})

        # FOK can return delayed/unmatched in API statuses. Confirm once; do not create fake open trades.
        if status in {"delayed", "live", "unmatched"} and order_id:
            await asyncio.sleep(config.CONFIRM_DELAYED_ORDER_SEC)
            confirmed = await asyncio.to_thread(self.polymarket.confirm_order_fill, order_id, token_price, expected_cost)
            self.db.update_trade_lifecycle(trade_id, last_status_check_at=int(time.time()))
            if confirmed:
                order_response.update({k: v for k, v in confirmed.items() if v not in (None, "")})
                status = str(order_response.get("status") or status).lower()
                self.db.update_trade_lifecycle(trade_id, submission_response_json=order_response, order_status=status, last_status_check_at=int(time.time()))

        if status != "matched":
            log.warning("Order not matched; not recording as open trade: %s", order_response)
            cancel_attempt_count = 0
            if order_id and status in {"live", "delayed"}:
                cancel_attempt_count = 1
                await asyncio.to_thread(self.polymarket.cancel_order, order_id)
            final_status = "unmatched" if status in {"unmatched", "not_matched", "live", "delayed", ""} else status
            self.db.update_trade_lifecycle(
                trade_id,
                status=final_status or "unmatched",
                order_status=status or "not_matched",
                submission_response_json=order_response,
                cancel_attempt_count=cancel_attempt_count,
                last_status_check_at=int(time.time()),
            )
            self.db.mark_order_attempt(signal.window_ts, market.get("slug", ""), order_id=order_id, status=status or "not_matched", reason="order_not_matched", asset=asset, timeframe=timeframe)
            return self._skip(signal, "order_not_matched", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"trade_id": trade_id, "status": status, "order_id": order_id})

        self.db.update_trade_lifecycle(trade_id, status="matched", order_status=status, last_status_check_at=int(time.time()))

        # Confirm matched order once more if response lacks fill amounts.
        if order_id and (not order_response.get("filled_shares") or order_response.get("normalization_note") == "estimated_or_unfilled"):
            confirmed = await asyncio.to_thread(self.polymarket.confirm_order_fill, order_id, token_price, expected_cost)
            self.db.update_trade_lifecycle(trade_id, last_status_check_at=int(time.time()))
            if confirmed and confirmed.get("filled_shares", 0) > 0:
                order_response.update({k: v for k, v in confirmed.items() if v not in (None, "")})
                self.db.update_trade_lifecycle(trade_id, first_fill_response_json=confirmed, submission_response_json=order_response, last_status_check_at=int(time.time()))

        filled_shares = float(order_response.get("filled_shares") or 0)
        filled_cost = float(order_response.get("filled_cost") or 0)
        avg_price = float(order_response.get("avg_price") or token_price)
        if filled_shares <= 0 or filled_cost <= 0:
            log.warning("Order response had no confirmed fill; not recording: %s", order_response)
            self.db.update_trade_lifecycle(
                trade_id,
                status="unmatched",
                order_status=status or "unconfirmed",
                submission_response_json=order_response,
                submission_error="fill_unconfirmed",
                last_status_check_at=int(time.time()),
            )
            self.db.mark_order_attempt(signal.window_ts, market.get("slug", ""), order_id=order_id, status=status or "unconfirmed", reason="fill_unconfirmed", asset=asset, timeframe=timeframe)
            return self._skip(signal, "order_fill_unconfirmed", market=market, token_price=token_price, edge_after_fees=edge_after_fees, details={"trade_id": trade_id, "order_response": order_response})

        signal_payload["order_normalization_note"] = order_response.get("normalization_note", "")
        filled_at = int(time.time())
        self.db.update_trade_lifecycle(
            trade_id,
            status="filled",
            filled_at=filled_at,
            first_fill_response_json=order_response,
            submission_response_json=order_response,
            order_id=order_id,
            order_status=status,
            entry_price=avg_price,
            size=filled_cost,
            shares=filled_shares,
            cost=filled_cost,
            filled_shares=filled_shares,
            filled_cost=filled_cost,
            avg_price=avg_price,
            signal_data=signal_payload,
            last_status_check_at=filled_at,
        )

        self._record_decision(signal, market=market, action="order_matched", reason="trade_opened",
                              token_price=avg_price, edge_after_fees=edge_after_fees,
                              details={"trade_id": trade_id, "filled_shares": filled_shares, "filled_cost": filled_cost, "order_id": order_id})

        await self.notify("trade_opened", {
            "trade_id": trade_id,
            "asset": asset,
            "timeframe": timeframe,
            "pattern": signal.pattern,
            "direction": target_outcome,
            "btc_price": signal.btc_price,
            "reference_price": signal.reference_price,
            "distance": signal.distance_to_reference,
            "entry_price": avg_price,
            "shares": filled_shares,
            "cost": filled_cost,
            "max_payout": filled_shares,
            "balance": balance,
            "model_probability": model_prob,
            "edge_after_fees": edge_after_fees,
        })

        return True

    def _insert_shadow_trade(self, *, signal, market: dict, asset: str, timeframe: str,
                             target_token: str, target_outcome: str, token_price: float,
                             weighted_avg: float, expected_cost: float, estimated_shares: float,
                             model_prob: float, edge_after_fees: float, fee_per_share: float,
                             market_mid: float, model_market_gap: float, resolved_price_key: str,
                             price_to_beat_source: str, q_details: dict, strategy_params: dict,
                             tick_price: float, shadow_reason: str) -> int:
        """Record a shadow order after the exact same real quality/execution gates pass.

        No Polymarket submit is made here. This is used both for explicit shadow
        mode and for the CLOB V2 signature_type=3 signer guard, so the system
        keeps learning while avoiding repeated 400 signer/API-key failures.
        """
        if bool(getattr(config, "SHADOW_DEDUP_ENABLED", True)):
            try:
                existing = self.db.get_existing_shadow_trade(
                    asset=asset, timeframe=timeframe, window_ts=signal.window_ts,
                    direction=target_outcome, token_id=target_token,
                )
            except Exception as e:
                existing = None
                log.warning("shadow duplicate lookup failed: %s", e)
            if existing:
                existing_id = int(existing.get("id") or 0)
                existing_status = existing.get("status") or "shadow_open"
                existing_price = float(existing.get("entry_price") or 0.0)
                log.info(
                    "🟰 SHADOW duplicate skipped %s %s %s window=%s existing_id=%s status=%s entry=%.4f",
                    asset, timeframe, target_outcome, int(signal.window_ts or 0),
                    existing_id, existing_status, existing_price,
                )
                self._record_decision(
                    signal, market=market, action="skip", reason="shadow_duplicate_existing",
                    token_price=existing_price or token_price, edge_after_fees=edge_after_fees,
                    details={
                        "existing_trade_id": existing_id,
                        "existing_status": existing_status,
                        "asset": asset,
                        "timeframe": timeframe,
                        "window_ts": int(signal.window_ts or 0),
                        "direction": target_outcome,
                        "token_id": target_token,
                        "dedup_key": f"{asset}:{timeframe}:{int(signal.window_ts or 0)}:{target_outcome}:{target_token}",
                    },
                )
                return existing_id

        now = int(time.time())
        avg_price = float(weighted_avg or token_price or tick_price or 0.0)
        shares = float(estimated_shares or (expected_cost / avg_price if avg_price > 0 else 0.0))
        filled_cost = float(shares * avg_price) if shares > 0 and avg_price > 0 else float(expected_cost or 0.0)
        signal_payload = {
            "asset": asset,
            "timeframe": timeframe,
            "pattern": signal.pattern,
            "confidence": signal.confidence,
            "model_probability": model_prob,
            "edge_after_fees": edge_after_fees,
            "fee_per_share": fee_per_share,
            "market_mid_probability": market_mid,
            "model_market_gap": model_market_gap,
            "strategy_name": strategy_params.get("strategy_name", signal.pattern),
            "indicators": signal.indicators,
            "btc_price": signal.btc_price,
            "reference_price": signal.reference_price,
            "seconds_left": signal.seconds_left,
            "market_price_to_beat": market.get("price_to_beat"),
            "price_to_beat_source": market.get("price_to_beat_source") or price_to_beat_source,
            "resolved_price_key": resolved_price_key,
            "quality_gate": q_details or {},
            "is_shadow": True,
            "shadow_reason": shadow_reason,
            "tick_price": tick_price,
            "note": "shadow order recorded after full live quality/execution gates; no Polymarket post_order was sent",
        }
        shadow_order_id = f"shadow_{int(time.time()*1000)}_{asset}_{timeframe}_{target_outcome}".replace(" ", "_")
        trade_id = self.db.insert_trade({
            "timestamp": now,
            "window_ts": signal.window_ts,
            "asset": asset,
            "timeframe": timeframe,
            "market_slug": market.get("slug", ""),
            "market_question": market.get("question", ""),
            "token_id": target_token,
            "direction": target_outcome,
            "entry_price": avg_price,
            "size": filled_cost,
            "shares": shares,
            "cost": filled_cost,
            "filled_shares": shares,
            "filled_cost": filled_cost,
            "avg_price": avg_price,
            "order_id": "",
            "order_status": "shadow",
            "model_probability": model_prob,
            "edge_after_fees": edge_after_fees,
            "reference_price": signal.reference_price,
            "signal_data": signal_payload,
            "status": "shadow_open",
            "attempting_at": now,
            "submitted_at": now,
            "submitted_order_id": shadow_order_id,
            "is_shadow": 1,
            "shadow_reason": shadow_reason,
        })
        self.db.mark_order_attempt(signal.window_ts, market.get("slug", ""), order_id=shadow_order_id,
                                   status="shadow_open", reason=shadow_reason, asset=asset, timeframe=timeframe)
        self._record_decision(signal, market=market, action="shadow_submit", reason=shadow_reason,
                              token_price=avg_price, edge_after_fees=edge_after_fees,
                              details={"trade_id": trade_id, "shadow_order_id": shadow_order_id,
                                       "shares": shares, "filled_cost": filled_cost, "avg_price": avg_price,
                                       "no_polymarket_post_order": True})
        self.flight_recorder.record(
            "order_submission", signal=signal, market=market, token_id=target_token, outcome=target_outcome,
            reason=shadow_reason, trade_id=trade_id, event_type="shadow_submit",
            message="影子交易已记录：真实盘口/质量门通过，但未调用 Polymarket post_order",
            details={"trade_id": trade_id, "shadow_order_id": shadow_order_id, "shares": shares,
                     "cost": filled_cost, "avg_price": avg_price, "reason": shadow_reason},
        )
        log.info("📝 SHADOW BUY %s %s %s @ %.4f shares=%.4f cost=$%.2f reason=%s",
                 asset, timeframe, target_outcome, avg_price, shares, filled_cost, shadow_reason)
        return trade_id

    # ============ Settlement ============

    async def _fetch_binance_close_price_for_window(self, asset: str, window_close_ts: int) -> tuple[float | None, str]:
        """Return the 1m Binance close immediately before a Polymarket window close.

        The shadow path needs a deterministic local fallback when Gamma/CLOB has
        not yet exposed a resolved outcome.  For crypto Up/Down markets the
        reference rule is Chainlink, but Binance close is the best local
        approximation available to keep the learning loop from stalling.
        """
        asset = str(asset or "BTC").upper()
        # 1) Prefer the in-memory feed if the relevant closed kline is still there.
        try:
            feed = self._feed_for_asset(asset)
            target_start_ms = (int(window_close_ts) - 60) * 1000
            rows = list(getattr(feed, "klines", []) or [])
            candidates = [k for k in rows if int(getattr(k, "open_time", 0) or 0) <= target_start_ms]
            if candidates:
                k = candidates[-1]
                close = float(getattr(k, "close", 0.0) or 0.0)
                if close > 0:
                    return close, "shadow_local_feed_close"
        except Exception as e:
            log.debug("shadow local close fallback miss asset=%s close_ts=%s error=%s", asset, window_close_ts, e)

        # 2) REST fallback for older backlog rows outside the in-memory buffer.
        symbol = config.asset_symbol(asset)
        start_ms = (int(window_close_ts) - 60) * 1000
        try:
            async with client_session() as sess:
                params = {
                    "symbol": symbol,
                    "interval": "1m",
                    "startTime": int(start_ms),
                    "limit": 1,
                }
                async with sess.get("https://api.binance.com/api/v3/klines", params=params, timeout=8) as resp:
                    if resp.status != 200:
                        log.warning("shadow Binance close REST failed status=%s asset=%s symbol=%s", resp.status, asset, symbol)
                        return None, ""
                    data = await resp.json()
                    if not data:
                        return None, ""
                    close = float(data[0][4])
                    return (close, "shadow_binance_rest_close") if close > 0 else (None, "")
        except Exception as e:
            log.warning("shadow Binance close REST error asset=%s symbol=%s close_ts=%s error=%s", asset, symbol, window_close_ts, e)
            return None, ""

    async def _resolve_shadow_outcome(self, t: dict, now: int) -> tuple[str | None, float, str]:
        """Resolve one shadow row using Gamma/CLOB first, then price fallback.

        Returns (outcome, final_price, settlement_source). ``final_price`` is a
        token final price (0/1) when available, or the asset close price for the
        Binance fallback.
        """
        market = None
        if t.get("market_slug"):
            market = await self.polymarket.find_market_by_slug(t["market_slug"])
        if not market:
            market = await self.polymarket.find_updown_market(
                t.get("asset", "BTC"), t["window_ts"], t.get("timeframe", "5m"), allow_nearby=False
            )

        if market:
            if market.get("resolved_outcome") in ("Up", "Down"):
                outcome = "win" if market["resolved_outcome"].lower() == str(t.get("direction", "")).lower() else "loss"
                final_price = 1.0 if outcome == "win" else 0.0
                return outcome, final_price, "shadow_gamma_resolved_outcome"

            final_price = market.get("up_outcome_price") if str(t.get("direction", "")).lower() == "up" else market.get("down_outcome_price")
            if final_price is not None:
                try:
                    final_price = float(final_price)
                    if final_price >= 0.99:
                        return "win", final_price, "shadow_gamma_outcome_prices"
                    if final_price <= 0.01:
                        return "loss", final_price, "shadow_gamma_outcome_prices"
                except Exception:
                    pass

        # Fallback only after the configured grace period.  This prevents early
        # mis-settlement but guarantees old shadow rows do not remain open forever.
        window_close = int(t["window_ts"]) + config.timeframe_seconds(t.get("timeframe", "5m"))
        fallback_after = max(int(getattr(config, "SHADOW_SETTLEMENT_BUFFER_SEC", 10) or 10),
                             int(getattr(config, "SHADOW_SETTLEMENT_FALLBACK_AFTER_SEC", 120) or 120))
        if now < window_close + fallback_after:
            return None, 0.0, ""

        ref = None
        try:
            ref = float(t.get("reference_price") or 0.0)
        except Exception:
            ref = 0.0
        if not ref or ref <= 0:
            signal_data = self._parse_signal_data(t.get("signal_data"))
            for key in ("reference_price", "market_price_to_beat"):
                try:
                    ref = float(signal_data.get(key) or 0.0)
                except Exception:
                    ref = 0.0
                if ref and ref > 0:
                    break
        if not ref or ref <= 0:
            return None, 0.0, ""

        close_price, source = await self._fetch_binance_close_price_for_window(t.get("asset", "BTC"), window_close)
        if close_price is None or close_price <= 0:
            return None, 0.0, ""

        is_up = close_price > ref
        direction = str(t.get("direction") or "").lower()
        won = (direction == "up" and is_up) or (direction == "down" and not is_up)
        return ("win" if won else "loss"), float(close_price), source or "shadow_binance_close_fallback"

    async def _settle_one_shadow_trade(self, t: dict, now: int) -> bool:
        outcome, final_price, settlement_source = await self._resolve_shadow_outcome(t, now)
        if outcome is None:
            return False

        filled_shares = float(t.get("filled_shares") or t.get("shares") or 0.0)
        filled_cost = float(t.get("filled_cost") or t.get("cost") or t.get("size") or 0.0)
        if outcome == "win":
            payout = filled_shares
            pnl = payout - filled_cost
        else:
            payout = 0.0
            pnl = -filled_cost

        self.db.resolve_trade(
            t["id"], outcome, payout, pnl, settlement_source=settlement_source,
            final_price=final_price or 0.0, status="shadow_resolved"
        )
        signal_data = self._parse_signal_data(t.get("signal_data"))
        if signal_data:
            sig_type = signal_data.get("strategy_name") or signal_data.get("pattern", "unknown")
            self.db.record_signal_outcome(f"shadow:{sig_type}", outcome == "win", pnl)
            self.db.record_calibration_bucket(float(t.get("model_probability") or 0), t.get("direction", ""), outcome == "win", pnl)
        review = ""
        if self.learner:
            resolved_trade = self.db.get_trade_by_id(t["id"])
            review = self.learner.review_trade(resolved_trade) if resolved_trade else ""
        # v14.2.42 Profit Rule Engine update after settlement
        if profit_rule_record_trade is not None:
            try:
                _resolved_for_rule = self.db.get_trade_by_id(t["id"]) or t
                profit_rule_record_trade(self.db, _resolved_for_rule)
            except Exception as _pr_e:
                log.warning("Profit Rule Engine update failed for trade %s: %s", t.get("id"), _pr_e)
        log.info(
            "📊 SHADOW SETTLED %s %s %s pnl=$%+.2f entry=%.4f shares=%.4f source=%s final=%.4f",
            t.get("asset", "BTC"), t.get("direction", ""), outcome.upper(), pnl,
            float(t.get("entry_price") or 0), filled_shares, settlement_source, float(final_price or 0.0)
        )
        await self.notify("trade_resolved", {
            "trade_id": t["id"], "asset": t.get("asset", "BTC"), "outcome": outcome,
            "pnl": pnl, "payout": payout, "cost": filled_cost, "balance": await self._get_balance(),
            "review": "🎭 SHADOW " + (review or "影子交易已结算"), "settlement_source": settlement_source,
        })
        return True

    async def settle_resolved_trades(self):
        open_trades = self.db.get_open_trades()
        now = int(time.time())

        # v14.2.38: Shadow settlement catch-up.  Older builds left many
        # shadow_open rows unresolved when Gamma had not exposed final outcome
        # yet.  We now process due rows each cycle and fall back to Binance close
        # after a safe grace period so the learning loop completes.
        max_shadow = max(1, int(getattr(config, "SHADOW_SETTLEMENT_MAX_PER_CYCLE", 25) or 25))
        settled_shadow = 0
        for t in self.db.get_shadow_open_trades():
            window_close = int(t["window_ts"]) + config.timeframe_seconds(t.get("timeframe", "5m"))
            if now < window_close + int(getattr(config, "SHADOW_SETTLEMENT_BUFFER_SEC", 10) or 10):
                continue
            if settled_shadow >= max_shadow:
                break
            try:
                if await self._settle_one_shadow_trade(t, now):
                    settled_shadow += 1
            except Exception as e:
                log.error("Shadow settlement error for trade %s: %s", t.get("id"), e, exc_info=True)

        for t in open_trades:
            window_close = t["window_ts"] + config.timeframe_seconds(t.get("timeframe", "5m"))
            if now < window_close + 45:
                continue

            try:
                market = None
                if t.get("market_slug"):
                    market = await self.polymarket.find_market_by_slug(t["market_slug"])
                if not market:
                    market = await self.polymarket.find_updown_market(t.get("asset", "BTC"), t["window_ts"], t.get("timeframe", "5m"), allow_nearby=False)
                if not market:
                    continue

                outcome = None
                final_price = 0.0
                if market.get("resolved_outcome") in ("Up", "Down"):
                    outcome = "win" if market["resolved_outcome"].lower() == t["direction"].lower() else "loss"
                    settlement_source = "gamma_resolved_outcome"
                    final_price = 1.0 if outcome == "win" else 0.0
                else:
                    settlement_source = "gamma_outcome_prices"
                    final_price = market.get("up_outcome_price") if t["direction"].lower() == "up" else market.get("down_outcome_price")
                if outcome is None and final_price is not None:
                    final_price = float(final_price)
                    if final_price >= 0.99:
                        outcome = "win"
                    elif final_price <= 0.01:
                        outcome = "loss"

                if outcome is None:
                    settlement_source = "clob_orderbook_fallback"
                    token_final_price = self.polymarket.get_token_price(t["token_id"])
                    if token_final_price is None:
                        continue
                    final_price = float(token_final_price)
                    if final_price >= 0.99:
                        outcome = "win"
                    elif final_price <= 0.01:
                        outcome = "loss"
                    else:
                        continue

                if outcome == "win":
                    payout = float(t.get("filled_shares") or t["shares"])
                    pnl = payout - float(t.get("filled_cost") or t["cost"])
                else:
                    payout = 0.0
                    pnl = -float(t.get("filled_cost") or t["cost"])

                self.db.resolve_trade(t["id"], outcome, payout, pnl, settlement_source=settlement_source, final_price=final_price or 0.0)

                signal_data = self._parse_signal_data(t["signal_data"])
                review = ""
                if signal_data:
                    sig_type = signal_data.get("pattern", "unknown")
                    self.db.record_signal_outcome(sig_type, outcome == "win", pnl)
                    self.db.record_calibration_bucket(float(t.get("model_probability") or 0), t.get("direction", ""), outcome == "win", pnl)

                if self.learner:
                    resolved_trade = self.db.get_trade_by_id(t["id"])
                    review = self.learner.review_trade(resolved_trade) if resolved_trade else ""

                balance = await self._get_balance()
                await self.notify("trade_resolved", {
                    "trade_id": t["id"],
                    "asset": t.get("asset", "BTC"),
                    "outcome": outcome,
                    "pnl": pnl,
                    "payout": payout,
                    "cost": float(t.get("filled_cost") or t["cost"]),
                    "balance": balance,
                    "review": review,
                    "settlement_source": settlement_source,
                })

                log.info("💰 Trade #%s %s: PnL $%+.2f, balance $%.2f", t["id"], outcome.upper(), pnl, balance)

            except Exception as e:
                log.error(f"Settlement error for trade {t['id']}: {e}")

    @staticmethod
    def _parse_signal_data(raw):
        import json
        try:
            return json.loads(raw) if isinstance(raw, str) else raw
        except Exception as e:
            log.warning("signal_data JSON parse failed; skipping malformed payload: %s", e)
            return None

    async def _get_balance(self):
        if not config.real_orders_enabled:
            # Dry-run/paper mode should not require wallet auth; use configured bankroll as virtual balance.
            return float(config.starting_bankroll)
        try:
            return await asyncio.to_thread(self.polymarket.get_balance)
        except Exception as e:
            log.error("Balance unavailable: %s", e)
            return None

    # ============ Main loop ============

    async def run(self):
        self.running = True
        log.info("Trader running...")
        last_settle = 0

        while self.running:
            try:
                signals = []
                now_i = int(time.time())
                for key, strategy in self.strategies.items():
                    interval_sec = getattr(strategy, "market_interval_sec", config.MARKET_INTERVAL_SEC)
                    window_ts = (now_i // interval_sec) * interval_sec
                    asset = getattr(strategy, "asset", str(key).split(":")[0])
                    if hasattr(strategy, "generate_signals"):
                        asset_signals = strategy.generate_signals()
                    else:
                        one = strategy.evaluate()
                        asset_signals = [one] if one else []
                    if not asset_signals:
                        self._record_observation(asset, strategy, window_ts)
                    for sig in asset_signals:
                        if sig:
                            sig.asset = asset
                            sig.timeframe = getattr(strategy, "timeframe", getattr(sig, "timeframe", "5m"))
                            sig.market_interval_sec = getattr(strategy, "market_interval_sec", getattr(sig, "market_interval_sec", 300))
                            sig.adjusted_score = float(getattr(sig, "adjusted_score", 0) or sig.target_probability or sig.confidence or 0)
                            signals.append(sig)
                if signals:
                    signals.sort(key=lambda s: -float(getattr(s, "adjusted_score", 0) or 0))
                    log.info("📡 Evaluating %s; triggered=%s", ", ".join(self.strategies.keys()), ", ".join([s.asset for s in signals]))
                    executed = 0
                    candidate_limit = max(int(config.MAX_TRADES_PER_WINDOW), int(config.MAX_SIGNAL_CANDIDATES_PER_LOOP))
                    for signal in signals[:candidate_limit]:
                        ok = await self.try_execute_signal(signal)
                        if ok:
                            executed += 1
                        if executed >= max(1, int(config.MAX_TRADES_PER_WINDOW)):
                            break

                if time.time() - last_settle > 20:
                    await self.settle_resolved_trades()
                    last_settle = time.time()

                await asyncio.sleep(config.MAIN_LOOP_INTERVAL_SEC)

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception(f"Trader loop error: {e}")
                await asyncio.sleep(10)

    def stop(self):
        self.running = False
