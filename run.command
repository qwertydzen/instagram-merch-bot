#!/usr/bin/env bash
# Double-click this in Finder (or run ./run.command) to start the bot
# with a visible terminal and open the tester in your browser.
set -e
cd "$(dirname "$0")"

# Some environments export PYTHONPATH pointing at a global site-packages built
# for a different Python version — that leaks incompatible deps into this venv.
unset PYTHONPATH PYTHONHOME

if [ ! -d .venv ]; then
  echo "Setting up virtualenv (first run)…"
  python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
fi

[ -f .env ] || { cp .env.example .env; echo "Created .env — add your ARCADE_API_KEY when you have it."; }

PORT="${PORT:-8000}"
( sleep 2; open "http://127.0.0.1:${PORT}" ) &
echo "Starting on http://127.0.0.1:${PORT}  (Ctrl+C to stop)"
exec .venv/bin/uvicorn app.main:app --reload --port "${PORT}"
