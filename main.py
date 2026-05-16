"""
Main entry point.

Boots all components in order:
  1. Database (SQLite)
  2. Binance feed (WebSocket BTC prices)
  3. Polymarket client (API connection + auth)
  4. Risk manager
  5. Strategy engine (Pin Bar detector)
  6. Learner (post-trade review)
  7. Trader (orchestrates 1-6)
  8. Telegram bot (UI + notifications)

Then runs event loop until SIGINT.
"""
import asyncio
import logging
import signal
import sys
from datetime import datetime, timezone

from config import config
from database import Database
from binance_feed import BinanceFeed, MultiAssetBinanceFeed
from polymarket_client import PolymarketClient
from risk_manager import RiskManager
from strategy import PinBarStrategy
from learner import Learner
from trader import Trader
try:
    from telegram_bot import TelegramBot
except Exception:
    TelegramBot = None
from data_collector import DataCollector
from realtime_orderbook import RealtimeOrderbookCache
from polymarket_ws import PolymarketMarketWebSocket
from product_doctor import ProductDoctor


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("main")


def validate_config():
    """Fail fast only for the credentials actually needed by the selected mode."""
    errors = []
    warnings = []

    if config.OBSERVER_ONLY and not config.DRY_RUN:
        errors.append("OBSERVER_ONLY=true requires DRY_RUN=true")

    if config.real_orders_enabled:
        # Live-only Telegram builds may start before the user enters Polymarket keys
        # in Telegram.  Do not fail startup; the trader will skip until keys are
        # saved and "重新连接认证" succeeds.
        if not config.has_polymarket_creds:
            warnings.append("Real-order mode selected but Polymarket keys are not set yet; Telegram key setup required before trading")
        if not config.ENABLE_TELEGRAM:
            warnings.append("ENABLE_TELEGRAM=false while real orders are enabled; allowed, but not recommended")

    if config.ENABLE_TELEGRAM:
        if not config.has_telegram_creds:
            errors.append("TG_BOT_TOKEN/TG_USER_ID missing or still placeholder while ENABLE_TELEGRAM=true")

    if str(config.MODE_RAW).strip().lower() == "test":
        warnings.append("Legacy MODE=test is treated as MODE=small_live. Use MODE=paper for no-order simulation.")

    for w in warnings:
        log.warning(w)

    if errors:
        log.error("Configuration errors:")
        for e in errors:
            log.error(f"  - {e}")
        log.error("Edit .env file (see .env.example for template)")
        sys.exit(1)


class NoopNotifier:
    async def push(self, event: str, payload: dict):
        return None
    async def start(self):
        return None
    async def stop(self):
        return None


async def startup_risk_health_check(db: Database, polymarket: PolymarketClient, risk: RiskManager):
    """Warn about small-balance settings and clear stale halted state when safe."""
    balance = None
    try:
        if polymarket and polymarket.connected:
            balance = await asyncio.to_thread(polymarket.get_balance)
    except Exception as e:
        log.warning("启动健康检查: 余额读取失败，跳过资本配置检查: %s", e)

    if balance is not None:
        try:
            b = float(balance)
            allocs = [float(config.asset_bankroll_allocation(a) or 0.0) for a in config.assets_enabled]
            positive_allocs = [x for x in allocs if x > 0]
            min_alloc = b * min(positive_allocs) if positive_allocs else b
            per_order = float(config.effective_per_order_amount or 0.0)
            log.info("启动资金检查: 钱包=$%.2f 单笔=$%.2f 最小币种分配=$%.2f 资本隔离=%s 授权=$%.2f",
                     b, per_order, min_alloc, config.capital_isolation_enabled, config.effective_authorized_capital_usd)
            if min_alloc < per_order:
                log.warning("⚠️ 配置告警: 钱包 $%.2f, 最小币种分配 $%.2f, 单笔 $%.2f -> 可能任何币种都不够下 1 笔", b, min_alloc, per_order)
                log.warning("建议小资金: AUTHORIZED_CAPITAL_USD=0 且 BTC/ETH/SOL/XRP_BANKROLL_ALLOCATION=1.0")
        except Exception as e:
            log.warning("启动资金检查失败: %s", e)

    try:
        stored_halted = bool(db.get_state("halted", False))
        if stored_halted and balance is not None:
            stop = float(config.stop_loss_balance or 0.0)
            if float(balance) > stop:
                log.info("启动检测: 余额 $%.2f > 止损线 $%.2f，自动 resume 并清理 halted 状态", float(balance), stop)
                db.set_state("halted", False)
                db.set_state("halt_reason", "")
                db.set_state("last_halt_notify", "")
                if risk:
                    risk.resume()
    except Exception as e:
        log.warning("启动 stale halted 检查失败: %s", e)


async def run():
    # ============ Initialize components ============

    db = Database(config.DB_PATH)
    # Runtime TG/SQLite settings must override .env defaults for sizing/risk controls and credentials.
    config.attach_runtime_state(db)
    saved_mode = db.get_state("mode", None)
    if saved_mode in ("paper", "small_live", "live", "test"):
        from config import _normalize_mode
        config.MODE = _normalize_mode(saved_mode)
        if config.MODE == "paper":
            config.DRY_RUN = True
    saved_dry_run = db.get_state("dry_run", None)
    if saved_dry_run is not None:
        config.DRY_RUN = bool(saved_dry_run)

    validate_config()

    if db.has_api_creds():
        log.warning("Sensitive API credentials exist in SQLite api_credentials table. Run: python3 clear_sensitive_data.py --yes")

    log.info("=" * 60)
    log.info(f"Multi-Asset Probability Edge Bot {config.VERSION} starting | Mode: {config.mode_display}")
    log.info(f"Bankroll: ${config.starting_bankroll:.0f} | Stop: ${config.stop_loss_balance:.0f}")
    log.info(f"Strategy: {config.STRATEGY_MODE} | Order: {config.ORDER_TYPE} | DataRecorder: {config.ENABLE_DATA_RECORDER}")
    log.info(f"DRY_RUN: {config.DRY_RUN} | OBSERVER_ONLY: {config.OBSERVER_ONLY} | TELEGRAM: {config.ENABLE_TELEGRAM}")
    log.info(f"Assets: {','.join(config.assets_enabled)} | Timeframes: {','.join(config.timeframes_enabled)}")
    if config.real_orders_enabled:
        log.warning("REAL MONEY ORDERS ENABLED: MODE=%s and DRY_RUN=false", config.MODE)
    elif config.has_polymarket_creds and config.DRY_RUN and not config.OBSERVER_ONLY:
        log.warning("AUTHENTICATED DRY-RUN: credentials loaded/checked, but DRY_RUN=true blocks orders")
    elif config.MODE == "small_live" and config.DRY_RUN:
        log.warning("MODE=small_live but DRY_RUN=true: no real orders will be submitted")
    log.info("=" * 60)
    log.info("✅ Database ready")

    feed = MultiAssetBinanceFeed(config.assets_enabled, buffer_size=180)
    await feed.start()
    log.info("✅ Binance feeds running: %s", ", ".join([config.asset_symbol(a) for a in config.assets_enabled]))

    market_ws = None
    realtime_cache = None
    if config.REALTIME_ORDERBOOK_ENABLED:
        realtime_cache = RealtimeOrderbookCache()
        market_ws = PolymarketMarketWebSocket(cache=realtime_cache)
        await market_ws.start()
        log.info("✅ Polymarket public market WS enabled; REST fallback remains active")

    polymarket = PolymarketClient(db, realtime_orderbook=realtime_cache, market_ws=market_ws)

    if config.OBSERVER_ONLY:
        await asyncio.to_thread(polymarket.connect_public)
        log.warning("OBSERVER_ONLY=true: public-only mode; trading loop and Telegram are disabled")
        await run_observer_only(db, polymarket, feed, market_ws=market_ws)
        return

    if config.has_polymarket_creds:
        await asyncio.to_thread(polymarket.connect)
        if config.real_orders_enabled and config.CANCEL_OPEN_ORDERS_ON_START:
            canceled = await asyncio.to_thread(polymarket.cancel_open_orders_safe)
            if canceled:
                log.warning("Startup safety canceled %s pre-existing open order(s)", canceled)
    else:
        await asyncio.to_thread(polymarket.connect_public)
        log.warning("No valid Polymarket credentials loaded yet: Telegram remains available; real orders are blocked until keys are set")

    risk = RiskManager(db, feed)
    await startup_risk_health_check(db, polymarket, risk)
    if db.get_state("halted", False):
        risk.is_halted = True
        risk.halt_reason = db.get_state("halt_reason", "")
        log.warning(f"Bot was halted: {risk.halt_reason}. Use TG /start → ▶️ 启动 to resume.")

    strategy = {f"{asset}:{tf}": PinBarStrategy(feed.get_feed(asset), db, asset=asset, timeframe=tf) for asset in config.assets_enabled for tf in config.timeframes_enabled}
    learner = Learner(db)

    if config.ENABLE_TELEGRAM:
        if TelegramBot is None:
            log.error("ENABLE_TELEGRAM=true but python-telegram-bot is not importable. Run pip install -r requirements.txt")
            sys.exit(1)
        tg = TelegramBot(db, polymarket=polymarket, feed=feed,
                         risk=risk, learner=learner)
        await tg.start()
    else:
        tg = NoopNotifier()
        log.warning("ENABLE_TELEGRAM=false: Telegram UI/alerts disabled")

    trader = Trader(strategy, polymarket, risk, feed, db, notify=tg.push, learner=learner)
    product_doctor = ProductDoctor(db, market_ws=market_ws)
    if hasattr(tg, "trader"):
        tg.trader = trader  # Wire back-reference
    if hasattr(tg, "product_doctor"):
        tg.product_doctor = product_doctor

    # ============ Run trading loop ============

    log.info("🚀 All systems online. Waiting for probability-edge signals...")

    # Send startup notification
    await tg.push("daily_summary", learner.daily_summary())

    # Setup graceful shutdown
    stop_event = asyncio.Event()

    def shutdown_handler():
        log.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            pass  # Windows

    # Run main components concurrently
    trader_task = asyncio.create_task(trader.run())
    daily_task = asyncio.create_task(daily_summary_loop(tg, learner))
    doctor_task = asyncio.create_task(product_doctor_loop(tg, product_doctor)) if config.PRODUCT_DOCTOR_ENABLED else None
    ws_cleanup_task = asyncio.create_task(ws_subscription_cleanup_loop(db, market_ws)) if market_ws else None
    data_collector = DataCollector(db, polymarket, feed) if config.ENABLE_DATA_RECORDER else None
    data_task = asyncio.create_task(data_collector.run()) if data_collector else None

    await stop_event.wait()

    # ============ Graceful shutdown ============

    log.info("Shutting down...")
    trader.stop()
    daily_task.cancel()
    if doctor_task:
        doctor_task.cancel()
    if ws_cleanup_task:
        ws_cleanup_task.cancel()
    if data_collector:
        data_collector.stop()
    if data_task:
        data_task.cancel()

    await asyncio.gather(
        trader_task, daily_task,
        *( [doctor_task] if doctor_task else [] ),
        *( [ws_cleanup_task] if ws_cleanup_task else [] ),
        *( [data_task] if data_task else [] ),
        return_exceptions=True,
    )
    await feed.stop()
    if market_ws:
        await market_ws.stop()
    await tg.stop()
    log.info("Goodbye!")



async def run_observer_only(db: Database, polymarket: PolymarketClient, feed: BinanceFeed, market_ws=None):
    """Run only the public-data recorder. No auth, no Telegram, no trading. """
    stop_event = asyncio.Event()

    def shutdown_handler():
        log.info("Shutdown signal received")
        stop_event.set()

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, shutdown_handler)
        except NotImplementedError:
            pass

    data_collector = DataCollector(db, polymarket, feed)
    data_task = asyncio.create_task(data_collector.run())
    log.info("📚 Observer-only mode running. Use Ctrl+C to stop.")
    await stop_event.wait()
    data_collector.stop()
    data_task.cancel()
    await asyncio.gather(data_task, return_exceptions=True)
    await feed.stop()
    if market_ws:
        await market_ws.stop()
    log.info("Observer-only shutdown complete")

async def product_doctor_loop(tg, doctor: ProductDoctor):
    """Periodically run product self-diagnosis and push warning-level findings."""
    while True:
        try:
            await asyncio.sleep(max(60, int(config.PRODUCT_DOCTOR_INTERVAL_SEC)))
            report = doctor.diagnose()
            if config.PRODUCT_DOCTOR_PUSH_WARNINGS and report.status in {"warning", "critical"}:
                await tg.push("product_doctor_alert", report.to_dict())
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception("Product doctor loop error: %s", e)
            await asyncio.sleep(300)


async def ws_subscription_cleanup_loop(db: Database, market_ws: PolymarketMarketWebSocket):
    """Bound public WS subscriptions so expired 5m/15m token ids cannot leak."""
    while True:
        try:
            await asyncio.sleep(max(60, int(getattr(config, "REALTIME_ORDERBOOK_CLEANUP_INTERVAL_SEC", 600) or 600)))
            active_tokens = await asyncio.to_thread(db.get_active_market_tokens)
            removed = await market_ws.cleanup_expired_subscriptions(active_tokens)
            if removed:
                log.info("WS subscription cleanup complete: removed=%s active_tokens=%s total=%s", removed, len(active_tokens), len(market_ws.desired_assets))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("WS subscription cleanup loop error: %s", e)
            await asyncio.sleep(300)


async def daily_summary_loop(tg: TelegramBot, learner: Learner):
    """Send daily summary at UTC midnight. Never crash silently."""
    while True:
        try:
            now = datetime.now(timezone.utc)
            seconds_until_midnight = 86400 - (now.hour * 3600 + now.minute * 60 + now.second)
            await asyncio.sleep(max(60, seconds_until_midnight))
            summary = learner.daily_summary()
            await tg.push("daily_summary", summary)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.exception(f"Daily summary loop error: {e}")
            await asyncio.sleep(300)


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
