#!/usr/bin/env bash
set -euo pipefail

DATA_DIR="${WEB2API_DATA_DIR:-/data}"
CONFIG_PATH="${WEB2API_CONFIG_PATH:-${DATA_DIR}/config.yaml}"
DB_PATH="${WEB2API_DB_PATH:-${DATA_DIR}/db.sqlite3}"
XVFB_ARGS="${XVFB_SCREEN_ARGS:--screen 0 1920x1080x24}"
DISPLAY_NUM="${XVFB_DISPLAY_NUM:-99}"
DISPLAY_VALUE=":${DISPLAY_NUM}"

mkdir -p "${DATA_DIR}"

export HOME="${DATA_DIR}"
export WEB2API_CONFIG_PATH="${CONFIG_PATH}"
export WEB2API_DB_PATH="${DB_PATH}"
export PYTHONUNBUFFERED=1

# 清理残留的浏览器 profile，避免 Singleton* 锁导致 Chromium 认为 profile 正在被使用。
rm -rf "${HOME}/fp-data"

if [[ ! -f "${CONFIG_PATH}" ]]; then
  cp /app/docker/config.container.yaml "${CONFIG_PATH}"
fi

mkdir -p "${HOME}/fp-data"

if [[ $# -gt 0 ]]; then
  exec "$@"
fi

cleanup() {
  if [[ -n "${XVFB_PID:-}" ]]; then
    kill "${XVFB_PID}" >/dev/null 2>&1 || true
  fi
}

trap cleanup EXIT INT TERM

mkdir -p /tmp/.X11-unix
rm -f "/tmp/.X${DISPLAY_NUM}-lock"

Xvfb "${DISPLAY_VALUE}" ${XVFB_ARGS} -nolisten tcp -ac &
XVFB_PID=$!

for _ in $(seq 1 100); do
  if [[ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]]; then
    break
  fi
  sleep 0.1
done

if [[ ! -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ]]; then
  echo "Xvfb failed to create display ${DISPLAY_VALUE}" >&2
  exit 1
fi

export DISPLAY="${DISPLAY_VALUE}"

exec python -u /app/main.py
