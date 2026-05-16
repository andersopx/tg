#!/usr/bin/env bash
set -euo pipefail

# BTC Bot v15.1.3 safe installer / new-install friendly upgrader
# Goals:
# - use one systemd service name by default: btc-bot
# - stop/disable old duplicate services before installing
# - preserve existing .env and btc_bot.db runtime state, including TG-entered keys
# - never overwrite real credentials with package placeholders

APP_DIR="${APP_DIR:-/root/btc_bot_v15_1_3}"
SERVICE_NAME="${SERVICE_NAME:-btc-bot}"
PKG_PATH="${1:-/root/btc_bot_v15_1_3.tar.gz}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
BACKUP_ROOT="${BACKUP_ROOT:-/root/btc_bot_backups}"
OLD_APP_DIR="${OLD_APP_DIR:-}"
START_AFTER_INSTALL="${START_AFTER_INSTALL:-false}"
OLD_SERVICE_NAMES="${OLD_SERVICE_NAMES:-btc-bot btc-bot-v15 btc-bot-v15-0-0 btc-bot-v15-1-1 btc-bot-v15-1-2 btc-bot-v14 btc-bot-v14-3-0}"
TS="$(date +%Y%m%d_%H%M%S)"

if [[ $EUID -ne 0 ]]; then
  echo "请用 root 执行：sudo bash $0 /path/to/btc_bot_v15_1_3.tar.gz" >&2
  exit 1
fi

if [[ ! -f "$PKG_PATH" ]]; then
  echo "压缩包不存在：$PKG_PATH" >&2
  exit 1
fi

log() { echo "[v15.1.3 installer] $*"; }
unit_exists() { systemctl list-unit-files "$1.service" >/dev/null 2>&1 || systemctl status "$1.service" >/dev/null 2>&1; }

stop_service_if_exists() {
  local svc="$1"
  if unit_exists "$svc"; then
    log "停止服务：$svc"
    systemctl stop "$svc" >/dev/null 2>&1 || true
    if [[ "$svc" != "$SERVICE_NAME" ]]; then
      log "禁用旧服务，避免双机器人：$svc"
      systemctl disable "$svc" >/dev/null 2>&1 || true
    fi
  fi
}

for svc in $OLD_SERVICE_NAMES; do
  stop_service_if_exists "$svc"
done
stop_service_if_exists "$SERVICE_NAME"

has_runtime_state() {
  local d="$1"
  [[ -d "$d" ]] || return 1
  [[ -f "$d/.env" || -f "$d/btc_bot.db" || -f "$d/btc_bot.sqlite" ]] && return 0
  find "$d" -maxdepth 1 -type f \( -name '*.db' -o -name '*.sqlite' \) 2>/dev/null | grep -q .
}

parse_env_value() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 0
  awk -F= -v k="$key" '
    $0 ~ "^[[:space:]]*#" {next}
    $1 == k {sub(/^[^=]*=/, ""); gsub(/^\"|\"$/, ""); gsub(/^'"'"'|\'"'"'$/, ""); print; exit}
  ' "$file"
}

resolve_db_path() {
  local base="$1"
  local env_file="$base/.env"
  local dbp
  dbp="$(parse_env_value "$env_file" DB_PATH || true)"
  if [[ -z "${dbp:-}" ]]; then
    dbp="btc_bot.db"
  fi
  if [[ "$dbp" = /* ]]; then
    printf '%s\n' "$dbp"
  else
    printf '%s/%s\n' "$base" "$dbp"
  fi
}

if [[ -n "$OLD_APP_DIR" && ! -d "$OLD_APP_DIR" ]]; then
  echo "OLD_APP_DIR 指向的目录不存在：$OLD_APP_DIR" >&2
  exit 1
fi

if [[ -z "$OLD_APP_DIR" ]]; then
  # Prefer known old runtime directories before APP_DIR.
  # This matters when the user has just extracted the new package into APP_DIR:
  # the package contains a placeholder .env, which must not be mistaken for the
  # old runtime .env / btc_bot.db.
  candidates=(
    "/root/btc_bot_v15_1_1"
    "/root/btc_bot_v15_1_0"
    "/root/btc_bot_v14_3_2"
    "/root/btc_bot_v14_3_0"
    "/root/btc_bot_v15_0_0"
    "/root/btc_bot_v15_high_win_rate_ready"
    "/root/btc_bot"
    "/opt/btc_bot"
    "/root/btc_bot_v14"
    "$APP_DIR"
  )
  for d in "${candidates[@]}"; do
    if has_runtime_state "$d"; then
      OLD_APP_DIR="$d"
      break
    fi
  done
fi

BACKUP_DIR="$BACKUP_ROOT/v15_1_3_${TS}"
mkdir -p "$BACKUP_DIR"

if [[ -n "$OLD_APP_DIR" && -d "$OLD_APP_DIR" ]]; then
  log "检测到旧运行目录：$OLD_APP_DIR"
  log "备份旧运行目录到：$BACKUP_DIR/old_app"
  mkdir -p "$BACKUP_DIR/old_app"
  # Preserve permissions and hidden files without following symlink targets.
  cp -a "$OLD_APP_DIR"/. "$BACKUP_DIR/old_app"/ 2>/dev/null || true
fi

if [[ -d "$APP_DIR" ]]; then
  log "备份目标目录到：$BACKUP_DIR/target_before_install"
  mkdir -p "$BACKUP_DIR/target_before_install"
  cp -a "$APP_DIR"/. "$BACKUP_DIR/target_before_install"/ 2>/dev/null || true
fi

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
log "解压新包：$PKG_PATH"
tar -xzf "$PKG_PATH" -C "$TMP_DIR"
SRC_ROOT="$(find "$TMP_DIR" -mindepth 1 -maxdepth 1 -type d | head -n 1)"
if [[ -z "$SRC_ROOT" ]]; then
  echo "压缩包结构异常：没有顶层目录" >&2
  exit 1
fi

mkdir -p "$APP_DIR"

# Install source files first. Runtime files are restored afterwards.
log "同步源码到：$APP_DIR"
find "$SRC_ROOT" -mindepth 1 -maxdepth 1 \
  ! -name '.env' \
  ! -name '.venv' \
  ! -name 'btc_bot.db' \
  ! -name 'btc_bot.db-wal' \
  ! -name 'btc_bot.db-shm' \
  ! -name '__pycache__' \
  ! -name '.pytest_cache' \
  -exec cp -a {} "$APP_DIR"/ \;

# Restore runtime .env and DB from old dir/target dir. Source priority:
# 1. OLD_APP_DIR if detected or provided
# 2. APP_DIR backup if already had runtime state
# 3. package .env for brand-new install
# 4. .env.example fallback
restore_from_dir() {
  local d="$1"
  [[ -d "$d" ]] || return 1
  local restored=1
  if [[ -f "$d/.env" ]]; then
    cp -a "$d/.env" "$APP_DIR/.env"
    log "已保留 .env：$d/.env -> $APP_DIR/.env"
    restored=0
  fi
  local old_db
  old_db="$(resolve_db_path "$d")"
  if [[ -f "$old_db" ]]; then
    local new_db
    new_db="$(resolve_db_path "$APP_DIR")"
    mkdir -p "$(dirname "$new_db")"
    cp -a "$old_db" "$new_db"
    log "已保留数据库：$old_db -> $new_db"
    for suffix in -wal -shm; do
      if [[ -f "$old_db$suffix" ]]; then
        cp -a "$old_db$suffix" "$new_db$suffix"
        log "已保留数据库旁路文件：$old_db$suffix -> $new_db$suffix"
      fi
    done
    restored=0
  fi
  return "$restored"
}

if [[ -n "$OLD_APP_DIR" ]]; then
  restore_from_dir "$OLD_APP_DIR" || true
elif [[ -d "$BACKUP_DIR/target_before_install" ]]; then
  restore_from_dir "$BACKUP_DIR/target_before_install" || true
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  log "新安装：已从 .env.example 创建安全 .env。请填写 TG_BOT_TOKEN / TG_USER_ID。"
fi

# New-install sanity hints. Do not fail here because the user may fill TG later.
if grep -q '^TG_BOT_TOKEN=your_telegram_bot_token_here$' "$APP_DIR/.env" 2>/dev/null; then
  log "提示：TG_BOT_TOKEN 仍是占位值，Telegram 暂时无法连接。"
fi
if grep -q '^TG_USER_ID=123456789$' "$APP_DIR/.env" 2>/dev/null; then
  log "提示：TG_USER_ID 仍是占位值，请改成你的 Telegram 数字 ID。"
fi

cd "$APP_DIR"
chmod +x ./*.sh 2>/dev/null || true

# Do not auto-modify an existing .env strategy profile. The package defaults and
# code-level defaults already keep PROFIT_RULE_LIVE_REQUIRE_PROMOTE=true.
# To intentionally enable the stricter legacy high-win-rate guard, copy the
# relevant HIGH_WIN_RATE_* values from .env.high_win_rate.example manually.

if [[ ! -d ".venv" ]]; then
  log "创建 Python 虚拟环境"
  "$PYTHON_BIN" -m venv .venv
fi
source .venv/bin/activate
log "安装 Python 依赖"
python -m pip install --upgrade pip
pip install -r requirements.txt

log "执行语法检查"
python -m compileall -q .

cat >/etc/systemd/system/${SERVICE_NAME}.service <<EOF
[Unit]
Description=BTC Trading Bot v15.1.3
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${APP_DIR}
Environment=PYTHONUNBUFFERED=1
ExecStart=${APP_DIR}/.venv/bin/python ${APP_DIR}/main.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ${SERVICE_NAME} >/dev/null

log "安装完成。服务名：${SERVICE_NAME}"
log "备份目录：${BACKUP_DIR}"
log "启动命令：systemctl restart ${SERVICE_NAME}"
log "日志命令：journalctl -u ${SERVICE_NAME} -f"

if [[ "$START_AFTER_INSTALL" == "true" ]]; then
  log "START_AFTER_INSTALL=true，正在启动服务：${SERVICE_NAME}"
  systemctl restart ${SERVICE_NAME}
  systemctl --no-pager --full status ${SERVICE_NAME} || true
else
  log "默认未自动启动。确认 .env / TG密钥 / btc_bot.db 迁移无误后再启动。"
fi
