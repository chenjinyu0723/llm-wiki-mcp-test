#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
HOST="${VALIDATOR_HOST:-127.0.0.1}"
PORT="${VALIDATOR_PORT:-8044}"
VENV_PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$VENV_PYTHON" ]]; then
  "$PYTHON" -m venv "$ROOT/.venv"
fi
if [[ "${VALIDATOR_INSTALL_DEPENDENCIES:-false}" == "true" ]] || ! "$VENV_PYTHON" -c 'import fastapi, httpx, openai, pydantic_settings, uvicorn' >/dev/null 2>&1; then
  "$VENV_PYTHON" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
fi
if [[ ! -f "$ROOT/.env" ]]; then
  cp "$ROOT/.env.example" "$ROOT/.env"
fi
cd "$ROOT"
exec "$VENV_PYTHON" -m uvicorn app.main:app --host "$HOST" --port "$PORT"
