#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export PYTHONPATH="$ROOT/backend"
export PLAYWRIGHT_BROWSERS_PATH="$ROOT/.cache/ms-playwright"
export LANGSMITH_TRACING=false LANGSMITH_TRACING_V2=false
export LANGCHAIN_TRACING=false LANGCHAIN_TRACING_V2=false LANGCHAIN_HANDLER=false
COMMAND="${1:-}"
if [ "$#" -gt 0 ]; then shift; fi
case "$COMMAND" in
  api|worker|doctor)
    if [ ! -x .venv/bin/python ]; then echo "Run scripts/bootstrap.sh first." >&2; exit 1; fi
    exec .venv/bin/python -m webagent "$COMMAND" "$@"
    ;;
  frontend)
    if [ ! -x .runtime/node ] || [ ! -d frontend/node_modules ]; then
      echo "Run scripts/bootstrap.sh first." >&2; exit 1
    fi
    cd frontend
    exec ../.runtime/node node_modules/vite/bin/vite.js "$@"
    ;;
  *) echo "Usage: scripts/dev.sh {api|worker|frontend|doctor} [options]" >&2; exit 2 ;;
esac

