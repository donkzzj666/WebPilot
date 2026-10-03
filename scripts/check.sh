#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p artifacts/verification/M1-25
VERIFY_DIR="$(mktemp -d "$ROOT/artifacts/verification/M1-25/check-$(date -u +%Y%m%dT%H%M%SZ).XXXXXX")"
export LANGSMITH_TRACING=false LANGSMITH_TRACING_V2=false
export LANGCHAIN_TRACING=false LANGCHAIN_TRACING_V2=false LANGCHAIN_HANDLER=false
./scripts/dev.sh doctor
.venv/bin/python -m pip check
.venv/bin/python scripts/dependencies/build_manifest.py --check
.venv/bin/python -m pytest -x --junitxml="$VERIFY_DIR/pytest.xml"
.runtime/node --experimental-strip-types --test --test-reporter=junit --test-reporter-destination="$VERIFY_DIR/frontend-contracts.xml" frontend/src/settings/identity-contracts.test.ts frontend/src/tasks/contracts.test.ts frontend/src/tasks/client.test.ts frontend/src/workbench/contracts.test.ts frontend/src/workbench/client.test.ts frontend/src/workbench/events.test.ts frontend/src/results/contracts.test.ts frontend/src/results/client.test.ts
./scripts/npm.sh --prefix frontend run typecheck
./scripts/npm.sh --prefix frontend run build
export PYTHONPATH="$ROOT/backend"
export PLAYWRIGHT_BROWSERS_PATH="$ROOT/.cache/ms-playwright"
if [ "$#" -gt 1 ] || { [ "$#" -eq 1 ] && [ "$1" != "--headed" ]; }; then
  echo "Usage: scripts/check.sh [--headed]" >&2
  exit 2
fi
.venv/bin/python scripts/verification/verify_m1_01.py --output-dir "$VERIFY_DIR/integration" "$@"
.venv/bin/python scripts/verification/verify_startup.py --output-dir "$VERIFY_DIR/startup"
.venv/bin/python scripts/verification/verify_task_api.py --output-dir "$VERIFY_DIR/task-api"
.venv/bin/python scripts/verification/verify_model_adapter.py --output-dir "$VERIFY_DIR/model-adapter"
.venv/bin/python scripts/verification/verify_secret_store.py --output-dir "$VERIFY_DIR/secret-store"
.venv/bin/python scripts/verification/verify_settings.py --output-dir "$VERIFY_DIR/settings"
.venv/bin/python scripts/verification/verify_natural_tasks.py --output-dir "$VERIFY_DIR/natural-tasks"
.venv/bin/python scripts/verification/verify_browser_sessions.py --output-dir "$VERIFY_DIR/browser-sessions"
.venv/bin/python scripts/verification/verify_scheduler.py --output-dir "$VERIFY_DIR/scheduler"
.venv/bin/python scripts/verification/verify_budgets.py --output-dir "$VERIFY_DIR/budgets"
.venv/bin/python scripts/verification/verify_gateway.py --output-dir "$VERIFY_DIR/gateway"
.venv/bin/python scripts/verification/verify_evidence.py --output-dir "$VERIFY_DIR/evidence"
.venv/bin/python scripts/verification/verify_verification.py --output-dir "$VERIFY_DIR/verification"
.venv/bin/python scripts/verification/verify_graph.py --output-dir "$VERIFY_DIR/graph"
.venv/bin/python scripts/verification/verify_recovery.py --output-dir "$VERIFY_DIR/recovery"
.venv/bin/python scripts/verification/verify_controls.py --output-dir "$VERIFY_DIR/controls"
.venv/bin/python scripts/verification/verify_writes.py --output-dir "$VERIFY_DIR/writes"
.venv/bin/python scripts/verification/verify_observability.py --output-dir "$VERIFY_DIR/observability"
.venv/bin/python scripts/verification/verify_task_entry.py --output-dir "$VERIFY_DIR/task-entry"
.venv/bin/python scripts/verification/verify_workbench.py --output-dir "$VERIFY_DIR/workbench"
.venv/bin/python scripts/verification/verify_results.py --output-dir "$VERIFY_DIR/results"
.venv/bin/python scripts/verification/verify_login_sessions.py --output-dir "$VERIFY_DIR/login-sessions"
.venv/bin/python scripts/verification/verify_auth_keychain.py --output-dir "$VERIFY_DIR/auth-keychain"
.venv/bin/python scripts/verification/verify_api_security.py --output-dir "$VERIFY_DIR/api-security"
.venv/bin/python scripts/verification/verify_network_boundary.py --output-dir "$VERIFY_DIR/network-boundary"
.venv/bin/python scripts/verification/verify_network_boundary.py --headless --output-dir "$VERIFY_DIR/network-headless"
.venv/bin/python scripts/verification/verify_fault_acceptance.py --output-dir "$VERIFY_DIR/fault-acceptance" "$@"
.venv/bin/python scripts/verification/check_evidence.py "$VERIFY_DIR/integration/report.json" "$VERIFY_DIR/startup/report.json" "$VERIFY_DIR/task-api/report.json" "$VERIFY_DIR/model-adapter/report.json" "$VERIFY_DIR/secret-store/report.json" "$VERIFY_DIR/settings/report.json" "$VERIFY_DIR/natural-tasks/report.json" "$VERIFY_DIR/browser-sessions/report.json" "$VERIFY_DIR/auth-keychain/report.json" "$VERIFY_DIR/api-security/report.json" "$VERIFY_DIR/network-boundary/report.json" "$VERIFY_DIR/network-headless/report.json" "$VERIFY_DIR/login-sessions/report.json" "$VERIFY_DIR/scheduler/report.json" "$VERIFY_DIR/budgets/report.json" "$VERIFY_DIR/gateway/report.json" "$VERIFY_DIR/evidence/report.json" "$VERIFY_DIR/verification/report.json" "$VERIFY_DIR/graph/report.json" "$VERIFY_DIR/recovery/report.json" "$VERIFY_DIR/controls/report.json" "$VERIFY_DIR/writes/report.json" "$VERIFY_DIR/observability/report.json" "$VERIFY_DIR/task-entry/report.json" "$VERIFY_DIR/workbench/report.json" "$VERIFY_DIR/results/report.json" "$VERIFY_DIR/fault-acceptance/report.json" > "$VERIFY_DIR/hash-verification.json"
cat "$VERIFY_DIR/hash-verification.json"
