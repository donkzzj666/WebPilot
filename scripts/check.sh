#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
export LANGSMITH_TRACING=false LANGSMITH_TRACING_V2=false
export LANGCHAIN_TRACING=false LANGCHAIN_TRACING_V2=false LANGCHAIN_HANDLER=false
./scripts/dev.sh doctor
.venv/bin/python -m pip check
.venv/bin/python scripts/build_manifest.py --check
.venv/bin/python -m pytest
./scripts/npm.sh --prefix frontend run typecheck
./scripts/npm.sh --prefix frontend run build
export PYTHONPATH="$ROOT/backend"
export PLAYWRIGHT_BROWSERS_PATH="$ROOT/.cache/ms-playwright"
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--headed" ]; }; then
  echo "Usage: scripts/check.sh [--headed]" >&2
  exit 2
fi
mkdir -p artifacts/verification/M1-01
VERIFY_DIR="$(mktemp -d "$ROOT/artifacts/verification/M1-01/check-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")"
.venv/bin/python scripts/verify_m1_01.py --output-dir "$VERIFY_DIR/integration" "$@"
.venv/bin/python scripts/verify_startup.py --output-dir "$VERIFY_DIR/startup"
.venv/bin/python scripts/check_evidence.py "$VERIFY_DIR/integration/report.json" "$VERIFY_DIR/startup/report.json" > "$VERIFY_DIR/hash-verification.json"
cat "$VERIFY_DIR/hash-verification.json"
