#!/usr/bin/env bash
set -euo pipefail

CANDIDATE_ID=${1:?usage: remove_shadow_candidate.sh CANDIDATE_ID}
[[ "$CANDIDATE_ID" =~ ^[A-Za-z0-9_-]+$ ]] || { echo "invalid candidate id" >&2; exit 2; }
UNIT="super-agent-shadow-$CANDIDATE_ID"
sudo systemctl disable --now "$UNIT.timer" 2>/dev/null || true
sudo systemctl stop "$UNIT.service" 2>/dev/null || true
sudo rm -f "/etc/systemd/system/$UNIT.service" "/etc/systemd/system/$UNIT.timer"
sudo systemctl daemon-reload
echo "SHADOW_DISABLED $CANDIDATE_ID; release and audit data preserved"
