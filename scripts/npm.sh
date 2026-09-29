#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
if [ ! -x "$ROOT/.runtime/node" ] || [ ! -f "$ROOT/.runtime/npm-cli.path" ]; then
  echo "Run scripts/bootstrap.sh first." >&2
  exit 1
fi
NODE="${WEBAGENT_NODE:-$ROOT/.runtime/node}"
NPM_CLI="${WEBAGENT_NPM_CLI:-$(cat "$ROOT/.runtime/npm-cli.path")}"
# Keep child npm scripts on the selected Node too.
export PATH="$(dirname "$NODE"):$ROOT/.runtime:$PATH"
exec "$NODE" "$NPM_CLI" "$@"
