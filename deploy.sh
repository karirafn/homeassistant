#!/bin/bash
set -e

REPO=/mnt/user/appdata/homeassistant
LOG_PREFIX="$(date '+%Y-%m-%d %H:%M:%S')"

git -C "$REPO" fetch origin main

LOCAL=$(git -C "$REPO" rev-parse HEAD)
REMOTE=$(git -C "$REPO" rev-parse origin/main)

if [ "$LOCAL" != "$REMOTE" ]; then
    echo "$LOG_PREFIX: Changes detected ($LOCAL -> $REMOTE), resetting..."
    git -C "$REPO" reset --hard origin/main
    git -C "$REPO" clean -fd
    echo "$LOG_PREFIX: Reset complete"
fi
