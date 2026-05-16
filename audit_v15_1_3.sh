#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python}"
if [[ -x "./.venv/bin/python" ]]; then
  PYTHON_BIN="./.venv/bin/python"
fi

echo "[audit v15.1.3] checking package and server safety..."

fail() { echo "[FAIL] $*" >&2; exit 1; }

[[ -f config.py ]] || fail "config.py missing"
[[ -f install_v15_1_3.sh ]] || fail "install_v15_1_3.sh missing"
[[ -f server_readiness_check.py ]] || fail "server_readiness_check.py missing"
[[ -f btc-bot.service.example ]] || fail "btc-bot.service.example missing"
[[ -f .env.example ]] || fail ".env.example missing"
[[ -f .env.ubuntu2404.example ]] || fail ".env.ubuntu2404.example missing"
[[ -f .env.high_win_rate.example ]] || fail ".env.high_win_rate.example missing"

# Server installs often contain .venv and runtime DB files in the app directory.
# Do not compile third-party packages or runtime state; only check project source.
mapfile -t py_files < <(find . \
  -path './.venv' -prune -o \
  -path './__pycache__' -prune -o \
  -path './.pytest_cache' -prune -o \
  -type f -name '*.py' -print | sort)
"$PYTHON_BIN" -m py_compile "${py_files[@]}"

if "$PYTHON_BIN" -m pytest -q; then
  true
else
  fail "pytest failed; install test dependencies or fix failing tests before server deploy"
fi

grep -q 'REAL_TRADING_ENABLED: bool = _env_bool("REAL_TRADING_ENABLED", False)' config.py || fail "REAL_TRADING_ENABLED not defined safely"
grep -q 'bool(self.REAL_TRADING_ENABLED)' config.py || fail "REAL_TRADING_ENABLED not used in real_orders_enabled"
grep -q 'POLYMARKET_REPLAY_ON_START: bool = _env_bool("POLYMARKET_REPLAY_ON_START", False)' config.py || fail "replay default not false"
grep -q 'cp "$APP_DIR/.env.ubuntu2404.example" "$APP_DIR/.env"' install_v15_1_3.sh || fail "installer must use Ubuntu 24.04 env template for fresh installs"
grep -q 'cp "$APP_DIR/.env.example" "$APP_DIR/.env"' install_v15_1_3.sh || fail "installer must still fall back to .env.example"

# Example templates must stay safe and must contain placeholders.
for f in .env.example .env.high_win_rate.example .env.ubuntu2404.example; do
  grep -q '^DRY_RUN=true$' "$f" || fail "$f must default DRY_RUN=true"
  grep -q '^REAL_TRADING_ENABLED=false$' "$f" || fail "$f must default REAL_TRADING_ENABLED=false"
  grep -q '^CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=false$' "$f" || fail "$f must default sig3 real submit=false"
  grep -q '^POLYMARKET_REPLAY_ON_START=false$' "$f" || fail "$f must default replay=false"
  grep -q '^PRICE_TO_BEAT_FALLBACK_ENABLED=false$' "$f" || fail "$f must default price-to-beat fallback=false"
  grep -q '^SHADOW_SETTLEMENT_EXTERNAL_FALLBACK_ENABLED=false$' "$f" || fail "$f must default shadow external settlement fallback=false"
  grep -q '^TG_BOT_TOKEN=your_telegram_bot_token_here$' "$f" || fail "$f must contain TG_BOT_TOKEN placeholder"
  grep -q '^TG_USER_ID=123456789$' "$f" || fail "$f must contain TG_USER_ID placeholder"
done


# Ubuntu 24.04 service template must be directly copyable for the default server path.
grep -q '^WorkingDirectory=/root/btc_bot_v15_1_3$' btc-bot.service.example || fail "service template WorkingDirectory must match default Ubuntu path"
grep -q '^ExecStart=/root/btc_bot_v15_1_3/.venv/bin/python /root/btc_bot_v15_1_3/main.py$' btc-bot.service.example || fail "service template ExecStart must match default Ubuntu path"

# A real server .env should not be forced to contain placeholders. Only enforce
# blocking safety contradictions that would make deployment misleading.
if [[ -f .env ]]; then
  grep -q '^REAL_TRADING_ENABLED=false$' .env || {
    grep -q '^DRY_RUN=false$' .env || fail ".env has real trading enabled without DRY_RUN=false"
    grep -q '^CLOB_V2_SIG3_REAL_SUBMIT_ENABLED=true$' .env || fail ".env has real trading enabled without sig3 real submit=true"
  }
fi

# Server readiness check uses local files/systemd/network. In CI/container we
# skip network because systemd and public endpoints may be unavailable there.
"$PYTHON_BIN" server_readiness_check.py --skip-network || fail "server readiness check found blocking local issues"

# Exclude .env placeholders from secret scan, but scan all source/scripts for accidental real keys.
if rg -n -I --glob '!*.pyc' --glob '!*.pyo' --glob '!*.md' --glob '!.env' --glob '!.env.example' --glob '!.env.high_win_rate.example' '0x[a-fA-F0-9]{64}|[0-9]{8,}:[A-Za-z0-9_-]{30,}' . >/tmp/btc_bot_v15_1_3_secret_scan.txt; then
  cat /tmp/btc_bot_v15_1_3_secret_scan.txt >&2
  fail "possible hardcoded secret found"
fi

echo "[audit v15.1.3] OK"
