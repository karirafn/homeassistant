#!/bin/bash
set -e

REPO=/mnt/user/appdata/homeassistant
CONTAINER=homeassistant
# Gitignored, so `git clean -fd` leaves it in place between runs.
REJECTED_FILE="$REPO/.deploy-rejected"
RESTART_TIMEOUT_SECONDS=120
# YAML-mode dashboards reload on browser refresh and need no restart.
NO_RESTART_PATHS_REGEX='^(views/|ui-lovelace\.yaml$)'

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S'): $*"
}

needs_restart() {
    git -C "$REPO" diff --name-only --no-renames "$1" "$2" | grep -qvE "$NO_RESTART_PATHS_REGEX"
}

git -C "$REPO" fetch -q origin main

LOCAL=$(git -C "$REPO" rev-parse HEAD)
REMOTE=$(git -C "$REPO" rev-parse origin/main)

if [ "$LOCAL" = "$REMOTE" ]; then
    exit 0
fi

if [ -f "$REJECTED_FILE" ] && [ "$(cat "$REJECTED_FILE")" = "$REMOTE" ]; then
    exit 0
fi

log "Changes detected ($LOCAL -> $REMOTE), resetting..."
RESTART_REQUIRED=false
if needs_restart "$LOCAL" "$REMOTE"; then
    RESTART_REQUIRED=true
fi

git -C "$REPO" reset -q --hard origin/main
git -C "$REPO" clean -fdq
log "Reset complete"

if [ "$RESTART_REQUIRED" = false ]; then
    log "Only dashboard files changed, restart skipped"
    exit 0
fi

# check_config exits 0 for integration-level errors (bad template, invalid automation
# trigger) and only logs them as "ERROR:" lines, so the exit code alone cannot gate.
check_failed() {
    local exit_code="$1" output="$2"
    [ "$exit_code" -ne 0 ] || grep -q '^ERROR:' <<< "$output"
}

CHECK_EXIT=0
CHECK_OUTPUT=$(docker exec "$CONTAINER" python3 -m homeassistant --script check_config -c /config 2>&1) || CHECK_EXIT=$?

if check_failed "$CHECK_EXIT" "$CHECK_OUTPUT"; then
    log "check_config failed for $REMOTE, rolling back to $LOCAL"
    echo "$CHECK_OUTPUT"
    git -C "$REPO" reset -q --hard "$LOCAL"
    git -C "$REPO" clean -fdq
    echo "$REMOTE" > "$REJECTED_FILE"
    log "Rolled back, restart skipped"
    exit 1
fi

rm -f "$REJECTED_FILE"
log "Config valid, restarting Home Assistant..."
docker restart -t "$RESTART_TIMEOUT_SECONDS" "$CONTAINER" > /dev/null
log "Restart triggered"
