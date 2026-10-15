#!/bin/sh
# omp-bridge task waiter for Hermes cells. Polls the bridge's long-poll endpoint until the task's
# revision advances past `after` AND its status is terminal-for-waiting (anything but queued/running),
# or the timeout lapses; then prints one JSON line and exits 0 so terminal(notify_on_complete) fires.
#
# usage: wait-task.sh <task_id> [after]
# Idempotent: re-arming a waiter for a finished task returns immediately with the current state.
set -u

if [ "${1:-}" = "--help" ] || [ "${1:-}" = "-h" ] || [ $# -lt 1 ]; then
    echo "usage: wait-task.sh <task_id> [after]"
    echo "Polls the omp-bridge until the task leaves queued/running, prints one JSON line, exits 0."
    echo "Header file: ${OMP_BRIDGE_HEADER:-/sandbox/.omp-bridge-header}"
    exit 0
fi

BASE_URL="${OMP_BRIDGE_URL:-http://host.openshell.internal:18780}"
HEADER_FILE="${OMP_BRIDGE_HEADER:-/sandbox/.omp-bridge-header}"
TASK_ID="$1"
AFTER="${2:-0}"

while :; do
    OUT=$(curl -fsS -m 3350 -H @"$HEADER_FILE" \
        "$BASE_URL/v1/wait/$TASK_ID?after=$AFTER&timeout=60" 2>/dev/null) || {
        sleep 5
        continue
    }
    STATUS=$(printf '%s' "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' 2>/dev/null) || {
        sleep 5
        continue
    }
    case "$STATUS" in
    queued | running)
        AFTER=$(printf '%s' "$OUT" | python3 -c 'import json,sys; print(json.load(sys.stdin)["revision"])')
        continue
        ;;
    *)
        printf '%s\n' "$OUT"
        exit 0
        ;;
    esac
done
