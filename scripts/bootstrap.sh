#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON="${WEBAGENT_PYTHON:-}"
if [ -z "$PYTHON" ]; then
  if command -v python3.12 >/dev/null 2>&1; then
    PYTHON="$(command -v python3.12)"
  elif [ -x "$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" ]; then
    PYTHON="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3"
  else
    PYTHON="$(command -v python3)"
  fi
fi
# Prove the actual interpreter's SQLite, not the unrelated sqlite3 command.
PYTHONPATH="$ROOT/backend" "$PYTHON" -c 'from webagent.runtime import check_runtime; print(check_runtime())'

NODE="${WEBAGENT_NODE:-}"
if [ -z "$NODE" ]; then
  NODE="$(command -v node || true)"
  if [ -z "$NODE" ] || ! "$NODE" -e 'process.exit(Number(process.versions.node.split(".")[0]) === 24 ? 0 : 1)'; then
    NODE="$HOME/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node"
  fi
fi
if [ ! -x "$NODE" ]; then
  echo "Set WEBAGENT_NODE to a Node 24 executable." >&2
  exit 1
fi
"$NODE" -e 'if (+process.versions.node.split(".")[0] !== 24) throw Error("This build targets Node 24"); console.log("Node", process.versions.node)'
NPM_CLI="${WEBAGENT_NPM_CLI:-}"
if [ -z "$NPM_CLI" ]; then
  NPM_BIN="$(command -v npm || true)"
  if [ -z "$NPM_BIN" ]; then
    echo "Set WEBAGENT_NPM_CLI to npm/bin/npm-cli.js." >&2
    exit 1
  fi
  NPM_CLI="$("$PYTHON" -c 'import pathlib,sys; print(pathlib.Path(sys.argv[1]).resolve())' "$NPM_BIN")"
fi
"$NODE" "$NPM_CLI" --version
mkdir -p .runtime .cache
ln -sfn "$NODE" .runtime/node
printf '%s\n' "$NPM_CLI" > .runtime/npm-cli.path
if [ ! -x .venv/bin/python ]; then
  "$PYTHON" -m venv .venv
fi
PYTHONPATH="$ROOT/backend" .venv/bin/python -m webagent doctor
.venv/bin/python -m pip --isolated install --index-url https://pypi.org/simple \
  --cache-dir "$ROOT/.cache/pip" --require-hashes -r requirements/requirements-dev.lock
.venv/bin/python -m pip check
./scripts/npm.sh ci --prefix frontend --cache "$ROOT/.cache/npm" --no-audit --no-fund
PLAYWRIGHT_BROWSERS_PATH="$ROOT/.cache/ms-playwright" .venv/bin/python -m playwright install chromium
.venv/bin/python scripts/dependencies/build_manifest.py --check
echo "Bootstrap complete. Start api, worker and frontend in separate terminals with scripts/dev.sh."
