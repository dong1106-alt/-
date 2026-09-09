#!/usr/bin/env bash
set -euo pipefail
BASE=/opt/super-agent
CURRENT=$(readlink -f "$BASE/current")
PREV=$(find "$BASE/releases" -mindepth 1 -maxdepth 1 -type d ! -path "$CURRENT" | sort | tail -1)
[ -n "$PREV" ] || { echo 'no previous release' >&2; exit 1; }
sudo ln -sfn "$PREV" "$BASE/current.next"
sudo mv -Tf "$BASE/current.next" "$BASE/current"
sudo systemctl daemon-reload
sudo systemctl restart super-agent-api super-agent-scheduler
sleep 4
curl -fsS http://127.0.0.1:8000/health >/dev/null
echo "ROLLBACK_OK $(basename "$PREV")"
