#!/usr/bin/env bash
set -euo pipefail

ARCHIVE=${1:?usage: install_shadow_candidate.sh /tmp/super-agent-shadow-CANDIDATE.zip}
BASE=/opt/super-agent
STAGE=$(mktemp -d "$BASE/.shadow-stage.XXXXXX")
trap 'rm -rf "$STAGE"' EXIT

unzip -q "$ARCHIVE" -d "$STAGE"
CANDIDATE_ID=$(python3 - "$STAGE/shadow_candidate.json" <<'PY'
import json, re, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
meta = payload.get("candidate_meta", {})
candidate_id = meta.get("candidate_id", "")
decision = meta.get("evaluation", {}).get("decision")
if decision != "shadow_ready" or not re.fullmatch(r"[A-Za-z0-9_-]+", candidate_id):
    raise SystemExit("invalid or unapproved shadow candidate")
print(candidate_id)
PY
)

RELEASE="$BASE/shadow-releases/$CANDIDATE_ID"
DATA="$BASE/shadow-data/$CANDIDATE_ID"
UNIT="super-agent-shadow-$CANDIDATE_ID"
sudo test ! -e "$RELEASE" || { echo "shadow release exists: $RELEASE" >&2; exit 1; }
sudo test ! -e "$DATA" || { echo "shadow data exists: $DATA" >&2; exit 1; }
sudo test -f "$BASE/data/sim_trades/portfolio.json"
sudo test -x "$BASE/.venv/bin/python"

sudo mkdir -p "$BASE/shadow-releases" "$BASE/shadow-data"
sudo cp -a "$BASE/data" "$DATA"
sudo rm -rf "$DATA/shadow"
sudo mkdir -p "$DATA/candidates" "$DATA/logs" "$DATA/reports"
sudo install -m 640 "$STAGE/shadow_candidate.json" "$DATA/candidates/active_shadow.json"
sudo mv "$STAGE" "$RELEASE"
sudo ln -s "$BASE/.venv" "$RELEASE/.venv"
sudo ln -s "$DATA" "$RELEASE/data"
sudo ln -s "$DATA/logs" "$RELEASE/logs"
sudo ln -s "$DATA/reports" "$RELEASE/reports"
sudo chown -R superagent:superagent "$RELEASE" "$DATA"

sudo tee "/etc/systemd/system/$UNIT.service" >/dev/null <<EOF
[Unit]
Description=Super Agent Isolated Shadow $CANDIDATE_ID
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
User=superagent
Group=superagent
WorkingDirectory=$RELEASE
EnvironmentFile=-/etc/super-agent/super-agent.env
Environment=TZ=Asia/Shanghai
ExecStart=$BASE/.venv/bin/python -u $RELEASE/scripts/shadow_pipeline.py
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=full
ReadWritePaths=$DATA
EOF

sudo tee "/etc/systemd/system/$UNIT.timer" >/dev/null <<EOF
[Unit]
Description=Run Super Agent Isolated Shadow $CANDIDATE_ID
[Timer]
OnCalendar=Mon..Fri *-*-* 18:10:00 Asia/Shanghai
Persistent=true
Unit=$UNIT.service
[Install]
WantedBy=timers.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now "$UNIT.timer"
sudo systemctl start "$UNIT.service"
sudo systemctl is-active --quiet "$UNIT.timer"
sudo test "$(readlink -f "$BASE/current")" != "$(readlink -f "$RELEASE")"
echo "SHADOW_DEPLOY_OK $CANDIDATE_ID"
echo "ROLLBACK: sudo $RELEASE/scripts/remove_shadow_candidate.sh $CANDIDATE_ID"
