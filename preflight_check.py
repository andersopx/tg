"""
Preflight check for BTC Polymarket 5m bot.

Online:
    python3 preflight_check.py
Offline parser fixture:
    python3 preflight_check.py --offline-fixture tests/fixtures/gamma_btc_5m.json
"""
import argparse
import asyncio
import importlib
import json
import sys
import time

# Python 3.6 compatibility for importlib.metadata
try:
    import importlib.metadata as importlib_metadata
except ImportError:
    import importlib_metadata

from config import config
from database import Database
from binance_feed import BinanceFeed
from polymarket_client import PolymarketClient, MarketOrderArgs


def ok(msg): print(f"✅ {msg}")
def warn(msg): print(f"⚠️  {msg}")
def fail(msg): print(f"❌ {msg}")


def check_common_config():
    errors = 0
    print(f"Preflight for {config.VERSION}")
    print(f"Mode={config.mode_display} | DRY_RUN={config.DRY_RUN} | TELEGRAM={config.ENABLE_TELEGRAM} | OBSERVER_ONLY={config.OBSERVER_ONLY}")
    if str(config.MODE_RAW).strip().lower() == "test":
        warn("Legacy MODE=test detected; use MODE=small_live for real small trading or MODE=paper for no-order simulation")
    if config.real_orders_enabled:
        warn("REAL MONEY ORDERS ENABLED: DRY_RUN=false")
        if not config.has_polymarket_creds:
            errors += 1
            fail("Valid Polymarket private key/funder required for real orders")
    elif config.has_polymarket_creds:
        ok("Authenticated dry-run/staging credentials detected; auth will be checked, but no real orders are enabled")
    else:
        ok("No real orders enabled")
    if config.ENABLE_TELEGRAM and not config.has_telegram_creds:
        errors += 1
        fail("ENABLE_TELEGRAM=true but Telegram credentials are incomplete or placeholders")
    return errors


def check_packages():
    errors = 0
    for mod in ("aiohttp", "websockets", "dotenv"):
        try:
            importlib.import_module(mod)
            ok(f"Python package available: {mod}")
        except Exception as e:
            errors += 1
            fail(f"Missing/failed package {mod}: {e}")
    try:
        importlib.import_module("telegram")
        ok("Python package available: telegram")
    except Exception as e:
        if config.ENABLE_TELEGRAM:
            errors += 1
            fail(f"Missing telegram package while ENABLE_TELEGRAM=true: {e}")
        else:
            warn("telegram package unavailable; okay because ENABLE_TELEGRAM=false")
    try:
        version = importlib_metadata.version("py-clob-client")
        ok(f"py-clob-client version: {version}")
    except Exception:
        if config.real_orders_enabled or config.has_polymarket_creds:
            errors += 1
            fail("py-clob-client not installed; required for authenticated preflight/trading")
        else:
            warn("py-clob-client not installed; okay for offline/parser checks, but needed for orderbook/trading")
    if MarketOrderArgs is not None:
        ok("py-clob-client supports MarketOrderArgs")
    elif config.real_orders_enabled or config.has_polymarket_creds:
        errors += 1
        fail("MarketOrderArgs missing; upgrade py-clob-client>=0.34.6 before authenticated staging/real trading")
    else:
        warn("MarketOrderArgs unavailable; real trading disabled")
    return errors


def check_parsed_market(pm, market, expected_window_ts=None):
    # type: (PolymarketClient, dict, Optional[int]) -> int
    errors = 0
    if not market:
        fail("No market object to check")
        return 1
    ok(f"Market parsed: {market.get('slug')}")
    if market.get("price_to_beat"):
        ok(f"Price to Beat parsed: ${float(market['price_to_beat']):,.2f}")
    elif config.REQUIRE_MARKET_PRICE_TO_BEAT:
        errors += 1
        fail("Price to Beat not parsed")
    else:
        warn("Price to Beat not parsed (check disabled by config)")
    if market.get("rules_chainlink_ok"):
        ok("Market text mentions Chainlink/BTC")
    elif config.STRICT_CHAINLINK_RULE_TEXT:
        errors += 1
        fail("Chainlink/BTC rule text not detected")
    else:
        warn("Chainlink/BTC text not detected but STRICT_CHAINLINK_RULE_TEXT=false")
    if market.get("up_token_id") and market.get("down_token_id"):
        ok("Up/Down token ids parsed")
    else:
        errors += 1
        fail("Up/Down token ids missing")
    slug_ts = market.get("slug_window_ts") or pm._window_ts_from_slug(market.get("slug", ""))
    if expected_window_ts and slug_ts and int(slug_ts) != int(expected_window_ts):
        errors += 1
        fail(f"Market slug window mismatch: expected {expected_window_ts}, got {slug_ts}")
    return errors


async def run_offline_fixture(path: str):
    errors = check_common_config() + check_packages()
    db = Database(config.DB_PATH)
    pm = PolymarketClient(db)
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    obj = raw[0] if isinstance(raw, list) else raw.get("data", [raw])[0] if isinstance(raw, dict) and isinstance(raw.get("data"), list) else raw
    market = pm._parse_market(obj)
    market["slug_window_ts"] = pm._window_ts_from_slug(market.get("slug", ""))
    errors += check_parsed_market(pm, market, market.get("slug_window_ts"))
    print("-" * 60)
    if errors:
        fail(f"Offline preflight completed with {errors} blocking issue(s).")
        return 1
    ok("Offline fixture preflight passed.")
    return 0


async def run_online():
    errors = check_common_config() + check_packages()
    db = Database(config.DB_PATH)
    ok(f"SQLite initialized: {config.DB_PATH}")
    if db.has_api_creds():
        warn("SQLite api_credentials table contains stored credentials. Run clear_sensitive_data.py --yes")

    feed = BinanceFeed(buffer_size=max(30, config.VOL_LOOKBACK_MIN + 5))
    try:
        await feed.bootstrap()
        if feed.get_current_price() > 0 and len(feed.get_recent_klines(5)) >= 5:
            ok(f"Binance REST bootstrap OK, BTC proxy=${feed.get_current_price():,.2f}")
        else:
            errors += 1
            fail("Binance REST bootstrap returned insufficient klines")
    except Exception as e:
        errors += 1
        fail(f"Binance REST bootstrap failed: {e}")

    pm = PolymarketClient(db)
    now = int(time.time())
    window_ts = (now // config.MARKET_INTERVAL_SEC) * config.MARKET_INTERVAL_SEC
    market = await pm.find_btc_5min_market(window_ts, allow_nearby=False)
    if not market:
        errors += 1
        fail(f"Current BTC 5m market not found for window {window_ts}")
    else:
        errors += check_parsed_market(pm, market, window_ts)

    if config.STORE_API_CREDS:
        warn("STORE_API_CREDS=true: API credentials will be stored in SQLite. Keep btc_bot.db private.")
    else:
        ok("STORE_API_CREDS=false: API credentials are not persisted to SQLite")

    if config.real_orders_enabled or config.has_polymarket_creds:
        try:
            pm.connect()
            bal = pm.get_balance()
            if bal is None:
                if config.DRY_RUN:
                    warn("Authenticated balance unavailable; proceeding in DRY_RUN mode")
                elif config.MODE == "small_live":
                    warn("Authenticated balance unavailable; proceeding in small_live mode with caution")
                else:
                    errors += 1
                    fail("Authenticated balance unavailable")
            else:
                ok(f"Authenticated CLOB connection OK, collateral balance≈${bal:.2f}")
                if config.DRY_RUN:
                    ok("Authenticated dry-run confirmed: credentials work, but DRY_RUN=true blocks order submission")
        except Exception as e:
            if config.DRY_RUN:
                warn(f"Authenticated CLOB check error (DRY_RUN mode): {e}")
            elif config.MODE == "small_live":
                warn(f"Authenticated CLOB check error (small_live mode): {e}")
            else:
                errors += 1
                fail(f"Authenticated CLOB check failed: {e}")
    else:
        ok("Authenticated CLOB check skipped because no valid credentials are loaded and real orders are disabled")

    loss_caps = []
    if config.MAX_DAILY_LOSS_USD > 0:
        loss_caps.append(config.MAX_DAILY_LOSS_USD)
    if config.MAX_DAILY_LOSS_PCT > 0:
        loss_caps.append(config.starting_bankroll * config.MAX_DAILY_LOSS_PCT)
    if loss_caps:
        ok(f"Daily realized-loss cap active: ${min(loss_caps):.2f}")
    else:
        warn("Daily realized-loss cap disabled")

    print("-" * 60)
    if errors:
        fail(f"Preflight completed with {errors} blocking issue(s). Fix before real trading.")
        return 1
    ok("Preflight passed.")
    return 0


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline-fixture", help="Gamma market JSON fixture to validate parser without network")
    return ap.parse_args()


if __name__ == "__main__":
    args = parse_args()
    if args.offline_fixture:
        raise SystemExit(asyncio.run(run_offline_fixture(args.offline_fixture)))
    raise SystemExit(asyncio.run(run_online()))
