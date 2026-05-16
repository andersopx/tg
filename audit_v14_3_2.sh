#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${1:-$(pwd)}"
cd "$APP_DIR"
echo "=============================="
echo "v14.3.2 runtime audit"
echo "=============================="
PYTHON_BIN="${PYTHON_BIN:-python}"
if [[ -x "./.venv/bin/python" ]]; then
  PYTHON_BIN="./.venv/bin/python"
fi
"$PYTHON_BIN" - <<'PY'
from config import config
keys = [
    'VERSION','MODE','DRY_RUN','ENABLE_TELEGRAM','TG_USER_ID','SHADOW_TRADING_ENABLED',
    'REAL_TRADING_ENABLED','CLOB_V2_SIG3_REAL_SUBMIT_ENABLED',
    'ADAPTIVE_EDGE_ENGINE_ENABLED','HIGH_WIN_RATE_MODE','PROFIT_RULE_ENGINE_ENABLED',
    'PROFIT_RULE_HARD_75_ENABLED','PROFIT_RULE_RELAXED_CAN_TRADE','PROFIT_RULE_NEW_RULE_CAN_SHADOW',
    'PROFIT_RULE_NEW_RULE_REQUIRES_ADAPTIVE_EDGE','PROFIT_RULE_MIN_ENTRY_PRICE','PROFIT_RULE_MAX_ENTRY_PRICE',
]
for k in keys:
    print(k, '=', getattr(config, k, None))
print('TG_BOT_TOKEN_SET=', bool(getattr(config, 'TG_BOT_TOKEN', '')))
PY

echo "=============================="
echo "systemd"
echo "=============================="
if command -v systemctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
  systemctl cat btc-bot --no-pager | grep -E 'WorkingDirectory|ExecStart' || true
  systemctl status btc-bot --no-pager | sed -n '1,25p' || true
else
  echo "SYSTEMD_UNAVAILABLE"
fi

echo "=============================="
echo "db tables/status"
echo "=============================="
DB="btc_bot.db"
if [ -f "$DB" ]; then
  sqlite3 -header -column "$DB" "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name;"
  sqlite3 -header -column "$DB" "SELECT status, COUNT(*) AS n, ROUND(SUM(COALESCE(pnl,0)),2) AS pnl FROM trades GROUP BY status ORDER BY n DESC;" 2>/dev/null || true
else
  echo "DB_NOT_FOUND"
fi

echo "=============================="
echo "recent logs"
echo "=============================="
if command -v journalctl >/dev/null 2>&1 && systemctl is-system-running >/dev/null 2>&1; then
  journalctl -u btc-bot -n 120 --no-pager | grep -E 'Adaptive Edge|Profit Rule|SHADOW|ERROR|Traceback' || true
else
  echo "JOURNAL_UNAVAILABLE"
fi
