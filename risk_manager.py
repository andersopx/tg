"""
Risk Manager - safety layer before any trade.
"""
import logging
import time
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Tuple

from config import config
from database import Database
from binance_feed import BinanceFeed

log = logging.getLogger(__name__)


@dataclass
class RiskCheck:
    can_trade: bool
    reason: str
    severity: str = "info"  # "info", "warning", "critical"


@dataclass
class CapitalScope:
    enabled: bool
    cap: float
    reserve_balance: float
    current_balance: float
    trading_equity: float


class RiskManager:
    def __init__(self, db: Database, feed: BinanceFeed):
        self.db = db
        self.feed = feed
        self.cooldown_until: int = 0
        self.is_halted: bool = False
        self.halt_reason: str = ""
        # Do not let stale DB halt/notify state survive a restart forever.
        # The next can_trade() call will re-evaluate current balance/risk
        # against the current .env/TG settings and re-halt if still needed.
        try:
            db_halted = db.get_state("halted", False)
            if str(db_halted).lower() == "true":
                log.info("启动时检测到数据库 halted=true，清理 stale halt 状态并重新评估")
                db.set_state("halted", False)
                db.set_state("halt_reason", "")
                db.set_state("last_halt_notify", "")
        except Exception as e:
            log.warning("Failed to load/clear halt state from db: %s", e)

    def capital_scope(self, balance: Optional[float]) -> CapitalScope:
        """Return the principal-isolated trading pool.

        If the user authorizes $50 out of a $100 wallet, the first real balance
        read anchors a $50 reserve. The bot may trade only balance above that
        reserve. Profits increase trading_equity; there is no profit cap.
        """
        b = float(balance or 0.0)
        cap = float(config.effective_authorized_capital_usd or 0.0)
        if cap <= 0:
            return CapitalScope(False, 0.0, 0.0, b, max(0.0, b))

        # v14.2.31: small-balance safety. If the wallet is materially below
        # the configured authorized capital, do not create a negative reserve
        # or tiny per-asset pools. Treat capital isolation as disabled and log
        # a clear warning. Example: wallet $40, cap $50.
        if b < cap * 0.95:
            log.warning("钱包 $%.2f < 授权本金 $%.2f，自动关闭资本隔离。本次可交易余额按 $%.2f 计算。", b, cap, b)
            return CapitalScope(False, 0.0, 0.0, b, max(0.0, b))

        state_cap = float(self.db.get_state("capital_isolation_capital_limit_usd", 0.0) or 0.0)
        reserve_state = self.db.get_state("capital_isolation_reserved_balance", None)

        # First setup or explicit TG/env cap change: re-anchor at the current
        # balance. This matches the user changing the authorized capital pool.
        if reserve_state is None or abs(state_cap - cap) > 1e-9:
            reserve = max(0.0, b - cap)
            self.db.set_state("capital_isolation_initial_balance", b)
            self.db.set_state("capital_isolation_reserved_balance", reserve)
            self.db.set_state("capital_isolation_capital_limit_usd", cap)
        else:
            reserve = max(0.0, float(reserve_state or 0.0))

        equity = max(0.0, b - reserve)
        return CapitalScope(True, cap, reserve, b, equity)

    def available_trading_equity(self, balance: Optional[float]) -> float:
        return self.capital_scope(balance).trading_equity

    def capital_status_summary(self, balance: Optional[float]) -> dict:
        scope = self.capital_scope(balance)
        return {
            "enabled": scope.enabled,
            "authorized_capital": scope.cap,
            "reserve_balance": scope.reserve_balance,
            "current_balance": scope.current_balance,
            "trading_equity": scope.trading_equity,
        }

    def can_trade(self, balance: Optional[float]) -> RiskCheck:
        if self.is_halted:
            return RiskCheck(False, f"Bot halted: {self.halt_reason}", "critical")

        if balance is None:
            # Distinguish unavailable balance from true zero balance. Safer to skip, not permanently halt.
            return RiskCheck(False, "Balance unavailable; skipping without halt", "warning")

        scope = self.capital_scope(balance)
        if scope.enabled:
            # Capital isolation replaces the legacy whole-wallet stop line.
            # The protected reserve is the stop boundary; wallet-level PnL is
            # not used because deposits/profits outside the authorized pool must
            # not distort risk control.
            if scope.trading_equity <= 0.01:
                self.halt(
                    f"Authorized trading capital exhausted: balance ${balance:.2f} reached protected reserve ${scope.reserve_balance:.2f}"
                )
                return RiskCheck(
                    False,
                    f"Authorized capital exhausted: trading equity ${scope.trading_equity:.2f}; protected reserve ${scope.reserve_balance:.2f}",
                    "critical",
                )
        elif balance <= config.stop_loss_balance:
            self.halt(f"Balance ${balance:.2f} hit stop-loss ${config.stop_loss_balance:.2f}")
            return RiskCheck(False, f"Balance ${balance:.2f} ≤ stop-loss ${config.stop_loss_balance:.2f}", "critical")

        if self.db.count_open_trades() >= config.effective_max_open_trades:
            return RiskCheck(False, f"Open-trade limit reached ({config.effective_max_open_trades})", "warning")

        max_trades = int(config.effective_max_trades_per_day or 0)
        if max_trades > 0 and self.db.get_today_trade_count() >= max_trades:
            return RiskCheck(False, f"Daily trade limit reached ({max_trades})", "warning")

        max_attempts = int(config.effective_max_order_attempts_per_day or 0)
        attempts_today = self.db.get_today_order_attempt_count()
        if max_attempts > 0 and attempts_today >= max_attempts:
            return RiskCheck(False, f"Daily order-attempt limit reached ({max_attempts})", "warning")

        today_pnl = self.db.get_today_realized_pnl()
        loss_caps = []
        if float(config.effective_max_daily_loss_usd or 0) > 0:
            loss_caps.append(float(config.effective_max_daily_loss_usd))
        if float(config.effective_max_daily_loss_pct or 0) > 0:
            loss_caps.append(config.starting_bankroll * float(config.effective_max_daily_loss_pct))
        daily_loss_cap = min(loss_caps) if loss_caps else 0.0
        if daily_loss_cap > 0 and today_pnl <= -daily_loss_cap:
            self.halt(f"Daily realized loss ${today_pnl:.2f} hit cap -${daily_loss_cap:.2f}")
            return RiskCheck(False, f"Daily loss cap hit: ${today_pnl:.2f} ≤ -${daily_loss_cap:.2f}", "critical")

        # v15: lock in a good day instead of giving profits back in low-quality
        # late-day trades. High-water is stored by UTC day.
        if bool(getattr(config, "DAILY_PROFIT_LOCK_ENABLED", True)):
            try:
                today_key = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                hw_key = f"daily_profit_highwater_{today_key}"
                highwater = float(self.db.get_state(hw_key, 0.0) or 0.0)
                if today_pnl > highwater:
                    highwater = float(today_pnl)
                    self.db.set_state(hw_key, highwater)
                start_usd = float(getattr(config, "DAILY_PROFIT_LOCK_START_USD", 3.0) or 0.0)
                giveback_usd = float(getattr(config, "DAILY_PROFIT_LOCK_GIVEBACK_USD", 1.25) or 0.0)
                giveback_pct = float(getattr(config, "DAILY_PROFIT_LOCK_GIVEBACK_PCT", 0.45) or 0.0)
                giveback_limit = max(giveback_usd, highwater * giveback_pct)
                if highwater >= start_usd and (highwater - today_pnl) >= giveback_limit:
                    self.halt(f"Daily profit lock: high-water ${highwater:.2f}, current ${today_pnl:.2f}, giveback ${highwater - today_pnl:.2f}")
                    return RiskCheck(False, f"Daily profit lock: protected ${highwater:.2f} high-water after giveback", "critical")
            except Exception as e:
                log.warning("Daily profit lock check failed: %s", e)

        # v15: intraday win-rate governor. It pauses new trades when live
        # outcomes are too weak, so the bot stops digging during a bad regime.
        if bool(getattr(config, "WINRATE_GOVERNOR_ENABLED", True)):
            try:
                stats = self.db.get_today_outcome_stats(include_shadow=bool(getattr(config, "WINRATE_GOVERNOR_INCLUDE_SHADOW", False)))
                attempts = int(stats.get("attempts") or 0)
                min_attempts = int(getattr(config, "WINRATE_GOVERNOR_MIN_TRADES", 8) or 0)
                win_rate = float(stats.get("win_rate") or 0.0)
                min_wr = float(getattr(config, "WINRATE_GOVERNOR_MIN_WIN_RATE", 0.55) or 0.0)
                if attempts >= min_attempts and win_rate < min_wr:
                    self.cooldown_until = max(self.cooldown_until, int(time.time()) + int(config.COOLDOWN_AFTER_LOSSES_SEC))
                    return RiskCheck(False, f"Win-rate governor: today {win_rate:.0%} over {attempts} resolved trades < {min_wr:.0%}", "warning")
            except Exception as e:
                log.warning("Win-rate governor check failed: %s", e)

        if time.time() < self.cooldown_until:
            remaining = int(self.cooldown_until - time.time())
            return RiskCheck(False, f"Cooldown active, {remaining}s remaining", "warning")

        consecutive = self.db.get_consecutive_losses()
        if consecutive >= config.CONSECUTIVE_LOSS_LIMIT:
            self.cooldown_until = int(time.time()) + config.COOLDOWN_AFTER_LOSSES_SEC
            return RiskCheck(False, f"{consecutive} consecutive losses → cooldown", "warning")

        change_5m = abs(self.feed.get_price_change_pct(5))
        if change_5m > config.BLACK_SWAN_THRESHOLD:
            return RiskCheck(False, f"Black swan: BTC ±{change_5m*100:.2f}% in 5min", "warning")

        return RiskCheck(True, "All checks passed", "info")


    def can_trade_asset(self, asset: str, balance: Optional[float], bet_size: float = 0.0, feed: Optional[BinanceFeed] = None) -> RiskCheck:
        """Per-asset risk check layered over global safety checks."""
        asset = str(asset or "BTC").upper()
        base = self.can_trade(balance)
        if not base.can_trade:
            return base

        if feed is None:
            feed = self.feed
        try:
            change_5m = abs(feed.get_price_change_pct(5))
            threshold = config.asset_black_swan_threshold(asset)
            if change_5m > threshold:
                return RiskCheck(False, f"Black swan: {asset} ±{change_5m*100:.2f}% in 5min", "warning")
        except Exception as e:
            log.warning("Black swan check failed for %s: %s. Skipping check.", asset, e)

        allocation_pct = config.asset_bankroll_allocation(asset)
        if allocation_pct > 0 and balance is not None and bet_size > 0:
            base_capital = self.available_trading_equity(balance) if config.capital_isolation_enabled else float(balance)
            asset_max_capital = float(base_capital) * allocation_pct
            asset_used = self.db.get_asset_balance_used(asset)
            if asset_used + bet_size > asset_max_capital + 1e-9:
                return RiskCheck(False, f"{asset} allocation used: ${asset_used:.2f}/${asset_max_capital:.2f}", "warning")

        return RiskCheck(True, "OK", "info")

    def calc_max_bet(self, balance: float) -> float:
        available = self.available_trading_equity(balance) if config.capital_isolation_enabled else float(balance)
        if config.MODE in {"paper", "small_live"}:
            return min(config.effective_per_order_amount, available)
        cap = available * config.effective_live_max_bet_ratio
        return max(0.0, min(config.effective_live_max_bet, cap, available))

    def validate_order(self, bet_size: float, balance: float) -> Tuple[bool, str]:
        if bet_size <= 0:
            return False, "Bet size is zero"
        if bet_size > balance:
            return False, f"Bet ${bet_size:.2f} exceeds balance ${balance:.2f}"

        if config.capital_isolation_enabled:
            scope = self.capital_scope(balance)
            if bet_size > scope.trading_equity + 0.01:
                return False, (
                    f"Bet ${bet_size:.2f} exceeds authorized trading equity "
                    f"${scope.trading_equity:.2f}; protected reserve ${scope.reserve_balance:.2f}"
                )

        max_allowed = self.calc_max_bet(balance)
        if bet_size > max_allowed + 0.01:
            return False, f"Bet ${bet_size:.2f} exceeds max ${max_allowed:.2f}"

        # small_live uses the legacy TEST_BET_SIZE cap; allow a small rounding buffer.
        if config.MODE in {"paper", "small_live"} and bet_size > config.effective_test_bet_size + 0.05:
            return False, f"Small-live cap is ${config.effective_test_bet_size:.2f}"

        return True, "OK"

    def halt(self, reason: str):
        self.is_halted = True
        self.halt_reason = reason
        self.db.set_state("halted", True)
        self.db.set_state("halt_reason", reason)
        log.critical(f"🛑 BOT HALTED: {reason}")

    def resume(self):
        self.is_halted = False
        self.halt_reason = ""
        self.cooldown_until = 0
        self.db.set_state("halted", False)
        self.db.set_state("halt_reason", "")
        log.info("✅ Bot resumed")

    def status_summary(self) -> dict:
        return {
            "halted": self.is_halted,
            "halt_reason": self.halt_reason,
            "cooldown_remaining": max(0, int(self.cooldown_until - time.time())),
            "consecutive_losses": self.db.get_consecutive_losses(),
            "open_trades": self.db.count_open_trades(),
            "today_trades": self.db.get_today_trade_count(),
            "today_attempts": self.db.get_today_order_attempt_count(),
            "today_pnl": self.db.get_today_realized_pnl(),
        }
