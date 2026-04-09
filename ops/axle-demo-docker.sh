#!/usr/bin/env bash
set -euo pipefail

COORDINATOR_CONTAINER="${AXLE_DEMO_COORDINATOR_CONTAINER:-axle-coordinator}"
WORKER_CONTAINER="${AXLE_DEMO_WORKER_CONTAINER:-axle-worker}"
BASE_URL="${AXLE_DEMO_BASE_URL:-http://127.0.0.1}"
ADMIN_TOKEN="${AXLE_DEMO_ADMIN_TOKEN:-change-me-admin}"
POLL_SECONDS="${AXLE_DEMO_POLL_SECONDS:-2}"
CLI_HOME="${AXLE_DEMO_CLI_HOME:-/data/.axle-cli}"
DEFAULT_ISSUE_KEY="${AXLE_DEMO_ISSUE_KEY:-KAN-9803}"
DEFAULT_PROJECT_KEY="${AXLE_DEMO_PROJECT_KEY:-KAN}"
DEFAULT_LABEL="${AXLE_DEMO_LABEL:-axle-run}"
RUNS_DIR="${AXLE_DEMO_RUNS_DIR:-${CLI_HOME}/coordinator/runs}"
LOG_TAIL="${AXLE_DEMO_LOG_TAIL:-200}"

usage() {
  cat <<'EOF'
Usage:
  axle-demo-docker.sh logs
  axle-demo-docker.sh coordinator-logs
  axle-demo-docker.sh worker-logs
  axle-demo-docker.sh ps
  axle-demo-docker.sh health
  axle-demo-docker.sh config
  axle-demo-docker.sh restart
  axle-demo-docker.sh latest
  axle-demo-docker.sh latest-show
  axle-demo-docker.sh latest-watch
  axle-demo-docker.sh latest-issue <issue_key>
  axle-demo-docker.sh show-issue <issue_key>
  axle-demo-docker.sh watch-issue <issue_key>
  axle-demo-docker.sh pr-issue <issue_key>
  axle-demo-docker.sh last-pr
  axle-demo-docker.sh run <run_id>
  axle-demo-docker.sh watch <run_id>
  axle-demo-docker.sh show <run_id>
  axle-demo-docker.sh jira-trigger <payload.json>
  axle-demo-docker.sh jira-smoke
  axle-demo-docker.sh jira-multifile

Environment overrides:
  AXLE_DEMO_COORDINATOR_CONTAINER   default: axle-coordinator
  AXLE_DEMO_WORKER_CONTAINER        default: axle-worker
  AXLE_DEMO_BASE_URL                default: http://127.0.0.1
  AXLE_DEMO_ADMIN_TOKEN             default: change-me-admin
  AXLE_DEMO_POLL_SECONDS            default: 2
  AXLE_DEMO_LOG_TAIL                default: 200
  AXLE_DEMO_CLI_HOME                default: /data/.axle-cli
  AXLE_DEMO_RUNS_DIR                default: /data/.axle-cli/coordinator/runs
  AXLE_DEMO_ISSUE_KEY               default: KAN-9803
  AXLE_DEMO_PROJECT_KEY             default: KAN
  AXLE_DEMO_LABEL                   default: axle-run

Examples:
  ./ops/axle-demo-docker.sh logs
  ./ops/axle-demo-docker.sh watch 38b12550-f6c1-5c2f-9c92-a18bb047176e
  ./ops/axle-demo-docker.sh latest-watch
  ./ops/axle-demo-docker.sh watch-issue KAN-9803
  ./ops/axle-demo-docker.sh jira-multifile
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

fetch_issue_latest_run_json() {
  local issue_key="$1"
  curl -fsS "${BASE_URL}/api/issues/${issue_key}/runs/latest" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}"
}

fetch_issue_status_json() {
  local issue_key="$1"
  curl -fsS "${BASE_URL}/api/issues/${issue_key}/status" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}"
}

try_fetch_issue_status_json() {
  local issue_key="$1"
  local response
  response="$(curl -sS -w $'\n%{http_code}' "${BASE_URL}/api/issues/${issue_key}/status" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}")"
  ISSUE_STATUS_HTTP_CODE="${response##*$'\n'}"
  ISSUE_STATUS_BODY="${response%$'\n'*}"
}

docker_exec_coordinator() {
  docker exec "$COORDINATOR_CONTAINER" sh -lc "$1"
}

health_check() {
  curl -fsS "${BASE_URL}/healthz"
}

latest_run_id() {
  docker_exec_coordinator "latest=\$(ls -1t ${RUNS_DIR}/*.json 2>/dev/null | head -n 1); [ -n \"\$latest\" ] || exit 1; LATEST_RUN_FILE=\"\$latest\" python3 - <<'PY'
import json
import os
path = os.environ['LATEST_RUN_FILE']
with open(path, 'r', encoding='utf-8') as handle:
    print(json.load(handle)['run_id'])
PY"
}

latest_run_with_pr() {
  docker_exec_coordinator "python3 - <<'PY'
import glob
import json

paths = sorted(glob.glob('${RUNS_DIR}/*.json'))
for path in reversed(paths):
    with open(path, 'r', encoding='utf-8') as handle:
        payload = json.load(handle)
    if payload.get('pr_url'):
        print(payload['run_id'])
        break
PY"
}

issue_latest_run_id() {
  local issue_key="$1"
  local json
  json="$(fetch_issue_latest_run_json "$issue_key")"
  AXLE_DEMO_PAYLOAD="$json" python3 - <<'PY'
import json
import os
payload = json.loads(os.environ["AXLE_DEMO_PAYLOAD"])
print(payload["run_id"])
PY
}

build_demo_payload() {
  local event_id="$1"
  local summary="$2"
  local description="$3"

  python3 - "$event_id" "$summary" "$description" "$DEFAULT_ISSUE_KEY" "$DEFAULT_PROJECT_KEY" "$DEFAULT_LABEL" <<'PY'
import json
import sys

event_id, summary, description, issue_key, project_key, label = sys.argv[1:]

payload = {
    "event_id": event_id,
    "webhookEvent": "jira:issue_updated",
    "issue": {
        "key": issue_key,
        "fields": {
            "summary": summary,
            "description": description,
            "project": {"key": project_key},
            "labels": [label],
            "updated": "2026-04-09T18:00:00Z",
        },
    },
}
print(json.dumps(payload))
PY
}

post_jira_payload() {
  local payload_file="$1"
  curl -fsS -X POST "${BASE_URL}/api/webhooks/runs" \
    -H "Authorization: Bearer ${ADMIN_TOKEN}" \
    -H "Content-Type: application/json" \
    --data @"${payload_file}"
}

print_run_summary() {
  local payload
  payload="$(cat)"

  AXLE_DEMO_PAYLOAD="$payload" python3 - <<'PY'
import json
import os
import sys

payload = json.loads(os.environ["AXLE_DEMO_PAYLOAD"])
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

print_issue_status_summary() {
  local payload
  payload="$(cat)"

  AXLE_DEMO_PAYLOAD="$payload" python3 - <<'PY'
import json
import os

payload = json.loads(os.environ["AXLE_DEMO_PAYLOAD"])
changed_files = payload.get("changed_files") or []

print(f"issue_key: {payload.get('issue_key', '')}")
print(f"run_id: {payload.get('run_id', '')}")
print(f"status: {payload.get('status', '')}")
print(f"result: {payload.get('result', '')}")
print(f"last_stage: {payload.get('last_stage') or '-'}")
print(f"last_event: {payload.get('last_event') or '-'}")
if changed_files:
    print("changed_files:")
    for path in changed_files:
        print(f"  - {path}")
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
    status="$(AXLE_DEMO_PAYLOAD="$json" python3 - <<'PY'
import json
import os
print(json.loads(os.environ["AXLE_DEMO_PAYLOAD"]).get("status", ""))
PY
)"

    if [[ "$status" == "completed" || "$status" == "failed" ]]; then
      printf '\nfinal_status: %s\n' "$status"
      break
    fi

    sleep "$POLL_SECONDS"
  done
}

watch_issue() {
  local issue_key="$1"

  while true; do
    try_fetch_issue_status_json "$issue_key"
    clear
    if [[ "$ISSUE_STATUS_HTTP_CODE" == "404" ]]; then
      printf 'issue_key: %s\n' "$issue_key"
      printf 'status: waiting\n'
      printf 'last_event: waiting for first run to be created\n'
      sleep "$POLL_SECONDS"
      continue
    fi

    if [[ "$ISSUE_STATUS_HTTP_CODE" != "200" ]]; then
      printf 'issue_key: %s\n' "$issue_key"
      printf 'status: error\n'
      printf 'last_event: unexpected HTTP status %s\n' "$ISSUE_STATUS_HTTP_CODE"
      if [[ -n "$ISSUE_STATUS_BODY" ]]; then
        printf '\n%s\n' "$ISSUE_STATUS_BODY"
      fi
      return 1
    fi

    local json
    json="$ISSUE_STATUS_BODY"
    printf '%s\n' "$json" | print_issue_status_summary

    local status
    status="$(AXLE_DEMO_PAYLOAD="$json" python3 - <<'PY'
import json
import os
print(json.loads(os.environ["AXLE_DEMO_PAYLOAD"]).get("status", ""))
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

  docker logs "$COORDINATOR_CONTAINER" --tail "$LOG_TAIL" -f 2>&1 | sed 's/^/[coordinator] /' &
  docker logs "$WORKER_CONTAINER" --tail "$LOG_TAIL" -f 2>&1 | sed 's/^/[worker] /' &
  wait
}

coordinator_logs() {
  docker logs "$COORDINATOR_CONTAINER" --tail "$LOG_TAIL" -f
}

worker_logs() {
  docker logs "$WORKER_CONTAINER" --tail "$LOG_TAIL" -f
}

show_config() {
  cat <<EOF
coordinator_container: ${COORDINATOR_CONTAINER}
worker_container: ${WORKER_CONTAINER}
base_url: ${BASE_URL}
admin_token: ${ADMIN_TOKEN}
poll_seconds: ${POLL_SECONDS}
log_tail: ${LOG_TAIL}
cli_home: ${CLI_HOME}
runs_dir: ${RUNS_DIR}
issue_key: ${DEFAULT_ISSUE_KEY}
project_key: ${DEFAULT_PROJECT_KEY}
label: ${DEFAULT_LABEL}
EOF
}

main() {
  local cmd="${1:-}"
  local json
  local run_id
  local payload_file
  local event_id

  case "$cmd" in
    logs)
      combined_logs
      ;;
    coordinator-logs)
      coordinator_logs
      ;;
    worker-logs)
      worker_logs
      ;;
    ps)
      docker ps --filter "name=${COORDINATOR_CONTAINER}" --filter "name=${WORKER_CONTAINER}"
      ;;
    health)
      health_check
      ;;
    config)
      show_config
      ;;
    restart)
      docker restart "$COORDINATOR_CONTAINER" "$WORKER_CONTAINER"
      ;;
    latest)
      latest_run_id
      ;;
    latest-show)
      run_id="$(latest_run_id)"
      json="$(fetch_run_json "$run_id")"
      printf '%s\n' "$json" | print_run_summary
      ;;
    latest-watch)
      run_id="$(latest_run_id)"
      watch_run "$run_id"
      ;;
    latest-issue)
      require_arg "$@"
      issue_latest_run_id "$2"
      ;;
    show-issue)
      require_arg "$@"
      try_fetch_issue_status_json "$2"
      if [[ "$ISSUE_STATUS_HTTP_CODE" == "404" ]]; then
        printf 'issue_key: %s\n' "$2"
        printf 'status: not_found\n'
        printf 'last_event: no Axle run exists for this issue yet\n'
        exit 0
      fi
      if [[ "$ISSUE_STATUS_HTTP_CODE" != "200" ]]; then
        printf '%s\n' "$ISSUE_STATUS_BODY"
        exit 1
      fi
      json="$ISSUE_STATUS_BODY"
      printf '%s\n' "$json" | print_issue_status_summary
      ;;
    watch-issue)
      require_arg "$@"
      watch_issue "$2"
      ;;
    pr-issue)
      require_arg "$@"
      json="$(fetch_issue_status_json "$2")"
      AXLE_DEMO_PAYLOAD="$json" python3 - <<'PY'
import json
import os
payload = json.loads(os.environ["AXLE_DEMO_PAYLOAD"])
print(payload.get("pr_url", ""))
PY
      ;;
    last-pr)
      run_id="$(latest_run_with_pr)"
      [ -n "$run_id" ] || exit 1
      json="$(fetch_run_json "$run_id")"
      AXLE_DEMO_PAYLOAD="$json" python3 - <<'PY'
import json
import os
payload = json.loads(os.environ["AXLE_DEMO_PAYLOAD"])
print(payload.get("pr_url", ""))
PY
      ;;
    run|watch)
      require_arg "$@"
      watch_run "$2"
      ;;
    show)
      require_arg "$@"
      json="$(fetch_run_json "$2")"
      printf '%s\n' "$json" | print_run_summary
      ;;
    jira-trigger)
      require_arg "$@"
      post_jira_payload "$2"
      ;;
    jira-smoke)
      event_id="demo-smoke-$(date +%s)"
      payload_file="$(mktemp)"
      build_demo_payload \
        "$event_id" \
        "Change landing heading" \
        "Make only this change:\n\n1. In todos/templates/todos/index.html change the main heading from Todo List to My Tasks.\n\nDo not modify any other files.\nDo not change Python files.\nDo not add tests." \
        > "$payload_file"
      post_jira_payload "$payload_file"
      rm -f "$payload_file"
      ;;
    jira-multifile)
      event_id="demo-multifile-$(date +%s)"
      payload_file="$(mktemp)"
      build_demo_payload \
        "$event_id" \
        "Update HTML copy" \
        "Make only these changes:\n\n1. In todos/templates/todos/index.html change the main heading to My Tasks.\n2. In todos/templates/todos/base.html update the page title to My Tasks.\n\nDo not modify any other files.\nDo not change Python files.\nDo not add tests." \
        > "$payload_file"
      post_jira_payload "$payload_file"
      rm -f "$payload_file"
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
