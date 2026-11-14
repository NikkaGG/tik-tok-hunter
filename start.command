#!/bin/zsh
# TikTok Lead Hunter: запуск для macOS (можна запускати подвійним кліком у Finder або з терміналу)
cd "$(dirname "$0")" || exit 1
export PYTHONDONTWRITEBYTECODE=1
export PYTHONUTF8=1

# Зупиняємо попередній процес на порту 8000, якщо він ще висить
OLD_PIDS=$(lsof -ti tcp:8000 2>/dev/null)
if [ -n "$OLD_PIDS" ]; then
  echo "Stopping previous server instance on port 8000 ($OLD_PIDS)..."
  kill -9 $OLD_PIDS 2>/dev/null
fi

VENV_DIR="$HOME/.ttlh-venv"
if [ -x ".venv/bin/python" ]; then
  VENV_DIR=".venv"
fi

if [ ! -x "$VENV_DIR/bin/python" ]; then
  echo "[1/3] Creating virtual environment for macOS..."
  python3 -m venv "$VENV_DIR" || exit 1
  echo "[2/3] Installing dependencies..."
  "$VENV_DIR/bin/python" -m pip install -r requirements.txt || exit 1
  echo "[3/3] Installing Chromium for Playwright..."
  "$VENV_DIR/bin/python" -m playwright install chromium || exit 1
fi

echo "Starting server: http://127.0.0.1:8000 (Ctrl+C to stop)"
(sleep 2 && open "http://127.0.0.1:8000/?v=$RANDOM") &
"$VENV_DIR/bin/python" -m uvicorn app.main:app --port 8000

