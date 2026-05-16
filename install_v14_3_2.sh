#!/usr/bin/env bash
set -euo pipefail
APP_DIR="${1:-/root/btc_bot_v14_3_2}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE="btc-bot"
PYTHON_BIN="${PYTHON_BIN:-python3}"

echo "==> Installing btc_bot v14.3.2"
echo "SRC_DIR=$SRC_DIR"
echo "APP_DIR=$APP_DIR"

systemctl stop "$SERVICE" 2>/dev/null || true

if [ "$SRC_DIR" != "$APP_DIR" ]; then
  mkdir -p "$APP_DIR"
  rsync -a --delete \
    --exclude '.venv' \
    --exclude 'btc_bot.db' \
    --exclude 'btc_bot.db-*' \
    "$SRC_DIR"/ "$APP_DIR"/
fi

# Preserve runtime configuration/state from a previous version if the new app does not have it.
for OLD in /root/btc_bot_v14_3_1 /root/btc_bot_v14_3_0 /root/btc_bot_v14_2_38_ready; do
  if [ -d "$OLD" ]; then
    if [ ! -s "$APP_DIR/.env" ] || ! grep -q '^TG_BOT_TOKEN=.*[^[:space:]]' "$APP_DIR/.env" 2>/dev/null; then
      [ -f "$OLD/.env" ] && cp -a "$OLD/.env" "$APP_DIR/.env" || true
    fi
    if [ ! -f "$APP_DIR/btc_bot.db" ]; then
      [ -f "$OLD/btc_bot.db" ] && cp -a "$OLD/btc_bot.db" "$APP_DIR/btc_bot.db" || true
    fi
  fi
done

cd "$APP_DIR"
if [ ! -f .env ]; then
  cp .env.example .env
fi

if [ ! -d .venv ]; then
  "$PYTHON_BIN" -m venv .venv
fi
./.venv/bin/python -m pip install --upgrade pip >/dev/null
./.venv/bin/python -m pip install -r requirements.txt

# Compile before touching systemd start.
./.venv/bin/python -m py_compile *.py

cat > /etc/systemd/system/${SERVICE}.service <<EOF
[Unit]
Description=BTC Polymarket Bot v14.3.2
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python $APP_DIR/main.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null
systemctl start "$SERVICE"
sleep 3
systemctl status "$SERVICE" --no-pager | sed -n '1,28p'
echo "==> Installed at $APP_DIR"
echo "observe: journalctl -u btc-bot -f | grep -E 'Adaptive Edge|Profit Rule|SHADOW|ERROR|Traceback'"
