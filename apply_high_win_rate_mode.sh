#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f ".env" ]]; then
  cp .env.high_win_rate.example .env
  echo "已创建 .env。请先填写 TG_BOT_TOKEN、TG_USER_ID、POLYMARKET_PRIVATE_KEY、POLYMARKET_FUNDER，然后再次运行。"
  exit 0
fi

backup=".env.backup.$(date +%Y%m%d_%H%M%S)"
cp .env "$backup"
echo "已备份当前 .env -> $backup"

append_missing() {
  local key="$1"
  local value="$2"
  if grep -qE "^${key}=" .env; then
    return 0
  fi
  printf "%s=%s\n" "$key" "$value" >> .env
}

{
  echo ""
  echo "# ============ v15 high-win-rate upgrade settings ============"
} >> .env

append_missing HIGH_WIN_RATE_MODE true
append_missing TARGET_WIN_RATE 0.75
append_missing HIGH_WIN_RATE_MIN_MODEL_PROB 0.74
append_missing HIGH_WIN_RATE_UNKNOWN_STRATEGY_MIN_MODEL_PROB 0.78
append_missing HIGH_WIN_RATE_MIN_EDGE_AFTER_FEES 0.035
append_missing HIGH_WIN_RATE_MAX_TOKEN_PRICE 0.92
append_missing HIGH_WIN_RATE_MIN_RECENT_SAMPLES 8
append_missing HIGH_WIN_RATE_REJECT_RECENT_WIN_RATE 0.50
append_missing HIGH_WIN_RATE_MAX_STRATEGY_LOSS_STREAK 2
append_missing HIGH_WIN_RATE_WILSON_Z 1.2815515655446004
append_missing HIGH_WIN_RATE_MIN_WILSON_LOWER_BOUND 0.55
append_missing HIGH_WIN_RATE_STATS_LOOKBACK 80
append_missing HIGH_WIN_RATE_USE_SHADOW_STATS true

append_missing PROFIT_RULE_ENGINE_ENABLED true
append_missing PROFIT_RULE_LIVE_REQUIRE_PROMOTE true
append_missing PROFIT_RULE_MIN_PROMOTE_WIN_RATE 0.70
append_missing PROFIT_RULE_PROMOTE_REQUIRE_TARGET_WIN_RATE true
append_missing PROFIT_RULE_MIN_WILSON_PROMOTE 0.55
append_missing PROFIT_RULE_MAX_LOSS_STREAK 2
append_missing PROFIT_RULE_FREEZE_MINUTES 60

append_missing WINRATE_GOVERNOR_ENABLED true
append_missing WINRATE_GOVERNOR_MIN_TRADES 8
append_missing WINRATE_GOVERNOR_MIN_WIN_RATE 0.55
append_missing WINRATE_GOVERNOR_INCLUDE_SHADOW false
append_missing DAILY_PROFIT_LOCK_ENABLED true
append_missing DAILY_PROFIT_LOCK_START_USD 3.0
append_missing DAILY_PROFIT_LOCK_GIVEBACK_USD 1.25
append_missing DAILY_PROFIT_LOCK_GIVEBACK_PCT 0.45

append_missing BACKTEST_REQUIRED_BEFORE_LIVE true
append_missing BACKTEST_MIN_WIN_RATE 0.60
append_missing MAX_OPEN_TRADES 1
append_missing MAX_TRADES_PER_DAY 24
append_missing MAX_ORDER_ATTEMPTS_PER_DAY 72
append_missing CONSECUTIVE_LOSS_LIMIT 3
append_missing COOLDOWN_AFTER_LOSSES_SEC 1800
append_missing MIN_MARKET_ORDER_USD 1.0

echo "高胜率目标模式配置已合并。你的原密钥没有被覆盖。"
