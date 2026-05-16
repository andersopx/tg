#!/usr/bin/env bash
set -euo pipefail

echo "[audit v15.1.3] checking package safety..."

fail() { echo "[FAIL] $*" >&2; exit 1; }

[[ -f config.py ]] || fail "config.py missing"
[[ -f install_v15_1_3.sh ]] || fail "install_v15_1_3.sh missing"
[[ -f .env.example ]] || fail ".env.example missing"
[[ -f .env.high_win_rate.example ]] || fail ".env.high_win_rate.example missing"

python -m compileall -q .
pytest -q

grep -q 'REAL_TRADING_ENABLED: bool = _env_bool("REAL_TRADING_ENABLED", False)' config.py || fail "REAL_TRADING_ENABLED not defined safely"
grep -q 'bool(self.REAL_TRADING_ENABLED)' config.py || fail "REAL_TRADING_ENABLED not used in real_orders_enabled"
grep -q 'POLYMARKET_REPLAY_ON_START: bool = _env_bool("POLYMARKET_REPLAY_ON_START", False)' config.py || fail "replay default not false"
grep -q 'cp "$APP_DIR/.env.example" "$APP_DIR/.env"' install_v15_1_3.sh || fail "installer must fall back to .env.example for fresh installs"

env_files=(.env.example .env.high_win_rate.example)
if [[ -f .env ]]; then
  env_files+=(.env)
fi
for f in "${env_files[@]}"; do
  grep -q '^DRY_RUN=true$' "$f" || fail "$f must default DRY_RUN=true"
  grep -q '^REAL_TRADING_ENABLED=false$' "$f" || fail "$f must default REAL_TRADING_ENABLED=false"
  grep -q '^CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=false$' "$f" || fail "$f must default sig3 real submit=false"
  grep -q '^POLYMARKET_REPLAY_ON_START=false$' "$f" || fail "$f must default replay=false"
  grep -q '^PRICE_TO_BEAT_FALLBACK_ENABLED=false$' "$f" || fail "$f must default price-to-beat fallback=false"
  grep -q '^SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED=false$' "$f" || fail "$f must default shadow external settlement fallback=false"
  grep -q '^TG_BOT_TOKEN=your_telegram_bot_token_here$' "$f" || fail "$f must contain TG_BOT_TOKEN placeholder"
  grep -q '^TG_USER_ID=123456789$' "$f" || fail "$f must contain TG_USER_ID placeholder"
done

# Exclude .env placeholders from secret scan, but scan all source/scripts for accidental real keys.
if rg -n -I --glob '!*.pyc' --glob '!*.pyo' --glob '!*.md' --glob '!.env' --glob '!.env.example' --glob '!.env.high_win_rate.example' '0x[a-fA-F0-9]{64}|[0-9]{8,}:[A-Za-z0-9_-]{30,}' . >/tmp/btc_bot_v15_1_3_secret_scan.txt; then
  cat /tmp/btc_bot_v15_1_3_secret_scan.txt >&2
  fail "possible hardcoded secret found"
fi

echo "[audit v15.1.3] OK"
