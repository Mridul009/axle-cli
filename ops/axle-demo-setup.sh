#!/usr/bin/env bash
# Source this file to configure the Docker demo shell:
#   source ops/axle-demo-setup.sh
#
# Or execute checks without changing your shell:
#   ./ops/axle-demo-setup.sh check

if [ -n "${ZSH_VERSION:-}" ]; then
  eval '_axle_demo_script_path="${(%):-%x}"'
else
  _axle_demo_script_path="${BASH_SOURCE[0]:-$0}"
fi
_axle_demo_script_dir="$(cd "$(dirname "$_axle_demo_script_path")" >/dev/null 2>&1 && pwd)"
_axle_demo_repo_root="$(cd "${_axle_demo_script_dir}/.." >/dev/null 2>&1 && pwd)"

export AXLE_DEMO_COORDINATOR_CONTAINER="${AXLE_DEMO_COORDINATOR_CONTAINER:-axle-coordinator}"
export AXLE_DEMO_WORKER_CONTAINER="${AXLE_DEMO_WORKER_CONTAINER:-axle-worker}"
export AXLE_DEMO_BASE_URL="${AXLE_DEMO_BASE_URL:-http://127.0.0.1:8080}"
export AXLE_DEMO_ADMIN_TOKEN="${AXLE_DEMO_ADMIN_TOKEN:-change-me-admin}"
export AXLE_DEMO_POLL_SECONDS="${AXLE_DEMO_POLL_SECONDS:-2}"
export AXLE_DEMO_LOG_TAIL="${AXLE_DEMO_LOG_TAIL:-200}"
export AXLE_DEMO_CLI_HOME="${AXLE_DEMO_CLI_HOME:-/data/.axle-cli}"
export AXLE_DEMO_RUNS_DIR="${AXLE_DEMO_RUNS_DIR:-${AXLE_DEMO_CLI_HOME}/coordinator/runs}"
export AXLE_DEMO_ISSUE_KEY="${AXLE_DEMO_ISSUE_KEY:-KAN-19}"
export AXLE_DEMO_PROJECT_KEY="${AXLE_DEMO_PROJECT_KEY:-KAN}"
export AXLE_DEMO_LABEL="${AXLE_DEMO_LABEL:-axle-run}"

axle-demo() {
  "${_axle_demo_repo_root}/ops/axle-demo-docker.sh" "$@"
}

_axle_demo_status_line() {
  printf '%-28s %s\n' "$1" "$2"
}

_axle_demo_check_command() {
  if command -v "$1" >/dev/null 2>&1; then
    _axle_demo_status_line "$1" "ok"
    return 0
  fi
  _axle_demo_status_line "$1" "missing"
  return 1
}

_axle_demo_check_container() {
  local container="$1"
  local status
  status="$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)"
  if [ "$status" = "running" ]; then
    _axle_demo_status_line "$container" "running"
    return 0
  fi
  if [ -n "$status" ]; then
    _axle_demo_status_line "$container" "$status"
    return 1
  fi
  _axle_demo_status_line "$container" "not_found"
  return 1
}

_axle_demo_check_health() {
  local response
  response="$(curl -fsS "${AXLE_DEMO_BASE_URL}/healthz" 2>/dev/null || true)"
  if [ "$response" = '{"ok": true}' ] || [ "$response" = '{"ok":true}' ]; then
    _axle_demo_status_line "coordinator health" "ok"
    return 0
  fi
  _axle_demo_status_line "coordinator health" "error at ${AXLE_DEMO_BASE_URL}/healthz"
  return 1
}

_axle_demo_check_worker_api() {
  local response
  response="$(curl -fsS "${AXLE_DEMO_BASE_URL}/api/workers" -H "Authorization: Bearer ${AXLE_DEMO_ADMIN_TOKEN}" 2>/dev/null || true)"
  if [ -n "$response" ]; then
    _axle_demo_status_line "worker api" "ok"
    return 0
  fi
  _axle_demo_status_line "worker api" "error"
  return 1
}

_axle_demo_check_runs_dir() {
  if docker exec "$AXLE_DEMO_COORDINATOR_CONTAINER" sh -lc "[ -d '$AXLE_DEMO_RUNS_DIR' ]" >/dev/null 2>&1; then
    _axle_demo_status_line "runs dir" "$AXLE_DEMO_RUNS_DIR"
    return 0
  fi
  _axle_demo_status_line "runs dir" "missing: $AXLE_DEMO_RUNS_DIR"
  return 1
}

_axle_demo_print_config() {
  cat <<EOF
AXLE_DEMO_COORDINATOR_CONTAINER=${AXLE_DEMO_COORDINATOR_CONTAINER}
AXLE_DEMO_WORKER_CONTAINER=${AXLE_DEMO_WORKER_CONTAINER}
AXLE_DEMO_BASE_URL=${AXLE_DEMO_BASE_URL}
AXLE_DEMO_ADMIN_TOKEN=${AXLE_DEMO_ADMIN_TOKEN}
AXLE_DEMO_POLL_SECONDS=${AXLE_DEMO_POLL_SECONDS}
AXLE_DEMO_LOG_TAIL=${AXLE_DEMO_LOG_TAIL}
AXLE_DEMO_CLI_HOME=${AXLE_DEMO_CLI_HOME}
AXLE_DEMO_RUNS_DIR=${AXLE_DEMO_RUNS_DIR}
AXLE_DEMO_ISSUE_KEY=${AXLE_DEMO_ISSUE_KEY}
AXLE_DEMO_PROJECT_KEY=${AXLE_DEMO_PROJECT_KEY}
AXLE_DEMO_LABEL=${AXLE_DEMO_LABEL}
EOF
}

_axle_demo_check() {
  local failed=0
  _axle_demo_print_config
  printf '\n'
  _axle_demo_check_command docker || failed=1
  _axle_demo_check_command curl || failed=1
  _axle_demo_check_container "$AXLE_DEMO_COORDINATOR_CONTAINER" || failed=1
  _axle_demo_check_container "$AXLE_DEMO_WORKER_CONTAINER" || failed=1
  _axle_demo_check_health || failed=1
  _axle_demo_check_worker_api || failed=1
  _axle_demo_check_runs_dir || failed=1
  printf '\n'
  if [ "$failed" -eq 0 ]; then
    echo "Axle demo setup is ready."
    echo "Try: axle-demo show-issue ${AXLE_DEMO_ISSUE_KEY}"
  else
    echo "Axle demo setup has errors. Check Docker containers, AXLE_DEMO_BASE_URL, and AXLE_DEMO_ADMIN_TOKEN."
  fi
  return "$failed"
}

case "${1:-}" in
  check)
    _axle_demo_check
    ;;
  config)
    _axle_demo_print_config
    ;;
  "")
    echo "Configured Axle demo environment."
    echo "Function available in this shell: axle-demo"
    echo "Run: axle-demo health"
    ;;
  *)
    echo "Usage:"
    echo "  source ops/axle-demo-setup.sh"
    echo "  ./ops/axle-demo-setup.sh check"
    echo "  ./ops/axle-demo-setup.sh config"
    return 2 2>/dev/null || exit 2
    ;;
esac
