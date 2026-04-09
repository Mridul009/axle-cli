#!/usr/bin/env bash
set -euo pipefail

COORDINATOR_CONTAINER="${AXLE_DEMO_COORDINATOR_CONTAINER:-axle-coordinator}"
WORKER_CONTAINER="${AXLE_DEMO_WORKER_CONTAINER:-axle-worker}"
BASE_URL="${AXLE_DEMO_BASE_URL:-http://127.0.0.1}"
ADMIN_TOKEN="${AXLE_DEMO_ADMIN_TOKEN:-change-me-admin}"
POLL_SECONDS="${AXLE_DEMO_POLL_SECONDS:-2}"

usage() {
  cat <<'EOF'
Usage:
  axle-demo-docker.sh logs
  axle-demo-docker.sh run <run_id>
  axle-demo-docker.sh watch <run_id>
  axle-demo-docker.sh show <run_id>

Environment overrides:
  AXLE_DEMO_COORDINATOR_CONTAINER   default: axle-coordinator
  AXLE_DEMO_WORKER_CONTAINER        default: axle-worker
  AXLE_DEMO_BASE_URL                default: http://127.0.0.1
  AXLE_DEMO_ADMIN_TOKEN             default: change-me-admin
  AXLE_DEMO_POLL_SECONDS            default: 2

Examples:
  ./ops/axle-demo-docker.sh logs
  ./ops/axle-demo-docker.sh watch 38b12550-f6c1-5c2f-9c92-a18bb047176e
EOF
}

require_arg() {
  if [[ $# -lt 2 || -z "${2:-}" ]]; then
    usage
    exit 1
  fi
}

fetch_run_json() {
  local run_id="$1"
  curl -fsS "${BASE_URL}/api/runs/${run_id}" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}"
}

print_run_summary() {
  python3 - "$@" <<'PY'
import json
import sys

payload = json.load(sys.stdin)
events = payload.get("events") or []
last_event = events[-1] if events else {}
changed_files = payload.get("changed_files") or []

print(f"run_id: {payload.get('run_id', '')}")
print(f"status: {payload.get('status', '')}")
print(f"result: {payload.get('result', '')}")
print(f"test_command: {payload.get('test_command', '')}")
print(f"last_stage: {last_event.get('stage') or '-'}")
print(f"last_event: {last_event.get('message') or '-'}")
if changed_files:
    print("changed_files:")
    for path in changed_files:
        print(f"  - {path}")
if payload.get("failure"):
    print(f"failure: {payload['failure']}")
if payload.get("pr_url"):
    print(f"pr_url: {payload['pr_url']}")
PY
}

watch_run() {
  local run_id="$1"

  while true; do
    local json
    json="$(fetch_run_json "$run_id")"
    clear
    printf '%s\n' "$json" | print_run_summary

    local status
    status="$(printf '%s' "$json" | python3 - <<'PY'
import json
import sys
print(json.load(sys.stdin).get("status", ""))
PY
)"

    if [[ "$status" == "completed" || "$status" == "failed" ]]; then
      printf '\nfinal_status: %s\n' "$status"
      break
    fi

    sleep "$POLL_SECONDS"
  done
}

combined_logs() {
  trap 'kill 0' INT TERM EXIT

  docker logs "$COORDINATOR_CONTAINER" --since 30s -f 2>&1 | sed 's/^/[coordinator] /' &
  docker logs "$WORKER_CONTAINER" --since 30s -f 2>&1 | sed 's/^/[worker] /' &
  wait
}

main() {
  local cmd="${1:-}"

  case "$cmd" in
    logs)
      combined_logs
      ;;
    run|watch)
      require_arg "$@"
      watch_run "$2"
      ;;
    show)
      require_arg "$@"
      fetch_run_json "$2" | print_run_summary
      ;;
    ""|-h|--help|help)
      usage
      ;;
    *)
      echo "Unknown command: $cmd" >&2
      usage
      exit 1
      ;;
  esac
}

main "$@"
