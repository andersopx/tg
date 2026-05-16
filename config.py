"""
Configuration - all tunable parameters in one place.

V12 final server guard focus:
- Polymarket BTC 5m rule alignment: Up wins when Chainlink end >= start.
- Binance is only a fast proxy; if Gamma exposes Price-to-Beat, use it.
- Avoid fake fills, overfitted learning, and silent task failures.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except Exception:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except Exception:
        return default




def _env_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip()

def _env_csv(name: str, default: str) -> list:
    raw = os.getenv(name, default)
    return [x.strip().upper() for x in str(raw).split(",") if x.strip()]

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def _normalize_mode(raw: str) -> str:
    """Canonical modes. Legacy test remains accepted but becomes small_live."""
    m = str(raw or "paper").strip().lower().replace("-", "_")
    aliases = {
        "dry": "paper",
        "dry_run": "paper",
        "paper_trade": "paper",
        "paper_trading": "paper",
        "demo": "paper",
        "observer": "paper",
        "test": "small_live",          # legacy, but clearer name is small_live
        "small": "small_live",
        "smalllive": "small_live",
        "small_live": "small_live",
        "live": "live",
        "prod": "live",
        "production": "live",
    }
    return aliases.get(m, "paper")


def _is_placeholder_secret(value: str) -> bool:
    """Treat .env.example placeholder values as missing credentials."""
    v = str(value or "").strip().lower()
    if not v:
        return True
    placeholders = {
        "your_private_key_here",
        "your_wallet_address_here",
        "your_telegram_bot_token_here",
        "123456789",
        "0",
        "none",
        "null",
        "changeme",
        "replace_me",
        "replace-me",
    }
    return v in placeholders or v.startswith("your_") or "placeholder" in v


@dataclass
class Config:
    # v14.2.42 Profit Rule Engine: learned rule mining, not fixed 75% only.
    PROFIT_RULE_ENGINE_ENABLED: bool = _env_bool("PROFIT_RULE_ENGINE_ENABLED", True)
    PROFIT_RULE_HARD_75_ENABLED: bool = _env_bool("PROFIT_RULE_HARD_75_ENABLED", False)
    PROFIT_RULE_DEFAULT_HIGH_PROB: float = _env_float("PROFIT_RULE_DEFAULT_HIGH_PROB", 0.75)
    PROFIT_RULE_SHADOW_MIN_PROB: float = _env_float("PROFIT_RULE_SHADOW_MIN_PROB", 0.65)
    PROFIT_RULE_MIN_ENTRY_PRICE: float = _env_float("PROFIT_RULE_MIN_ENTRY_PRICE", 0.08)
    PROFIT_RULE_MAX_ENTRY_PRICE: float = _env_float("PROFIT_RULE_MAX_ENTRY_PRICE", 0.75)
    PROFIT_RULE_MAX_MODEL_GAP: float = _env_float("PROFIT_RULE_MAX_MODEL_GAP", 0.50)
    PROFIT_RULE_MIN_NET_PROFIT_MULTIPLE: float = _env_float("PROFIT_RULE_MIN_NET_PROFIT_MULTIPLE", 1.0)
    PROFIT_RULE_MAX_NET_PROFIT_MULTIPLE: float = _env_float("PROFIT_RULE_MAX_NET_PROFIT_MULTIPLE", 12.0)
    PROFIT_RULE_MIN_SAMPLES_PROMOTE: int = _env_int("PROFIT_RULE_MIN_SAMPLES_PROMOTE", 8)
    PROFIT_RULE_MIN_RECENT_SAMPLES: int = _env_int("PROFIT_RULE_MIN_RECENT_SAMPLES", 3)
    PROFIT_RULE_RECENT_N: int = _env_int("PROFIT_RULE_RECENT_N", 10)
    PROFIT_RULE_MAX_LOSS_STREAK: int = _env_int("PROFIT_RULE_MAX_LOSS_STREAK", 2)
    PROFIT_RULE_FREEZE_MINUTES: int = _env_int("PROFIT_RULE_FREEZE_MINUTES", 60)
    PROFIT_RULE_MIN_PROMOTE_WIN_RATE: float = _env_float("PROFIT_RULE_MIN_PROMOTE_WIN_RATE", 0.62)
    PROFIT_RULE_MIN_AVG_PNL_PROMOTE: float = _env_float("PROFIT_RULE_MIN_AVG_PNL_PROMOTE", 0.25)
    PROFIT_RULE_LIVE_REQUIRE_PROMOTE: bool = _env_bool("PROFIT_RULE_LIVE_REQUIRE_PROMOTE", True)
    PROFIT_RULE_RELAXED_CAN_TRADE: bool = _env_bool("PROFIT_RULE_RELAXED_CAN_TRADE", False)
    PROFIT_RULE_NEW_RULE_CAN_SHADOW: bool = _env_bool("PROFIT_RULE_NEW_RULE_CAN_SHADOW", True)
    PROFIT_RULE_NEW_RULE_REQUIRES_ADAPTIVE_EDGE: bool = _env_bool("PROFIT_RULE_NEW_RULE_REQUIRES_ADAPTIVE_EDGE", True)
    PROFIT_RULE_NEW_RULE_MIN_ADAPTIVE_EV: float = _env_float("PROFIT_RULE_NEW_RULE_MIN_ADAPTIVE_EV", 0.08)
    PROFIT_RULE_NEW_RULE_MIN_ADAPTIVE_QUALITY: float = _env_float("PROFIT_RULE_NEW_RULE_MIN_ADAPTIVE_QUALITY", 0.56)
    PROFIT_RULE_PROMOTE_REQUIRE_TARGET_WIN_RATE: bool = _env_bool("PROFIT_RULE_PROMOTE_REQUIRE_TARGET_WIN_RATE", True)
    PROFIT_RULE_MIN_WILSON_PROMOTE: float = _env_float("PROFIT_RULE_MIN_WILSON_PROMOTE", 0.55)


    # ============ v14.3.2 Adaptive Edge Engine ============
    # Replaces fixed 75% thinking with dynamic EV, settlement probability and rule health.
    ADAPTIVE_EDGE_ENGINE_ENABLED: bool = _env_bool("ADAPTIVE_EDGE_ENGINE_ENABLED", True)
    ADAPTIVE_EDGE_MIN_EV_SHADOW: float = _env_float("ADAPTIVE_EDGE_MIN_EV_SHADOW", 0.08)
    ADAPTIVE_EDGE_MIN_EV_LIVE: float = _env_float("ADAPTIVE_EDGE_MIN_EV_LIVE", 0.16)
    ADAPTIVE_EDGE_MIN_QUALITY_SHADOW: float = _env_float("ADAPTIVE_EDGE_MIN_QUALITY_SHADOW", 0.56)
    ADAPTIVE_EDGE_MIN_QUALITY_LIVE: float = _env_float("ADAPTIVE_EDGE_MIN_QUALITY_LIVE", 0.70)
    ADAPTIVE_EDGE_BASE_RISK_BUFFER: float = _env_float("ADAPTIVE_EDGE_BASE_RISK_BUFFER", 0.06)
    ADAPTIVE_EDGE_MAX_NEGATIVE_VELOCITY_PENALTY: float = _env_float("ADAPTIVE_EDGE_MAX_NEGATIVE_VELOCITY_PENALTY", 0.10)
    ADAPTIVE_EDGE_MIN_SECONDS_LEFT_5M: int = _env_int("ADAPTIVE_EDGE_MIN_SECONDS_LEFT_5M", 20)
    ADAPTIVE_EDGE_MAX_SECONDS_LEFT_5M: int = _env_int("ADAPTIVE_EDGE_MAX_SECONDS_LEFT_5M", 240)
    ADAPTIVE_EDGE_MIN_SECONDS_LEFT_15M: int = _env_int("ADAPTIVE_EDGE_MIN_SECONDS_LEFT_15M", 45)
    ADAPTIVE_EDGE_MAX_SECONDS_LEFT_15M: int = _env_int("ADAPTIVE_EDGE_MAX_SECONDS_LEFT_15M", 600)
    ADAPTIVE_EDGE_PROB_BLEND_MODEL: float = _env_float("ADAPTIVE_EDGE_PROB_BLEND_MODEL", 0.45)
    ADAPTIVE_EDGE_PROB_BLEND_SETTLEMENT: float = _env_float("ADAPTIVE_EDGE_PROB_BLEND_SETTLEMENT", 0.40)
    ADAPTIVE_EDGE_PROB_BLEND_MARKET: float = _env_float("ADAPTIVE_EDGE_PROB_BLEND_MARKET", 0.15)
    ADAPTIVE_EDGE_UNKNOWN_RULE_MAX_MODE: str = _env_str("ADAPTIVE_EDGE_UNKNOWN_RULE_MAX_MODE", "shadow")

    # ============ High win-rate target mode ============
    # These gates do not guarantee any fixed win rate. They make the live system
    # refuse low-conviction setups, pause weak rule clusters, and protect profits.
    HIGH_WIN_RATE_MODE: bool = _env_bool("HIGH_WIN_RATE_MODE", False)
    TARGET_WIN_RATE: float = _env_float("TARGET_WIN_RATE", 0.75)
    HIGH_WIN_RATE_MIN_MODEL_PROB: float = _env_float("HIGH_WIN_RATE_MIN_MODEL_PROB", 0.74)
    HIGH_WIN_RATE_UNKNOWN_STRATEGY_MIN_MODEL_PROB: float = _env_float("HIGH_WIN_RATE_UNKNOWN_STRATEGY_MIN_MODEL_PROB", 0.78)
    HIGH_WIN_RATE_MIN_EDGE_AFTER_FEES: float = _env_float("HIGH_WIN_RATE_MIN_EDGE_AFTER_FEES", 0.035)
    HIGH_WIN_RATE_MAX_TOKEN_PRICE: float = _env_float("HIGH_WIN_RATE_MAX_TOKEN_PRICE", 0.92)
    HIGH_WIN_RATE_MIN_RECENT_SAMPLES: int = _env_int("HIGH_WIN_RATE_MIN_RECENT_SAMPLES", 8)
    HIGH_WIN_RATE_REJECT_RECENT_WIN_RATE: float = _env_float("HIGH_WIN_RATE_REJECT_RECENT_WIN_RATE", 0.50)
    HIGH_WIN_RATE_MAX_STRATEGY_LOSS_STREAK: int = _env_int("HIGH_WIN_RATE_MAX_STRATEGY_LOSS_STREAK", 2)
    HIGH_WIN_RATE_WILSON_Z: float = _env_float("HIGH_WIN_RATE_WILSON_Z", 1.2815515655446004)
    HIGH_WIN_RATE_MIN_WILSON_LOWER_BOUND: float = _env_float("HIGH_WIN_RATE_MIN_WILSON_LOWER_BOUND", 0.55)
    HIGH_WIN_RATE_STATS_LOOKBACK: int = _env_int("HIGH_WIN_RATE_STATS_LOOKBACK", 80)
    HIGH_WIN_RATE_USE_SHADOW_STATS: bool = _env_bool("HIGH_WIN_RATE_USE_SHADOW_STATS", True)

    WINRATE_GOVERNOR_ENABLED: bool = _env_bool("WINRATE_GOVERNOR_ENABLED", True)
    WINRATE_GOVERNOR_MIN_TRADES: int = _env_int("WINRATE_GOVERNOR_MIN_TRADES", 8)
    WINRATE_GOVERNOR_MIN_WIN_RATE: float = _env_float("WINRATE_GOVERNOR_MIN_WIN_RATE", 0.55)
    WINRATE_GOVERNOR_INCLUDE_SHADOW: bool = _env_bool("WINRATE_GOVERNOR_INCLUDE_SHADOW", False)
    DAILY_PROFIT_LOCK_ENABLED: bool = _env_bool("DAILY_PROFIT_LOCK_ENABLED", True)
    DAILY_PROFIT_LOCK_START_USD: float = _env_float("DAILY_PROFIT_LOCK_START_USD", 3.0)
    DAILY_PROFIT_LOCK_GIVEBACK_USD: float = _env_float("DAILY_PROFIT_LOCK_GIVEBACK_USD", 1.25)
    DAILY_PROFIT_LOCK_GIVEBACK_PCT: float = _env_float("DAILY_PROFIT_LOCK_GIVEBACK_PCT", 0.45)

    VERSION: str = "v15.1.4-shadow-orphan-fix"


    # ============ One-click startup gate ============
    # start.sh runs: preflight -> historical backtest gate -> optional Polymarket replay -> main.
    AUTO_PREFLIGHT_ON_START: bool = _env_bool("AUTO_PREFLIGHT_ON_START", True)
    AUTO_BACKTEST_ON_START: bool = _env_bool("AUTO_BACKTEST_ON_START", True)
    BACKTEST_REQUIRED_BEFORE_LIVE: bool = _env_bool("BACKTEST_REQUIRED_BEFORE_LIVE", True)
    BACKTEST_LOOKBACK_DAYS: int = _env_int("BACKTEST_LOOKBACK_DAYS", 365)
    BACKTEST_SOURCE: str = os.getenv("BACKTEST_SOURCE", "binance-api")
    BACKTEST_SYMBOL: str = os.getenv("BACKTEST_SYMBOL", "BTCUSDT")
    BACKTEST_SWEEP: bool = _env_bool("BACKTEST_SWEEP", True)
    BACKTEST_MIN_TRADES: int = _env_int("BACKTEST_MIN_TRADES", 200)
    BACKTEST_MIN_WIN_RATE: float = _env_float("BACKTEST_MIN_WIN_RATE", 0.60)
    BACKTEST_MAX_LOSS_STREAK: int = _env_int("BACKTEST_MAX_LOSS_STREAK", 12)
    BACKTEST_OUT_DIR: str = os.getenv("BACKTEST_OUT_DIR", "backtest_reports")
    BACKTEST_CACHE_DIR: str = os.getenv("BACKTEST_CACHE_DIR", "backtest_cache")
    BACKTEST_SWEEP_ENTRIES: str = os.getenv("BACKTEST_SWEEP_ENTRIES", "45,60,75,90,120,180")
    BACKTEST_SWEEP_PROBS: str = os.getenv("BACKTEST_SWEEP_PROBS", "0.60,0.62,0.65,0.68,0.70")
    BACKTEST_SWEEP_DISTANCES: str = os.getenv("BACKTEST_SWEEP_DISTANCES", "25,35,50,75")
    POLYMARKET_REPLAY_ON_START: bool = _env_bool("POLYMARKET_REPLAY_ON_START", False)
    # For real orders, recent Polymarket price-history replay is required by default.
    # This is slower than pure BTC backtest, but it is the only gate that checks historical CLOB entry prices.
    POLYMARKET_REPLAY_REQUIRED_BEFORE_LIVE: bool = _env_bool("POLYMARKET_REPLAY_REQUIRED_BEFORE_LIVE", True)
    POLYMARKET_REPLAY_DAYS: int = _env_int("POLYMARKET_REPLAY_DAYS", 7)
    POLYMARKET_REPLAY_MIN_PRICED_ROWS: int = _env_int("POLYMARKET_REPLAY_MIN_PRICED_ROWS", 20)
    POLYMARKET_REPLAY_MIN_WIN_RATE: float = _env_float("POLYMARKET_REPLAY_MIN_WIN_RATE", 0.54)
    POLYMARKET_REPLAY_MIN_AVG_PNL_PER_SHARE: float = _env_float("POLYMARKET_REPLAY_MIN_AVG_PNL_PER_SHARE", 0.0)
    ONECLICK_CONTINUE_ON_DRYRUN_BACKTEST_FAIL: bool = _env_bool("ONECLICK_CONTINUE_ON_DRYRUN_BACKTEST_FAIL", True)

    # ============ Multi-asset support ============
    # Enabled assets. Set ASSETS_ENABLED=BTC to return to the old single-asset behavior.
    ASSETS_ENABLED: str = os.getenv("ASSETS_ENABLED", "BTC,ETH,SOL,XRP")
    # Timeframes to trade. Supported now: 5m and 15m crypto Up/Down markets.
    TIMEFRAMES_ENABLED: str = os.getenv("TIMEFRAMES_ENABLED", "5m,15m")
    MAX_TRADES_PER_WINDOW: int = _env_int("MAX_TRADES_PER_WINDOW", 3)
    # Try more candidates than actual per-window order cap. If top signals fail CLOB/risk filters, lower-ranked signals can still be tested.
    MAX_SIGNAL_CANDIDATES_PER_LOOP: int = _env_int("MAX_SIGNAL_CANDIDATES_PER_LOOP", 18)

    BTC_BINANCE_SYMBOL: str = os.getenv("BTC_BINANCE_SYMBOL", "BTCUSDT")
    ETH_BINANCE_SYMBOL: str = os.getenv("ETH_BINANCE_SYMBOL", "ETHUSDT")
    SOL_BINANCE_SYMBOL: str = os.getenv("SOL_BINANCE_SYMBOL", "SOLUSDT")
    XRP_BINANCE_SYMBOL: str = os.getenv("XRP_BINANCE_SYMBOL", "XRPUSDT")

    BTC_SLUG_PREFIX: str = os.getenv("BTC_SLUG_PREFIX", "btc")
    ETH_SLUG_PREFIX: str = os.getenv("ETH_SLUG_PREFIX", "eth")
    SOL_SLUG_PREFIX: str = os.getenv("SOL_SLUG_PREFIX", "sol")
    XRP_SLUG_PREFIX: str = os.getenv("XRP_SLUG_PREFIX", "xrp")

    BTC_BANKROLL_ALLOCATION: float = _env_float("BTC_BANKROLL_ALLOCATION", 1.0)
    ETH_BANKROLL_ALLOCATION: float = _env_float("ETH_BANKROLL_ALLOCATION", 1.0)
    SOL_BANKROLL_ALLOCATION: float = _env_float("SOL_BANKROLL_ALLOCATION", 1.0)
    XRP_BANKROLL_ALLOCATION: float = _env_float("XRP_BANKROLL_ALLOCATION", 1.0)

    BTC_PIN_BAR_MIN_RANGE: float = _env_float("BTC_PIN_BAR_MIN_RANGE", 25.0)
    ETH_PIN_BAR_MIN_RANGE: float = _env_float("ETH_PIN_BAR_MIN_RANGE", 5.0)
    SOL_PIN_BAR_MIN_RANGE: float = _env_float("SOL_PIN_BAR_MIN_RANGE", 0.5)
    XRP_PIN_BAR_MIN_RANGE: float = _env_float("XRP_PIN_BAR_MIN_RANGE", 0.003)

    BTC_MIN_PRICE_DISTANCE: float = _env_float("BTC_MIN_PRICE_DISTANCE", 35.0)
    ETH_MIN_PRICE_DISTANCE: float = _env_float("ETH_MIN_PRICE_DISTANCE", 8.0)
    SOL_MIN_PRICE_DISTANCE: float = _env_float("SOL_MIN_PRICE_DISTANCE", 1.0)
    XRP_MIN_PRICE_DISTANCE: float = _env_float("XRP_MIN_PRICE_DISTANCE", 0.006)

    BTC_BLACK_SWAN: float = _env_float("BTC_BLACK_SWAN", 0.012)
    ETH_BLACK_SWAN: float = _env_float("ETH_BLACK_SWAN", 0.018)
    SOL_BLACK_SWAN: float = _env_float("SOL_BLACK_SWAN", 0.025)
    XRP_BLACK_SWAN: float = _env_float("XRP_BLACK_SWAN", 0.030)

    # ============ Polymarket API ============
    POLYMARKET_PRIVATE_KEY: str = os.getenv("POLYMARKET_PRIVATE_KEY", "")
    POLYMARKET_FUNDER: str = os.getenv("POLYMARKET_FUNDER", "")
    POLYMARKET_SIGNATURE_TYPE: int = _env_int("POLYMARKET_SIGNATURE_TYPE", 1)
    POLYMARKET_HOST: str = os.getenv("POLYMARKET_HOST", "https://clob.polymarket.com")
    POLYMARKET_GAMMA: str = os.getenv("POLYMARKET_GAMMA", "https://gamma-api.polymarket.com")
    POLYMARKET_DATA_API: str = os.getenv("POLYMARKET_DATA_API", "https://data-api.polymarket.com")
    POLYGON_CHAIN_ID: int = _env_int("POLYGON_CHAIN_ID", 137)

    # ============ Telegram ============
    TG_BOT_TOKEN: str = os.getenv("TG_BOT_TOKEN", "")
    TG_USER_ID: int = _env_int("TG_USER_ID", 0)

    # ============ Mode ============
    # Canonical values: paper / small_live / live.
    # Legacy MODE=test is still accepted, but normalized to small_live.
    MODE_RAW: str = os.getenv("MODE", "paper")
    MODE: str = _normalize_mode(os.getenv("MODE", "paper"))
    DRY_RUN: bool = _env_bool("DRY_RUN", True)  # fail-safe default: no real orders unless .env explicitly sets false
    # Independent real-order arming switch. DRY_RUN=false alone must never enable live orders.
    REAL_TRADING_ENABLED: bool = _env_bool("REAL_TRADING_ENABLED", False)
    # OBSERVER_ONLY=true runs public market/data collection only. No Telegram, no auth, no trading loop.
    OBSERVER_ONLY: bool = _env_bool("OBSERVER_ONLY", False)
    ENABLE_TELEGRAM: bool = _env_bool("ENABLE_TELEGRAM", False)
    # Telegram can switch authenticated dry-run into small_live real orders only after startup gates pass.
    # This avoids editing files while still preventing one-tap unsafe live escalation.
    TELEGRAM_LIVE_SWITCH_ENABLED: bool = _env_bool("TELEGRAM_LIVE_SWITCH_ENABLED", True)
    TELEGRAM_LIVE_SWITCH_REQUIRE_GATE: bool = _env_bool("TELEGRAM_LIVE_SWITCH_REQUIRE_GATE", True)
    LIVE_SWITCH_MAX_GATE_AGE_SEC: int = _env_int("LIVE_SWITCH_MAX_GATE_AGE_SEC", 86400)

    # ============ Market / resolution model ============
    MARKET_INTERVAL_SEC: int = 300
    TIE_GOES_TO_UP: bool = True
    REFERENCE_PRICE_SOURCE: str = os.getenv("REFERENCE_PRICE_SOURCE", "binance_proxy")
    USE_MARKET_PRICE_TO_BEAT: bool = _env_bool("USE_MARKET_PRICE_TO_BEAT", True)
    REQUIRE_MARKET_PRICE_TO_BEAT: bool = _env_bool("REQUIRE_MARKET_PRICE_TO_BEAT", True)
    # V14.2.16: If Gamma does not expose Price-to-Beat, do not hard-stop before
    # reading the live CLOB. Fall back to the signal's reference line and still
    # require orderbook/slippage/edge checks before placing a real order.
    PRICE_TO_BEAT_FALLBACK_ENABLED: bool = _env_bool("PRICE_TO_BEAT_FALLBACK_ENABLED", True)
    MAX_REFERENCE_MISMATCH_USD: float = _env_float("MAX_REFERENCE_MISMATCH_USD", 150.0)
    STALE_FEED_MAX_SEC: int = _env_int("STALE_FEED_MAX_SEC", 15)
    MARKET_SLUG_SEARCH_RADIUS: int = _env_int("MARKET_SLUG_SEARCH_RADIUS", 0)  # trading uses exact 5m window by default
    ALLOW_NEARBY_MARKET_FOR_TRADING: bool = _env_bool("ALLOW_NEARBY_MARKET_FOR_TRADING", False)
    STRICT_CHAINLINK_RULE_TEXT: bool = _env_bool("STRICT_CHAINLINK_RULE_TEXT", True)

    # ============ Capital & Position Sizing ============
    TEST_BANKROLL: float = _env_float("TEST_BANKROLL", 100.0)
    TEST_BET_SIZE: float = _env_float("TEST_BET_SIZE", 1.0)
    TEST_STOP_LOSS_BALANCE: float = _env_float("TEST_STOP_LOSS_BALANCE", 0.5)

    # Optional capital isolation. 0 = disabled. When set, the bot treats only this
    # amount as authorized trading principal and reserves the rest of wallet cash.
    # Example: wallet balance 100, AUTHORIZED_CAPITAL_USD=50 -> reserve 50;
    # losses stop when balance reaches the reserve, while profits are not capped.
    AUTHORIZED_CAPITAL_USD: float = _env_float("AUTHORIZED_CAPITAL_USD", 0.0)

    LIVE_BANKROLL: float = _env_float("LIVE_BANKROLL", 100.0)
    LIVE_MIN_BET: float = _env_float("LIVE_MIN_BET", 5.0)
    LIVE_MAX_BET: float = _env_float("LIVE_MAX_BET", 25.0)
    LIVE_STOP_LOSS_BALANCE: float = _env_float("LIVE_STOP_LOSS_BALANCE", 50.0)
    LIVE_MAX_BET_RATIO: float = _env_float("LIVE_MAX_BET_RATIO", 0.15)

    # ============ Risk Protection ============
    CONSECUTIVE_LOSS_LIMIT: int = _env_int("CONSECUTIVE_LOSS_LIMIT", 3)
    COOLDOWN_AFTER_LOSSES_SEC: int = _env_int("COOLDOWN_AFTER_LOSSES_SEC", 1800)
    BLACK_SWAN_THRESHOLD: float = _env_float("BLACK_SWAN_THRESHOLD", 0.012)
    BLACK_SWAN_WINDOW_SEC: int = _env_int("BLACK_SWAN_WINDOW_SEC", 300)
    MAX_TRADES_PER_DAY: int = _env_int("MAX_TRADES_PER_DAY", 24)
    MAX_ORDER_ATTEMPTS_PER_DAY: int = _env_int("MAX_ORDER_ATTEMPTS_PER_DAY", 72)
    MAX_DAILY_LOSS_USD: float = _env_float("MAX_DAILY_LOSS_USD", 12.0)
    MAX_DAILY_LOSS_PCT: float = _env_float("MAX_DAILY_LOSS_PCT", 0.12)
    MAX_OPEN_TRADES: int = _env_int("MAX_OPEN_TRADES", 3)

    # ============ Strategy: probability edge ============
    STRATEGY_MODE: str = os.getenv("STRATEGY_MODE", "probability_edge")
    VOL_LOOKBACK_MIN: int = _env_int("VOL_LOOKBACK_MIN", 20)
    MOMENTUM_LOOKBACK_MIN: int = _env_int("MOMENTUM_LOOKBACK_MIN", 3)
    MOMENTUM_WEIGHT: float = _env_float("MOMENTUM_WEIGHT", 0.35)

    MIN_PRICE_DISTANCE_USD: float = _env_float("MIN_PRICE_DISTANCE_USD", 35.0)
    MIN_DISTANCE_SIGMA_MULT: float = _env_float("MIN_DISTANCE_SIGMA_MULT", 0.45)
    MIN_MODEL_PROB: float = _env_float("MIN_MODEL_PROB", 0.59)
    MIN_EDGE_AFTER_FEES: float = _env_float("MIN_EDGE_AFTER_FEES", 0.035)
    MIN_CONFIDENCE_TO_TRADE: float = _env_float("MIN_CONFIDENCE_TO_TRADE", 0.59)

    PIN_BAR_TAIL_RATIO: float = _env_float("PIN_BAR_TAIL_RATIO", 2.0)
    PIN_BAR_BODY_MAX_RATIO: float = _env_float("PIN_BAR_BODY_MAX_RATIO", 0.35)
    PIN_BAR_MIN_RANGE_USD: float = _env_float("PIN_BAR_MIN_RANGE_USD", 25.0)

    # ============ Multi-strategy signal expansion ============
    # Adds more trade opportunities beyond the original probability/pin-bar style signal.
    # Keep comma-separated names. Recommended: prob_edge,odds_reversal,late_line_snipe,momentum_follow,lottery_reversal
    STRATEGIES_ENABLED: str = os.getenv("STRATEGIES_ENABLED", "barrier_reclaim,prob_edge,odds_ev,odds_lag,odds_reversal,wick_rejection_odds,late_line_snipe,momentum_follow,lottery_reversal")

    # Late line snipe: close to settlement, buy the side already controlling Price-to-Beat if CLOB price is still fair.
    LATE_LINE_SNIPE_ENABLED: bool = _env_bool("LATE_LINE_SNIPE_ENABLED", True)
    LATE_SNIPE_MIN_SECONDS_LEFT: int = _env_int("LATE_SNIPE_MIN_SECONDS_LEFT", 5)
    LATE_SNIPE_MAX_SECONDS_LEFT: int = _env_int("LATE_SNIPE_MAX_SECONDS_LEFT", 28)
    LATE_SNIPE_MIN_MODEL_PROB: float = _env_float("LATE_SNIPE_MIN_MODEL_PROB", 0.72)
    LATE_SNIPE_MIN_EDGE_AFTER_FEES: float = _env_float("LATE_SNIPE_MIN_EDGE_AFTER_FEES", 0.020)
    LATE_SNIPE_MIN_TOKEN_PRICE: float = _env_float("LATE_SNIPE_MIN_TOKEN_PRICE", 0.55)
    LATE_SNIPE_MAX_TOKEN_PRICE: float = _env_float("LATE_SNIPE_MAX_TOKEN_PRICE", 0.94)
    LATE_SNIPE_MAX_SPREAD: float = _env_float("LATE_SNIPE_MAX_SPREAD", 0.050)
    LATE_SNIPE_MAX_MARKET_PROB_GAP: float = _env_float("LATE_SNIPE_MAX_MARKET_PROB_GAP", 0.24)
    LATE_SNIPE_MARKET_CONSENSUS_WEIGHT: float = _env_float("LATE_SNIPE_MARKET_CONSENSUS_WEIGHT", 0.18)
    LATE_SNIPE_MIN_DISTANCE_FACTOR: float = _env_float("LATE_SNIPE_MIN_DISTANCE_FACTOR", 0.45)
    LATE_SNIPE_MIN_SIGMA_MULT: float = _env_float("LATE_SNIPE_MIN_SIGMA_MULT", 0.32)

    # Momentum follow: mid/late window trend continuation, independent of Pin Bar.
    MOMENTUM_FOLLOW_ENABLED: bool = _env_bool("MOMENTUM_FOLLOW_ENABLED", True)
    MOMENTUM_MIN_SECONDS_LEFT: int = _env_int("MOMENTUM_MIN_SECONDS_LEFT", 20)
    MOMENTUM_MAX_SECONDS_LEFT: int = _env_int("MOMENTUM_MAX_SECONDS_LEFT", 170)
    MOMENTUM_MIN_MODEL_PROB: float = _env_float("MOMENTUM_MIN_MODEL_PROB", 0.57)
    MOMENTUM_MIN_EDGE_AFTER_FEES: float = _env_float("MOMENTUM_MIN_EDGE_AFTER_FEES", 0.022)
    MOMENTUM_MIN_TOKEN_PRICE: float = _env_float("MOMENTUM_MIN_TOKEN_PRICE", 0.28)
    MOMENTUM_MAX_TOKEN_PRICE: float = _env_float("MOMENTUM_MAX_TOKEN_PRICE", 0.88)
    MOMENTUM_MAX_SPREAD: float = _env_float("MOMENTUM_MAX_SPREAD", 0.060)
    MOMENTUM_MAX_MARKET_PROB_GAP: float = _env_float("MOMENTUM_MAX_MARKET_PROB_GAP", 0.30)
    MOMENTUM_MARKET_CONSENSUS_WEIGHT: float = _env_float("MOMENTUM_MARKET_CONSENSUS_WEIGHT", 0.24)
    MOMENTUM_MIN_DISTANCE_FACTOR: float = _env_float("MOMENTUM_MIN_DISTANCE_FACTOR", 0.55)
    MOMENTUM_MIN_SIGMA_MULT: float = _env_float("MOMENTUM_MIN_SIGMA_MULT", 0.28)
    MOMENTUM_MIN_DRIFT_SIGMA_MULT: float = _env_float("MOMENTUM_MIN_DRIFT_SIGMA_MULT", 0.09)
    MOMENTUM_DRIFT_CLAMP: float = _env_float("MOMENTUM_DRIFT_CLAMP", 0.0018)

    # Odds reversal: main high-multiple strategy for 5m markets.
    # It buys the side that became cheap after an early move, only when price is moving back toward Price-to-Beat.
    # Example: price first moves above Price-to-Beat, Down becomes cheap, then price starts falling back toward the line.
    ODDS_REVERSAL_ENABLED: bool = _env_bool("ODDS_REVERSAL_ENABLED", True)
    ODDS_REVERSAL_BET_SIZE: float = _env_float("ODDS_REVERSAL_BET_SIZE", 1.0)
    ODDS_REVERSAL_MIN_SECONDS_LEFT: int = _env_int("ODDS_REVERSAL_MIN_SECONDS_LEFT", 35)
    ODDS_REVERSAL_MAX_SECONDS_LEFT: int = _env_int("ODDS_REVERSAL_MAX_SECONDS_LEFT", 240)
    ODDS_REVERSAL_MIN_MODEL_PROB: float = _env_float("ODDS_REVERSAL_MIN_MODEL_PROB", 0.08)
    ODDS_REVERSAL_MAX_MODEL_PROB: float = _env_float("ODDS_REVERSAL_MAX_MODEL_PROB", 0.48)
    ODDS_REVERSAL_MIN_EDGE_AFTER_FEES: float = _env_float("ODDS_REVERSAL_MIN_EDGE_AFTER_FEES", 0.015)
    ODDS_REVERSAL_MIN_TOKEN_PRICE: float = _env_float("ODDS_REVERSAL_MIN_TOKEN_PRICE", 0.03)
    ODDS_REVERSAL_MAX_TOKEN_PRICE: float = _env_float("ODDS_REVERSAL_MAX_TOKEN_PRICE", 0.25)
    ODDS_REVERSAL_MAX_SPREAD: float = _env_float("ODDS_REVERSAL_MAX_SPREAD", 0.045)
    ODDS_REVERSAL_MAX_MARKET_PROB_GAP: float = _env_float("ODDS_REVERSAL_MAX_MARKET_PROB_GAP", 0.22)
    ODDS_REVERSAL_MARKET_CONSENSUS_WEIGHT: float = _env_float("ODDS_REVERSAL_MARKET_CONSENSUS_WEIGHT", 0.05)
    ODDS_REVERSAL_MIN_LIQUIDITY_MULTIPLIER: float = _env_float("ODDS_REVERSAL_MIN_LIQUIDITY_MULTIPLIER", 1.0)
    ODDS_REVERSAL_MIN_DISTANCE_FACTOR: float = _env_float("ODDS_REVERSAL_MIN_DISTANCE_FACTOR", 0.45)
    ODDS_REVERSAL_MAX_DISTANCE_SIGMA_MULT: float = _env_float("ODDS_REVERSAL_MAX_DISTANCE_SIGMA_MULT", 3.20)
    ODDS_REVERSAL_MIN_LINE_CLOSING_FACTOR: float = _env_float("ODDS_REVERSAL_MIN_LINE_CLOSING_FACTOR", 0.12)
    ODDS_REVERSAL_MIN_LINE_CLOSING_SIGMA: float = _env_float("ODDS_REVERSAL_MIN_LINE_CLOSING_SIGMA", 0.08)
    ODDS_REVERSAL_MIN_RECENT_COUNTER_MOVE: float = _env_float("ODDS_REVERSAL_MIN_RECENT_COUNTER_MOVE", 0.00015)

    # Lottery reversal: very small budget, low token price, only when price is moving back toward Price-to-Beat.

    # ============ V14.2.25 Barrier Reclaim / target-line path strategy ============
    # Catches the user-observed pattern: price first moves to the wrong side of the
    # target line, cheapens the opposite token, then starts reclaiming the line.
    BARRIER_RECLAIM_ENABLED: bool = _env_bool("BARRIER_RECLAIM_ENABLED", True)
    BARRIER_RECLAIM_AMBUSH_ENABLED: bool = _env_bool("BARRIER_RECLAIM_AMBUSH_ENABLED", True)
    BARRIER_RECLAIM_CONFIRM_ENABLED: bool = _env_bool("BARRIER_RECLAIM_CONFIRM_ENABLED", True)
    BARRIER_RECLAIM_AMBUSH_MIN_SECONDS_LEFT: int = _env_int("BARRIER_RECLAIM_AMBUSH_MIN_SECONDS_LEFT", 60)
    BARRIER_RECLAIM_AMBUSH_MAX_SECONDS_LEFT: int = _env_int("BARRIER_RECLAIM_AMBUSH_MAX_SECONDS_LEFT", 180)
    BARRIER_RECLAIM_CONFIRM_MIN_SECONDS_LEFT: int = _env_int("BARRIER_RECLAIM_CONFIRM_MIN_SECONDS_LEFT", 10)
    BARRIER_RECLAIM_CONFIRM_MAX_SECONDS_LEFT: int = _env_int("BARRIER_RECLAIM_CONFIRM_MAX_SECONDS_LEFT", 60)
    BARRIER_RECLAIM_DEEP_AMBUSH_ENABLED: bool = _env_bool("BARRIER_RECLAIM_DEEP_AMBUSH_ENABLED", True)
    BARRIER_RECLAIM_DEEP_AMBUSH_MIN_TOKEN_PRICE: float = _env_float("BARRIER_RECLAIM_DEEP_AMBUSH_MIN_TOKEN_PRICE", 0.05)
    BARRIER_RECLAIM_DEEP_AMBUSH_MAX_TOKEN_PRICE: float = _env_float("BARRIER_RECLAIM_DEEP_AMBUSH_MAX_TOKEN_PRICE", 0.19)
    BARRIER_RECLAIM_AMBUSH_MIN_TOKEN_PRICE: float = _env_float("BARRIER_RECLAIM_AMBUSH_MIN_TOKEN_PRICE", 0.20)
    BARRIER_RECLAIM_AMBUSH_MAX_TOKEN_PRICE: float = _env_float("BARRIER_RECLAIM_AMBUSH_MAX_TOKEN_PRICE", 0.45)
    BARRIER_RECLAIM_CONFIRM_MIN_TOKEN_PRICE: float = _env_float("BARRIER_RECLAIM_CONFIRM_MIN_TOKEN_PRICE", 0.05)
    BARRIER_RECLAIM_CONFIRM_MAX_TOKEN_PRICE: float = _env_float("BARRIER_RECLAIM_CONFIRM_MAX_TOKEN_PRICE", 0.90)
    BARRIER_RECLAIM_NO_BUY_ABOVE: float = _env_float("BARRIER_RECLAIM_NO_BUY_ABOVE", 0.90)
    BARRIER_RECLAIM_USE_CUSTOM_QUALITY_GATE: bool = _env_bool("BARRIER_RECLAIM_USE_CUSTOM_QUALITY_GATE", True)
    BARRIER_RECLAIM_DEEP_AMBUSH_MIN_EDGE_AFTER_FEES: float = _env_float("BARRIER_RECLAIM_DEEP_AMBUSH_MIN_EDGE_AFTER_FEES", 0.20)
    BARRIER_RECLAIM_CUSTOM_MIN_EDGE_AFTER_FEES: float = _env_float("BARRIER_RECLAIM_CUSTOM_MIN_EDGE_AFTER_FEES", 0.08)
    BARRIER_RECLAIM_MIN_BOOK_QUALITY: float = _env_float("BARRIER_RECLAIM_MIN_BOOK_QUALITY", 0.45)
    BARRIER_RECLAIM_MAX_PRICE_IMPACT: float = _env_float("BARRIER_RECLAIM_MAX_PRICE_IMPACT", 0.12)
    BARRIER_RECLAIM_DEEP_MAX_PRICE_IMPACT: float = _env_float("BARRIER_RECLAIM_DEEP_MAX_PRICE_IMPACT", 0.16)
    BARRIER_RECLAIM_CONFIRM_MAX_PRICE_IMPACT: float = _env_float("BARRIER_RECLAIM_CONFIRM_MAX_PRICE_IMPACT", 0.08)
    # v14.2.28: final execution recheck needs a separate cap.  The older
    # EXECUTION_GUARD_SLIPPAGE_CAP=0.035 is appropriate for normal signals,
    # but it blocks $1 target-line reclaim entries where best ask can be cheap
    # and depth is thin.  These caps only apply to barrier_reclaim and still
    # keep FOK, depth, age, spread, balance and capital-isolation checks.
    BARRIER_RECLAIM_EXECUTION_SLIPPAGE_CAP: float = _env_float("BARRIER_RECLAIM_EXECUTION_SLIPPAGE_CAP", 0.22)
    BARRIER_RECLAIM_DEEP_EXECUTION_SLIPPAGE_CAP: float = _env_float("BARRIER_RECLAIM_DEEP_EXECUTION_SLIPPAGE_CAP", 0.50)
    BARRIER_RECLAIM_CONFIRM_EXECUTION_SLIPPAGE_CAP: float = _env_float("BARRIER_RECLAIM_CONFIRM_EXECUTION_SLIPPAGE_CAP", 0.18)
    BARRIER_RECLAIM_MAX_WEIGHTED_AVG_PRICE: float = _env_float("BARRIER_RECLAIM_MAX_WEIGHTED_AVG_PRICE", 0.45)
    BARRIER_RECLAIM_DEEP_MAX_WEIGHTED_AVG_PRICE: float = _env_float("BARRIER_RECLAIM_DEEP_MAX_WEIGHTED_AVG_PRICE", 0.35)
    BARRIER_RECLAIM_CONFIRM_MAX_WEIGHTED_AVG_PRICE: float = _env_float("BARRIER_RECLAIM_CONFIRM_MAX_WEIGHTED_AVG_PRICE", 0.90)
    BARRIER_RECLAIM_MAX_SPREAD: float = _env_float("BARRIER_RECLAIM_MAX_SPREAD", 0.10)
    BARRIER_RECLAIM_AMBUSH_MODEL_PROB: float = _env_float("BARRIER_RECLAIM_AMBUSH_MODEL_PROB", 0.56)
    BARRIER_RECLAIM_CONFIRM_MODEL_PROB: float = _env_float("BARRIER_RECLAIM_CONFIRM_MODEL_PROB", 0.78)
    BARRIER_RECLAIM_MIN_EDGE_AFTER_FEES: float = _env_float("BARRIER_RECLAIM_MIN_EDGE_AFTER_FEES", 0.015)
    BARRIER_RECLAIM_BET_SIZE: float = _env_float("BARRIER_RECLAIM_BET_SIZE", 1.0)
    BARRIER_RECLAIM_MIN_DISTANCE_FACTOR: float = _env_float("BARRIER_RECLAIM_MIN_DISTANCE_FACTOR", 0.50)
    BARRIER_RECLAIM_MAX_DISTANCE_FACTOR: float = _env_float("BARRIER_RECLAIM_MAX_DISTANCE_FACTOR", 3.50)
    BARRIER_RECLAIM_MIN_RETRACE_RATIO: float = _env_float("BARRIER_RECLAIM_MIN_RETRACE_RATIO", 0.20)
    BARRIER_RECLAIM_MAX_ENTRIES_PER_MARKET: int = _env_int("BARRIER_RECLAIM_MAX_ENTRIES_PER_MARKET", 2)
    # v14.2.39: data-derived guardrails from Shadow results.
    # Do not treat the 56%-60% barrier_reclaim confidence as a calibrated probability.
    # Current Shadow audit showed entry < 0.08 and model/market gap >= 0.50 were loss-only buckets.
    BARRIER_RECLAIM_GUARDRAILS_ENABLED: bool = _env_bool("BARRIER_RECLAIM_GUARDRAILS_ENABLED", True)
    BARRIER_RECLAIM_MIN_ENTRY_PRICE_GUARD: float = _env_float("BARRIER_RECLAIM_MIN_ENTRY_PRICE_GUARD", 0.10)
    BARRIER_RECLAIM_MAX_ENTRY_PRICE_GUARD: float = _env_float("BARRIER_RECLAIM_MAX_ENTRY_PRICE_GUARD", 0.50)
    BARRIER_RECLAIM_MIN_MODEL_PROB_GUARD: float = _env_float("BARRIER_RECLAIM_MIN_MODEL_PROB_GUARD", 0.75)
    BARRIER_RECLAIM_MAX_MODEL_MARKET_GAP_GUARD: float = _env_float("BARRIER_RECLAIM_MAX_MODEL_MARKET_GAP_GUARD", 0.50)
    REAL_ALLOWED_STRATEGIES: str = _env_str("REAL_ALLOWED_STRATEGIES", "barrier_reclaim")
    REAL_MAX_OPEN_TRADES_GUARD: int = _env_int("REAL_MAX_OPEN_TRADES_GUARD", 1)
    REAL_LOSS_STREAK_PAUSE: int = _env_int("REAL_LOSS_STREAK_PAUSE", 3)

    LOTTERY_MODE: bool = _env_bool("LOTTERY_MODE", True)
    LOTTERY_BET_SIZE: float = _env_float("LOTTERY_BET_SIZE", 1.0)
    LOTTERY_MAX_TRADES_PER_DAY: int = _env_int("LOTTERY_MAX_TRADES_PER_DAY", 5)  # currently documented cap; global caps still apply
    LOTTERY_MAX_DAILY_LOSS_USD: float = _env_float("LOTTERY_MAX_DAILY_LOSS_USD", 5.0)  # documented cap; global caps still apply
    LOTTERY_MIN_SECONDS_LEFT: int = _env_int("LOTTERY_MIN_SECONDS_LEFT", 8)
    LOTTERY_MAX_SECONDS_LEFT: int = _env_int("LOTTERY_MAX_SECONDS_LEFT", 55)
    LOTTERY_MIN_MODEL_PROB: float = _env_float("LOTTERY_MIN_MODEL_PROB", 0.045)
    LOTTERY_MAX_MODEL_PROB: float = _env_float("LOTTERY_MAX_MODEL_PROB", 0.30)
    LOTTERY_MIN_EDGE_AFTER_FEES: float = _env_float("LOTTERY_MIN_EDGE_AFTER_FEES", 0.010)
    LOTTERY_MIN_TOKEN_PRICE: float = _env_float("LOTTERY_MIN_TOKEN_PRICE", 0.01)
    LOTTERY_MAX_TOKEN_PRICE: float = _env_float("LOTTERY_MAX_TOKEN_PRICE", 0.07)
    LOTTERY_MAX_SPREAD: float = _env_float("LOTTERY_MAX_SPREAD", 0.035)
    LOTTERY_MAX_MARKET_PROB_GAP: float = _env_float("LOTTERY_MAX_MARKET_PROB_GAP", 0.18)
    LOTTERY_MARKET_CONSENSUS_WEIGHT: float = _env_float("LOTTERY_MARKET_CONSENSUS_WEIGHT", 0.0)
    LOTTERY_MIN_LIQUIDITY_MULTIPLIER: float = _env_float("LOTTERY_MIN_LIQUIDITY_MULTIPLIER", 1.0)
    LOTTERY_MIN_LINE_CLOSING_FACTOR: float = _env_float("LOTTERY_MIN_LINE_CLOSING_FACTOR", 0.18)
    LOTTERY_MIN_LINE_CLOSING_SIGMA: float = _env_float("LOTTERY_MIN_LINE_CLOSING_SIGMA", 0.10)

    # Odds EV: high-frequency expected-value filter for cheap outcome tokens.
    ODDS_EV_ENABLED: bool = _env_bool("ODDS_EV_ENABLED", True)
    ODDS_EV_BET_SIZE: float = _env_float("ODDS_EV_BET_SIZE", 1.0)
    ODDS_EV_MIN_SECONDS_LEFT: int = _env_int("ODDS_EV_MIN_SECONDS_LEFT", 25)
    ODDS_EV_MAX_SECONDS_LEFT: int = _env_int("ODDS_EV_MAX_SECONDS_LEFT", 255)
    ODDS_EV_MIN_TOKEN_PRICE: float = _env_float("ODDS_EV_MIN_TOKEN_PRICE", 0.02)
    ODDS_EV_MAX_TOKEN_PRICE: float = _env_float("ODDS_EV_MAX_TOKEN_PRICE", 0.35)
    ODDS_EV_MIN_MODEL_PROB: float = _env_float("ODDS_EV_MIN_MODEL_PROB", 0.035)
    ODDS_EV_MIN_EXPECTED_VALUE: float = _env_float("ODDS_EV_MIN_EXPECTED_VALUE", 0.12)
    ODDS_EV_MAX_SPREAD: float = _env_float("ODDS_EV_MAX_SPREAD", 0.060)
    ODDS_EV_MAX_MARKET_PROB_GAP: float = _env_float("ODDS_EV_MAX_MARKET_PROB_GAP", 0.35)
    ODDS_EV_MARKET_CONSENSUS_WEIGHT: float = _env_float("ODDS_EV_MARKET_CONSENSUS_WEIGHT", 0.03)
    ODDS_EV_MIN_LIQUIDITY_MULTIPLIER: float = _env_float("ODDS_EV_MIN_LIQUIDITY_MULTIPLIER", 1.0)

    # Odds lag: external price has started moving back toward Price-to-Beat,
    # but Polymarket odds have not caught up yet. Uses locally recorded market_snapshots.
    ODDS_LAG_ENABLED: bool = _env_bool("ODDS_LAG_ENABLED", True)
    ODDS_LAG_BET_SIZE: float = _env_float("ODDS_LAG_BET_SIZE", 1.0)
    ODDS_LAG_MIN_SECONDS_LEFT: int = _env_int("ODDS_LAG_MIN_SECONDS_LEFT", 20)
    ODDS_LAG_MAX_SECONDS_LEFT: int = _env_int("ODDS_LAG_MAX_SECONDS_LEFT", 250)
    ODDS_LAG_MIN_TOKEN_PRICE: float = _env_float("ODDS_LAG_MIN_TOKEN_PRICE", 0.03)
    ODDS_LAG_MAX_TOKEN_PRICE: float = _env_float("ODDS_LAG_MAX_TOKEN_PRICE", 0.35)
    ODDS_LAG_MIN_MODEL_PROB: float = _env_float("ODDS_LAG_MIN_MODEL_PROB", 0.05)
    ODDS_LAG_MIN_EXPECTED_VALUE: float = _env_float("ODDS_LAG_MIN_EXPECTED_VALUE", 0.10)
    ODDS_LAG_MAX_SPREAD: float = _env_float("ODDS_LAG_MAX_SPREAD", 0.060)
    ODDS_LAG_MAX_MARKET_PROB_GAP: float = _env_float("ODDS_LAG_MAX_MARKET_PROB_GAP", 0.34)
    ODDS_LAG_MARKET_CONSENSUS_WEIGHT: float = _env_float("ODDS_LAG_MARKET_CONSENSUS_WEIGHT", 0.02)
    ODDS_LAG_MIN_LIQUIDITY_MULTIPLIER: float = _env_float("ODDS_LAG_MIN_LIQUIDITY_MULTIPLIER", 1.0)
    ODDS_LAG_MIN_SNAPSHOT_COUNT: int = _env_int("ODDS_LAG_MIN_SNAPSHOT_COUNT", 2)
    ODDS_LAG_MIN_PRICE_MOVE_TO_LINE: float = _env_float("ODDS_LAG_MIN_PRICE_MOVE_TO_LINE", 0.10)
    ODDS_LAG_MAX_ASK_RISE_PCT: float = _env_float("ODDS_LAG_MAX_ASK_RISE_PCT", 0.35)
    ODDS_LAG_MAX_SNAPSHOT_AGE_SEC: int = _env_int("ODDS_LAG_MAX_SNAPSHOT_AGE_SEC", 45)

    # Wick rejection odds: long upper/lower tail + cheap opposite side + line-closing velocity.
    WICK_REJECTION_ENABLED: bool = _env_bool("WICK_REJECTION_ENABLED", True)
    WICK_BET_SIZE: float = _env_float("WICK_BET_SIZE", 1.0)
    WICK_MIN_SECONDS_LEFT: int = _env_int("WICK_MIN_SECONDS_LEFT", 25)
    WICK_MAX_SECONDS_LEFT: int = _env_int("WICK_MAX_SECONDS_LEFT", 245)
    WICK_MIN_TOKEN_PRICE: float = _env_float("WICK_MIN_TOKEN_PRICE", 0.03)
    WICK_MAX_TOKEN_PRICE: float = _env_float("WICK_MAX_TOKEN_PRICE", 0.30)
    WICK_MIN_MODEL_PROB: float = _env_float("WICK_MIN_MODEL_PROB", 0.06)
    WICK_MIN_EDGE_AFTER_FEES: float = _env_float("WICK_MIN_EDGE_AFTER_FEES", 0.012)
    WICK_MAX_SPREAD: float = _env_float("WICK_MAX_SPREAD", 0.055)
    WICK_MAX_MARKET_PROB_GAP: float = _env_float("WICK_MAX_MARKET_PROB_GAP", 0.28)
    WICK_MARKET_CONSENSUS_WEIGHT: float = _env_float("WICK_MARKET_CONSENSUS_WEIGHT", 0.04)
    WICK_MIN_LIQUIDITY_MULTIPLIER: float = _env_float("WICK_MIN_LIQUIDITY_MULTIPLIER", 1.0)
    WICK_MIN_WICK_BODY_RATIO: float = _env_float("WICK_MIN_WICK_BODY_RATIO", 1.30)
    WICK_MIN_WICK_RANGE_RATIO: float = _env_float("WICK_MIN_WICK_RANGE_RATIO", 0.35)
    WICK_MIN_LINE_CLOSING_FACTOR: float = _env_float("WICK_MIN_LINE_CLOSING_FACTOR", 0.10)
    WICK_MIN_LINE_CLOSING_SIGMA: float = _env_float("WICK_MIN_LINE_CLOSING_SIGMA", 0.06)

    # Intelligent post-trade review / strategy tuning.
    ENABLE_INTELLIGENT_REVIEW: bool = _env_bool("ENABLE_INTELLIGENT_REVIEW", True)
    REVIEW_MIN_SAMPLES_FOR_WEIGHT: int = _env_int("REVIEW_MIN_SAMPLES_FOR_WEIGHT", 20)
    REVIEW_WEIGHT_MIN: float = _env_float("REVIEW_WEIGHT_MIN", 0.65)
    REVIEW_WEIGHT_MAX: float = _env_float("REVIEW_WEIGHT_MAX", 1.18)
    REVIEW_TARGET_EV_PNL: float = _env_float("REVIEW_TARGET_EV_PNL", 0.0)

    # ============ Entry Timing ============
    ENTRY_MIN_SECONDS_LEFT: int = _env_int("ENTRY_MIN_SECONDS_LEFT", 5)
    ENTRY_MAX_SECONDS_LEFT: int = _env_int("ENTRY_MAX_SECONDS_LEFT", 150)

    # ============ Token Price Filter ============
    MIN_TOKEN_PRICE: float = _env_float("MIN_TOKEN_PRICE", 0.25)
    MAX_TOKEN_PRICE: float = _env_float("MAX_TOKEN_PRICE", 0.90)

    # V14.2.20: allow genuinely high-probability, high-edge signals to pass
    # strategy-specific token range filters when the token is not extremely
    # expensive. This is for $1 small-live orders. It does not bypass book,
    # spread, edge, quality gate, balance, capital isolation or order checks.
    HIGH_CONFIDENCE_TOKEN_RANGE_OVERRIDE_ENABLED: bool = _env_bool("HIGH_CONFIDENCE_TOKEN_RANGE_OVERRIDE_ENABLED", True)
    HIGH_CONFIDENCE_TOKEN_RANGE_MIN_PROB: float = _env_float("HIGH_CONFIDENCE_TOKEN_RANGE_MIN_PROB", 0.90)
    HIGH_CONFIDENCE_TOKEN_RANGE_MIN_EDGE: float = _env_float("HIGH_CONFIDENCE_TOKEN_RANGE_MIN_EDGE", 0.060)
    HIGH_CONFIDENCE_TOKEN_RANGE_MAX_TOKEN_PRICE: float = _env_float("HIGH_CONFIDENCE_TOKEN_RANGE_MAX_TOKEN_PRICE", 0.92)

    # ============ Orderbook / fee / execution filters ============
    CRYPTO_TAKER_FEE_RATE: float = _env_float("CRYPTO_TAKER_FEE_RATE", 0.07)
    MAX_SPREAD: float = _env_float("MAX_SPREAD", 0.06)
    TARGET_SPREAD_MAX: float = _env_float("TARGET_SPREAD_MAX", 0.06)
    MAX_MARKET_PROB_GAP: float = _env_float("MAX_MARKET_PROB_GAP", 0.28)
    MARKET_CONSENSUS_WEIGHT: float = _env_float("MARKET_CONSENSUS_WEIGHT", 0.22)
    MIN_LIQUIDITY_MULTIPLIER: float = _env_float("MIN_LIQUIDITY_MULTIPLIER", 1.20)
    MARKET_BUY_SLIPPAGE: float = _env_float("MARKET_BUY_SLIPPAGE", 0.035)
    EXECUTION_GUARD_SLIPPAGE_CAP: float = _env_float("EXECUTION_GUARD_SLIPPAGE_CAP", MARKET_BUY_SLIPPAGE)
    ENABLE_BOOK_SANITY_CHECK: bool = _env_bool("ENABLE_BOOK_SANITY_CHECK", True)
    MIN_BOOK_ASK_SUM: float = _env_float("MIN_BOOK_ASK_SUM", 0.92)
    MAX_BOOK_ASK_SUM: float = _env_float("MAX_BOOK_ASK_SUM", 1.10)
    MAX_OPPOSITE_SPREAD: float = _env_float("MAX_OPPOSITE_SPREAD", 0.08)
    ORDER_TYPE: str = os.getenv("ORDER_TYPE", "FOK").upper()

    # ============ V14.2 execution quality guard ============
    # Public-data-only final check before dry-run/live submission. Does not touch API/balance.
    EXECUTION_RECHECK_ENABLED: bool = _env_bool("EXECUTION_RECHECK_ENABLED", True)
    EXECUTION_GUARD_PRICE_JUMP_CAP: float = _env_float("EXECUTION_GUARD_PRICE_JUMP_CAP", 0.025)
    EXECUTION_MAX_PRICE_MOVE: float = _env_float("EXECUTION_MAX_PRICE_MOVE", EXECUTION_GUARD_PRICE_JUMP_CAP)
    EXECUTION_MAX_BOOK_AGE_SEC: float = _env_float("EXECUTION_MAX_BOOK_AGE_SEC", 2.5)
    EXECUTION_FLIGHT_RECORDER_ENABLED: bool = _env_bool("EXECUTION_FLIGHT_RECORDER_ENABLED", True)
    DYNAMIC_TICK_GUARD_ENABLED: bool = _env_bool("DYNAMIC_TICK_GUARD_ENABLED", True)
    DYNAMIC_TICK_MAX_AGE_SEC: float = _env_float("DYNAMIC_TICK_MAX_AGE_SEC", 30.0)

    # Active self-diagnosis and Telegram health reporting.
    PRODUCT_DOCTOR_ENABLED: bool = _env_bool("PRODUCT_DOCTOR_ENABLED", True)
    PRODUCT_DOCTOR_INTERVAL_SEC: int = _env_int("PRODUCT_DOCTOR_INTERVAL_SEC", 900)
    PRODUCT_DOCTOR_LOOKBACK_SEC: int = _env_int("PRODUCT_DOCTOR_LOOKBACK_SEC", 3600)
    PRODUCT_DOCTOR_PUSH_WARNINGS: bool = _env_bool("PRODUCT_DOCTOR_PUSH_WARNINGS", True)
    PRODUCT_DOCTOR_HIGH_CANDIDATES_PER_MIN: float = _env_float("PRODUCT_DOCTOR_HIGH_CANDIDATES_PER_MIN", 3.0)
    PRODUCT_DOCTOR_HIGH_PASS_RATIO: float = _env_float("PRODUCT_DOCTOR_HIGH_PASS_RATIO", 0.40)
    PRODUCT_DOCTOR_LOW_PASS_RATIO: float = _env_float("PRODUCT_DOCTOR_LOW_PASS_RATIO", 0.02)
    PRODUCT_DOCTOR_SKIP_CLUSTER_MIN: int = _env_int("PRODUCT_DOCTOR_SKIP_CLUSTER_MIN", 12)
    PRODUCT_DOCTOR_EXECUTION_ISSUE_MIN: int = _env_int("PRODUCT_DOCTOR_EXECUTION_ISSUE_MIN", 2)
    WS_HEALTH_MAX_MESSAGE_AGE_SEC: float = _env_float("WS_HEALTH_MAX_MESSAGE_AGE_SEC", 15.0)

    CONFIRM_DELAYED_ORDER_SEC: float = _env_float("CONFIRM_DELAYED_ORDER_SEC", 1.5)
    ORDER_ATTEMPT_LOCK_SEC: int = _env_int("ORDER_ATTEMPT_LOCK_SEC", 45)
    FAILED_ORDER_RETRY_LOCK_SEC: int = _env_int("FAILED_ORDER_RETRY_LOCK_SEC", 15)
    SUBMITTING_ORDER_LOCK_SEC: int = _env_int("SUBMITTING_ORDER_LOCK_SEC", 20)
    NOT_MATCHED_ORDER_RETRY_LOCK_SEC: int = _env_int("NOT_MATCHED_ORDER_RETRY_LOCK_SEC", 45)
    # v14.2.24: protect against SDK/network hangs after execution_recheck_pass.
    # If post_order does not return inside this window, mark the lifecycle as failed/unknown
    # instead of leaving the order stuck at attempted/submitting forever.
    ORDER_SUBMIT_TIMEOUT_SEC: int = _env_int("ORDER_SUBMIT_TIMEOUT_SEC", 8)
    SUBMIT_TIMEOUT_UNKNOWN_LOCK_SEC: int = _env_int("SUBMIT_TIMEOUT_UNKNOWN_LOCK_SEC", 75)
    STORE_API_CREDS: bool = _env_bool("STORE_API_CREDS", False)
    CANCEL_OPEN_ORDERS_ON_START: bool = _env_bool("CANCEL_OPEN_ORDERS_ON_START", True)

    # v14.2.34: Python CLOB V2 currently rejects Deposit Wallet/POLY_1271
    # submits with signer/API-key mismatch on some accounts. Keep balances/auth
    # working with signature_type=3, but block real submit by default and record
    # a shadow trade after all real quality/execution gates pass.
    CLOB_V2_REQUIRE_SDK: bool = _env_bool("CLOB_V2_REQUIRE_SDK", True)
    CLOB_V2_SIG3_REAL_SUBMIT_ENABLED: bool = _env_bool("CLOB_V2_SIG3_REAL_SUBMIT_ENABLED", False)
    SHADOW_TRADING_ENABLED: bool = _env_bool("SHADOW_TRADING_ENABLED", True)
    SHADOW_TRADING_ON_SIGNER_GUARD: bool = _env_bool("SHADOW_TRADING_ON_SIGNER_GUARD", True)
    SHADOW_DEDUP_ENABLED: bool = _env_bool("SHADOW_DEDUP_ENABLED", True)
    SHADOW_SETTLEMENT_BUFFER_SEC: int = _env_int("SHADOW_SETTLEMENT_BUFFER_SEC", 10)
    SHADOW_SETTLEMENT_FALLBACK_AFTER_SEC: int = _env_int("SHADOW_SETTLEMENT_FALLBACK_AFTER_SEC", 120)
    SHADOW_SETTLEMENT_MAX_PER_CYCLE: int = _env_int("SHADOW_SETTLEMENT_MAX_PER_CYCLE", 25)
    # v14.2.39: while real samples are still sparse, allow Shadow results to drive initial weights.
    STRATEGY_WEIGHT_USE_SHADOW_DATA: bool = _env_bool("STRATEGY_WEIGHT_USE_SHADOW_DATA", True)

    # ============ Adaptive quality gate / real-time stream screening ============
    # V14 does not pre-select a static "top N" list. Opportunities arrive over the day,
    # so every candidate is scored at arrival time and the threshold is raised/lowered
    # from live flow, PnL, loss streak and trade pace.
    QUALITY_GATE_ENABLED: bool = _env_bool("QUALITY_GATE_ENABLED", True)
    BASE_QUALITY_SCORE: float = _env_float("BASE_QUALITY_SCORE", 75.0)
    MIN_QUALITY_SCORE: float = _env_float("MIN_QUALITY_SCORE", 65.0)
    MAX_QUALITY_SCORE: float = _env_float("MAX_QUALITY_SCORE", 92.0)
    TARGET_TRADES_PER_HOUR: float = _env_float("TARGET_TRADES_PER_HOUR", 6.25)
    TARGET_TRADES_PER_4H: int = _env_int("TARGET_TRADES_PER_4H", 25)
    QUALITY_AHEAD_PENALTY: float = _env_float("QUALITY_AHEAD_PENALTY", 7.0)
    QUALITY_BEHIND_RELAX: float = _env_float("QUALITY_BEHIND_RELAX", 5.0)
    QUALITY_DAILY_LOSS_PENALTY: float = _env_float("QUALITY_DAILY_LOSS_PENALTY", 8.0)
    QUALITY_LOSS_STREAK_PENALTY: float = _env_float("QUALITY_LOSS_STREAK_PENALTY", 4.0)
    QUALITY_WINNING_BONUS: float = _env_float("QUALITY_WINNING_BONUS", 2.0)

    # V14 stream-pressure gate. A day can contain thousands of opportunities; the bot
    # cannot know the whole-day top set ahead of time. Instead it monitors recent
    # candidate pressure and becomes stricter only when the stream is crowded.
    OPPORTUNITY_FLOW_GATE_ENABLED: bool = _env_bool("OPPORTUNITY_FLOW_GATE_ENABLED", True)
    FLOW_LOOKBACK_SEC: int = _env_int("FLOW_LOOKBACK_SEC", 3600)
    FLOW_MIN_RECENT_DECISIONS: int = _env_int("FLOW_MIN_RECENT_DECISIONS", 24)
    FLOW_HIGH_CANDIDATES_PER_MIN: float = _env_float("FLOW_HIGH_CANDIDATES_PER_MIN", 1.50)
    FLOW_VERY_HIGH_CANDIDATES_PER_MIN: float = _env_float("FLOW_VERY_HIGH_CANDIDATES_PER_MIN", 4.00)
    FLOW_PRESSURE_MAX_PENALTY: float = _env_float("FLOW_PRESSURE_MAX_PENALTY", 8.0)
    FLOW_PASS_RATIO_TARGET: float = _env_float("FLOW_PASS_RATIO_TARGET", 0.18)
    FLOW_PASS_RATIO_MAX_PENALTY: float = _env_float("FLOW_PASS_RATIO_MAX_PENALTY", 4.0)
    FLOW_SCORE_PERCENTILE_WEIGHT: float = _env_float("FLOW_SCORE_PERCENTILE_WEIGHT", 0.35)

    # Hard execution-quality filters used by the quality gate. Existing Trader filters still run afterwards.
    QUALITY_MAX_SPREAD: float = _env_float("QUALITY_MAX_SPREAD", 0.080)
    QUALITY_MIN_DEPTH_USD: float = _env_float("QUALITY_MIN_DEPTH_USD", 1.0)
    QUALITY_MIN_BOOK_ASK_SUM: float = _env_float("QUALITY_MIN_BOOK_ASK_SUM", 0.75)
    QUALITY_MAX_BOOK_ASK_SUM: float = _env_float("QUALITY_MAX_BOOK_ASK_SUM", 1.30)
    QUALITY_MAX_PRICE_IMPACT: float = _env_float("QUALITY_MAX_PRICE_IMPACT", 0.035)
    QUALITY_MIN_EXPECTED_VALUE: float = _env_float("QUALITY_MIN_EXPECTED_VALUE", 0.12)
    QUALITY_MAIN_MIN_WIN_PROB: float = _env_float("QUALITY_MAIN_MIN_WIN_PROB", 0.62)
    QUALITY_ODDS_MIN_MODEL_PROB: float = _env_float("QUALITY_ODDS_MIN_MODEL_PROB", 0.06)
    QUALITY_BOOK_AGE_WARN_SEC: int = _env_int("QUALITY_BOOK_AGE_WARN_SEC", 6)

    # Strategy auto-weight / pause. This only gates new entries; it never changes credentials or balances.
    AUTO_STRATEGY_WEIGHTING: bool = _env_bool("AUTO_STRATEGY_WEIGHTING", True)
    STRATEGY_MIN_SAMPLES_FOR_GATE: int = _env_int("STRATEGY_MIN_SAMPLES_FOR_GATE", 20)
    STRATEGY_PAUSE_LOSS_STREAK: int = _env_int("STRATEGY_PAUSE_LOSS_STREAK", 8)
    STRATEGY_PAUSE_HOURS: int = _env_int("STRATEGY_PAUSE_HOURS", 6)
    STRATEGY_MIN_RECENT_WIN_RATE: float = _env_float("STRATEGY_MIN_RECENT_WIN_RATE", 0.20)

    # Optional Polymarket public market WebSocket. This is market-data only: no API creds,
    # no balances, no user channel, no order status. REST/CLOB summaries remain fallback.
    REALTIME_ORDERBOOK_ENABLED: bool = _env_bool("REALTIME_ORDERBOOK_ENABLED", True)
    REALTIME_ORDERBOOK_WS_URL: str = os.getenv("REALTIME_ORDERBOOK_WS_URL", "wss://ws-subscriptions-clob.polymarket.com/ws/market")
    REALTIME_ORDERBOOK_MAX_AGE_SEC: float = _env_float("REALTIME_ORDERBOOK_MAX_AGE_SEC", 3.0)
    REALTIME_ORDERBOOK_WARMUP_SEC: float = _env_float("REALTIME_ORDERBOOK_WARMUP_SEC", 0.35)
    REALTIME_ORDERBOOK_PING_SEC: float = _env_float("REALTIME_ORDERBOOK_PING_SEC", 10.0)
    REALTIME_ORDERBOOK_RECONNECT_MAX_SEC: float = _env_float("REALTIME_ORDERBOOK_RECONNECT_MAX_SEC", 30.0)
    REALTIME_ORDERBOOK_MAX_SUBSCRIPTIONS: int = _env_int("REALTIME_ORDERBOOK_MAX_SUBSCRIPTIONS", 200)
    REALTIME_ORDERBOOK_CLEANUP_INTERVAL_SEC: int = _env_int("REALTIME_ORDERBOOK_CLEANUP_INTERVAL_SEC", 600)
    MARKET_EVENT_COALESCE_MS: int = _env_int("MARKET_EVENT_COALESCE_MS", 800)
    ONE_INFLIGHT_DECISION_PER_MARKET: bool = _env_bool("ONE_INFLIGHT_DECISION_PER_MARKET", True)

    # CLOB constraints. Market BUY uses dollar amount; fallback limit orders need enough shares.
    MIN_MARKET_ORDER_USD: float = _env_float("MIN_MARKET_ORDER_USD", 1.0)
    MIN_LIMIT_ORDER_SHARES: float = _env_float("MIN_LIMIT_ORDER_SHARES", 5.0)
    TICK_SIZE: float = _env_float("TICK_SIZE", 0.01)

    # ============ Learning guardrails ============
    MIN_SAMPLES_FOR_LEARNING: int = _env_int("MIN_SAMPLES_FOR_LEARNING", 20)
    LEARNING_WEIGHT_MIN: float = _env_float("LEARNING_WEIGHT_MIN", 0.80)
    LEARNING_WEIGHT_MAX: float = _env_float("LEARNING_WEIGHT_MAX", 1.10)
    LEARNING_TARGET_WIN_RATE: float = _env_float("LEARNING_TARGET_WIN_RATE", 0.62)
    MIN_SAMPLES_FOR_CALIBRATION: int = _env_int("MIN_SAMPLES_FOR_CALIBRATION", 40)
    CALIBRATION_STRENGTH: float = _env_float("CALIBRATION_STRENGTH", 0.35)
    CALIBRATION_BUCKET_SIZE: float = _env_float("CALIBRATION_BUCKET_SIZE", 0.05)

    # ============ Public-data recorder / big-data layer ============
    ENABLE_DATA_RECORDER: bool = _env_bool("ENABLE_DATA_RECORDER", True)
    DATA_SNAPSHOT_INTERVAL_SEC: int = _env_int("DATA_SNAPSHOT_INTERVAL_SEC", 3)
    DATA_FETCH_TRADES: bool = _env_bool("DATA_FETCH_TRADES", True)
    DATA_FETCH_PRICE_HISTORY: bool = _env_bool("DATA_FETCH_PRICE_HISTORY", True)
    DATA_KEEP_DAYS: int = _env_int("DATA_KEEP_DAYS", 3)
    DATA_PRUNE_INTERVAL_HOURS: int = _env_int("DATA_PRUNE_INTERVAL_HOURS", 6)
    PRUNE_DECISION_LOGS: bool = _env_bool("PRUNE_DECISION_LOGS", True)
    PRUNE_EXECUTION_EVENTS: bool = _env_bool("PRUNE_EXECUTION_EVENTS", True)
    ENABLE_DECISION_LOGS: bool = _env_bool("ENABLE_DECISION_LOGS", True)
    OBSERVATION_LOG_INTERVAL_SEC: int = _env_int("OBSERVATION_LOG_INTERVAL_SEC", 20)

    # ============ Loops ============
    MAIN_LOOP_INTERVAL_SEC: int = _env_int("MAIN_LOOP_INTERVAL_SEC", 3)
    HEARTBEAT_INTERVAL_SEC: int = _env_int("HEARTBEAT_INTERVAL_SEC", 4)

    # ============ Database ============
    DB_PATH: str = os.getenv("DB_PATH", "btc_bot.db")

    def __post_init__(self):
        self.MODE = _normalize_mode(self.MODE_RAW)
        # Paper/observer modes are always non-trading, even if .env accidentally says
        # DRY_RUN=false and REAL_TRADING_ENABLED=true.
        if self.MODE == "paper" or self.OBSERVER_ONLY:
            self.DRY_RUN = True
            self.REAL_TRADING_ENABLED = False

    @property
    def is_paper(self) -> bool:
        return self.MODE == "paper"

    @property
    def is_small_live(self) -> bool:
        return self.MODE == "small_live"

    @property
    def is_live(self) -> bool:
        return self.MODE == "live"

    @property
    def effective_polymarket_private_key(self) -> str:
        """Runtime Polymarket signer key. Telegram/SQLite wins over .env."""
        value = self._state_get_first([
            "polymarket_private_key", "POLYMARKET_PRIVATE_KEY",
            "runtime_polymarket_private_key",
        ], None)
        return str(value if value is not None else self.POLYMARKET_PRIVATE_KEY or "").strip()

    @property
    def effective_polymarket_funder(self) -> str:
        """Runtime Polymarket funder/deposit wallet address. Telegram/SQLite wins over .env."""
        value = self._state_get_first([
            "polymarket_funder", "POLYMARKET_FUNDER",
            "runtime_polymarket_funder",
        ], None)
        return str(value if value is not None else self.POLYMARKET_FUNDER or "").strip()

    @property
    def effective_polymarket_signature_type(self) -> int:
        """Runtime signature type. Telegram/SQLite wins over .env."""
        value = self._state_get_first([
            "polymarket_signature_type", "POLYMARKET_SIGNATURE_TYPE",
            "runtime_polymarket_signature_type",
        ], None)
        if value is None:
            return int(self.POLYMARKET_SIGNATURE_TYPE)
        try:
            return int(value)
        except Exception:
            return int(self.POLYMARKET_SIGNATURE_TYPE)

    @property
    def has_polymarket_creds(self) -> bool:
        return (not _is_placeholder_secret(self.effective_polymarket_private_key)) and (not _is_placeholder_secret(self.effective_polymarket_funder))

    @property
    def has_telegram_creds(self) -> bool:
        return (not _is_placeholder_secret(self.TG_BOT_TOKEN)) and int(self.TG_USER_ID or 0) > 0

    @property
    def real_orders_enabled(self) -> bool:
        return (
            bool(self.REAL_TRADING_ENABLED)
            and (not self.DRY_RUN)
            and (not self.OBSERVER_ONLY)
            and self.MODE in {"small_live", "live"}
        )

    @property
    def auth_dry_run_enabled(self) -> bool:
        """Credentials are loaded and checked, but orders are blocked by DRY_RUN."""
        return self.DRY_RUN and (not self.OBSERVER_ONLY) and self.has_polymarket_creds

    @property
    def mode_display(self) -> str:
        base = {"paper": "paper/no-orders", "small_live": "small_live/real-small", "live": "live/real"}.get(self.MODE, self.MODE)
        if self.auth_dry_run_enabled:
            return base + "/auth-dry-run"
        return base

    @property
    def assets_enabled(self) -> list:
        assets = _env_csv("ASSETS_ENABLED", self.ASSETS_ENABLED)
        allowed = {"BTC", "ETH", "SOL", "XRP"}
        return [a for a in assets if a in allowed] or ["BTC"]

    @property
    def timeframes_enabled(self) -> list:
        raw = os.getenv("TIMEFRAMES_ENABLED", self.TIMEFRAMES_ENABLED)
        out = []
        for x in str(raw).split(","):
            t = x.strip().lower().replace(" ", "")
            if t in {"5", "5m", "5min"}:
                t = "5m"
            elif t in {"15", "15m", "15min"}:
                t = "15m"
            else:
                continue
            if t not in out:
                out.append(t)
        return out or ["5m"]

    def timeframe_seconds(self, timeframe: str = "5m") -> int:
        t = str(timeframe or "5m").strip().lower()
        if t in {"15", "15m", "15min"}:
            return 900
        return 300

    def timeframe_slug(self, timeframe: str = "5m") -> str:
        return "15m" if self.timeframe_seconds(timeframe) == 900 else "5m"

    def strategy_time_bounds(self, base_min: int, base_max: int, timeframe: str = "5m") -> tuple:
        sec = self.timeframe_seconds(timeframe)
        if sec <= 300:
            return int(base_min), int(base_max)
        factor = sec / 300.0
        return int(base_min), min(sec - 5, int(base_max * factor))

    @property
    def ASSET_BINANCE_SYMBOLS(self) -> dict:
        return {"BTC": self.BTC_BINANCE_SYMBOL, "ETH": self.ETH_BINANCE_SYMBOL, "SOL": self.SOL_BINANCE_SYMBOL, "XRP": self.XRP_BINANCE_SYMBOL}

    @property
    def ASSET_SLUG_PREFIXES(self) -> dict:
        return {"BTC": self.BTC_SLUG_PREFIX, "ETH": self.ETH_SLUG_PREFIX, "SOL": self.SOL_SLUG_PREFIX, "XRP": self.XRP_SLUG_PREFIX}

    @property
    def ASSET_BANKROLL_ALLOCATION(self) -> dict:
        return {"BTC": self.BTC_BANKROLL_ALLOCATION, "ETH": self.ETH_BANKROLL_ALLOCATION, "SOL": self.SOL_BANKROLL_ALLOCATION, "XRP": self.XRP_BANKROLL_ALLOCATION}

    @property
    def ASSET_PIN_BAR_MIN_RANGE(self) -> dict:
        return {"BTC": self.BTC_PIN_BAR_MIN_RANGE, "ETH": self.ETH_PIN_BAR_MIN_RANGE, "SOL": self.SOL_PIN_BAR_MIN_RANGE, "XRP": self.XRP_PIN_BAR_MIN_RANGE}

    @property
    def ASSET_MIN_PRICE_DISTANCE(self) -> dict:
        return {"BTC": self.BTC_MIN_PRICE_DISTANCE, "ETH": self.ETH_MIN_PRICE_DISTANCE, "SOL": self.SOL_MIN_PRICE_DISTANCE, "XRP": self.XRP_MIN_PRICE_DISTANCE}

    @property
    def ASSET_BLACK_SWAN_THRESHOLD(self) -> dict:
        return {"BTC": self.BTC_BLACK_SWAN, "ETH": self.ETH_BLACK_SWAN, "SOL": self.SOL_BLACK_SWAN, "XRP": self.XRP_BLACK_SWAN}

    def asset_symbol(self, asset: str) -> str:
        return self.ASSET_BINANCE_SYMBOLS.get(str(asset).upper(), "BTCUSDT")

    def asset_slug_prefix(self, asset: str) -> str:
        return self.ASSET_SLUG_PREFIXES.get(str(asset).upper(), str(asset).lower())

    def asset_bankroll_allocation(self, asset: str) -> float:
        return float(self.ASSET_BANKROLL_ALLOCATION.get(str(asset).upper(), 0.0) or 0.0)

    def asset_pin_bar_min_range(self, asset: str) -> float:
        return float(self.ASSET_PIN_BAR_MIN_RANGE.get(str(asset).upper(), self.PIN_BAR_MIN_RANGE_USD) or self.PIN_BAR_MIN_RANGE_USD)

    def asset_min_price_distance(self, asset: str) -> float:
        return float(self.ASSET_MIN_PRICE_DISTANCE.get(str(asset).upper(), self.MIN_PRICE_DISTANCE_USD) or self.MIN_PRICE_DISTANCE_USD)

    def asset_black_swan_threshold(self, asset: str) -> float:
        return float(self.ASSET_BLACK_SWAN_THRESHOLD.get(str(asset).upper(), self.BLACK_SWAN_THRESHOLD) or self.BLACK_SWAN_THRESHOLD)

    # ============ Runtime settings from Telegram / SQLite state ============
    # Telegram changes must win over .env defaults while the bot is running.
    # The aliases below intentionally support both legacy lowercase keys and
    # env-style uppercase keys so older TG panels/scripts continue to work.
    def attach_runtime_state(self, db) -> None:
        self._runtime_state = db

    def _state_get_first(self, keys, default=None):
        db = getattr(self, "_runtime_state", None)
        if db is None:
            return default
        for key in keys:
            try:
                value = db.get_state(key, None)
            except Exception:
                value = None
            if value is not None:
                return value
        return default

    def _runtime_float(self, keys, default: float) -> float:
        value = self._state_get_first(keys, None)
        if value is None:
            return float(default)
        try:
            return float(value)
        except Exception:
            return float(default)

    def _runtime_int(self, keys, default: int) -> int:
        value = self._state_get_first(keys, None)
        if value is None:
            return int(default)
        try:
            return int(value)
        except Exception:
            return int(default)

    @property
    def effective_test_bet_size(self) -> float:
        """Canonical single-order amount used by paper/small_live.

        UI wording must expose only one setting: 每笔金额.  Older builds may
        have persisted duplicate names such as bet_amount/order_amount, so they
        remain read-only legacy aliases here to avoid silently ignoring an
        existing Telegram setting after upgrade.  New writes should use
        test_bet_size only.
        """
        return max(0.0, self._runtime_float([
            "test_bet_size", "TEST_BET_SIZE",
            # legacy aliases: read only, do not show as separate TG settings
            "bet_size", "bet_amount", "order_amount", "per_order_amount",
            "runtime_test_bet_size",
        ], self.TEST_BET_SIZE))

    @property
    def effective_per_order_amount(self) -> float:
        """Alias for readability: 每笔金额 == 投注金额 == single order USD."""
        return self.effective_test_bet_size

    @property
    def effective_authorized_capital_usd(self) -> float:
        """Maximum principal the bot is allowed to put at risk.

        UI wording: 可用本金上限 / 授权本金. 0 disables isolation.
        This is not a profit cap; profits can grow the trading pool, while the
        reserved wallet balance stays protected.
        """
        return max(0.0, self._runtime_float([
            "authorized_capital_usd", "AUTHORIZED_CAPITAL_USD",
            "capital_limit_usd", "usable_capital_usd",
            "trading_capital_usd", "max_capital_usd",
            "runtime_authorized_capital_usd",
        ], self.AUTHORIZED_CAPITAL_USD))

    @property
    def capital_isolation_enabled(self) -> bool:
        return self.effective_authorized_capital_usd > 0

    @property
    def effective_live_min_bet(self) -> float:
        return max(0.0, self._runtime_float(["live_min_bet", "LIVE_MIN_BET", "runtime_live_min_bet"], self.LIVE_MIN_BET))

    @property
    def effective_live_max_bet(self) -> float:
        return max(0.0, self._runtime_float([
            "live_max_bet", "LIVE_MAX_BET", "max_bet", "max_order_amount", "runtime_live_max_bet",
        ], self.LIVE_MAX_BET))

    @property
    def effective_live_max_bet_ratio(self) -> float:
        return max(0.0, self._runtime_float(["live_max_bet_ratio", "LIVE_MAX_BET_RATIO"], self.LIVE_MAX_BET_RATIO))

    @property
    def effective_test_stop_loss_balance(self) -> float:
        return self._runtime_float([
            "test_stop_loss_balance", "TEST_STOP_LOSS_BALANCE",
            "stop_loss_balance", "stop_balance", "loss_stop_balance",
        ], self.TEST_STOP_LOSS_BALANCE)

    @property
    def effective_live_stop_loss_balance(self) -> float:
        return self._runtime_float([
            "live_stop_loss_balance", "LIVE_STOP_LOSS_BALANCE",
            "stop_loss_balance", "stop_balance", "loss_stop_balance",
        ], self.LIVE_STOP_LOSS_BALANCE)

    @property
    def effective_max_daily_loss_usd(self) -> float:
        return max(0.0, self._runtime_float([
            "max_daily_loss_usd", "MAX_DAILY_LOSS_USD", "daily_loss_limit_usd",
            "loss_stop_usd", "max_loss_usd",
        ], self.MAX_DAILY_LOSS_USD))

    @property
    def effective_max_daily_loss_pct(self) -> float:
        return max(0.0, self._runtime_float(["max_daily_loss_pct", "MAX_DAILY_LOSS_PCT"], self.MAX_DAILY_LOSS_PCT))

    @property
    def effective_max_open_trades(self) -> int:
        return max(0, self._runtime_int(["max_open_trades", "MAX_OPEN_TRADES"], self.MAX_OPEN_TRADES))

    @property
    def effective_max_trades_per_day(self) -> int:
        return max(0, self._runtime_int(["max_trades_per_day", "MAX_TRADES_PER_DAY"], self.MAX_TRADES_PER_DAY))

    @property
    def effective_max_order_attempts_per_day(self) -> int:
        return max(0, self._runtime_int(["max_order_attempts_per_day", "MAX_ORDER_ATTEMPTS_PER_DAY"], self.MAX_ORDER_ATTEMPTS_PER_DAY))

    def calc_bet_size(self, confidence: float, balance: float) -> float:
        if self.MODE in {"paper", "small_live"}:
            return min(self.effective_test_bet_size, balance)

        live_min = self.effective_live_min_bet
        live_max = max(live_min, self.effective_live_max_bet)
        scaled = live_min + (live_max - live_min) * \
                 max(0, min(1, (confidence - self.MIN_MODEL_PROB) / (0.90 - self.MIN_MODEL_PROB)))
        cap = balance * self.effective_live_max_bet_ratio
        return max(0.0, min(scaled, cap, live_max))

    def taker_fee_per_share(self, price: float) -> float:
        p = max(0.0, min(1.0, price))
        return self.CRYPTO_TAKER_FEE_RATE * p * (1.0 - p)

    @property
    def stop_loss_balance(self) -> float:
        return self.effective_test_stop_loss_balance if self.MODE in {"paper", "small_live"} else self.effective_live_stop_loss_balance

    @property
    def starting_bankroll(self) -> float:
        return self.TEST_BANKROLL if self.MODE in {"paper", "small_live"} else self.LIVE_BANKROLL


config = Config()
