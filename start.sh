#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")"

CONFIG_PATH="${WEB2API_CONFIG_PATH:-config.local.yaml}"
export WEB2API_CONFIG_PATH="$CONFIG_PATH"

if [ ! -f "$CONFIG_PATH" ]; then
  cp config.yaml "$CONFIG_PATH"
  echo "Created $CONFIG_PATH from config.yaml."
fi

if [ -z "${WEB2API_DB_PATH:-}" ]; then
  export WEB2API_DB_PATH="db.sqlite3"
fi

if command -v uv >/dev/null 2>&1; then
  uv sync
  echo "Starting Web2API at http://127.0.0.1:9000"
  exec uv run python main.py
fi

PYTHON_BIN="${PYTHON:-python3}"
if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  PYTHON_BIN="python"
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.12+ is required. Install Python or uv first." >&2
  exit 1
fi

if [ ! -d ".venv" ]; then
  "$PYTHON_BIN" -m venv .venv
fi

if [ -x ".venv/bin/python" ]; then
  VENV_PY=".venv/bin/python"
elif [ -x ".venv/Scripts/python.exe" ]; then
  VENV_PY=".venv/Scripts/python.exe"
else
  echo "Could not find Python inside .venv." >&2
  exit 1
fi

"$VENV_PY" -m pip install --upgrade pip
"$VENV_PY" -m pip install -e .

echo "Starting Web2API at http://127.0.0.1:9000"
exec "$VENV_PY" main.py
