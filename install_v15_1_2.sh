#!/usr/bin/env bash
set -euo pipefail

VERSION="v15.1.2"
APP_DIR="/root/btc_bot_v15_1_2"
ARCHIVE="/root/btc_bot_v15_1_2.tar.gz"
START_AFTER_INSTALL="${START_AFTER_INSTALL:-true}"

echo "Installing ${VERSION} to ${APP_DIR}"
mkdir -p "${APP_DIR}"

tar -xzf "${ARCHIVE}" -C "${APP_DIR}" --strip-components=1

if [[ ! -f "${APP_DIR}/.env" && -f "${APP_DIR}/.env.example" ]]; then
  cp "${APP_DIR}/.env.example" "${APP_DIR}/.env"
  echo "Created .env from .env.example; fill credentials before enabling live trading."
fi

if [[ "${START_AFTER_INSTALL}" == "true" && -x "${APP_DIR}/start.sh" ]]; then
  cd "${APP_DIR}"
  ./start.sh
fi
