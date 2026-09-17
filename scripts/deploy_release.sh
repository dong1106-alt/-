#!/usr/bin/env bash
set -euo pipefail

ARCHIVE=${1:?usage: deploy_release.sh /tmp/super-agent-VERSION.zip VERSION}
VERSION=${2:?usage: deploy_release.sh /tmp/super-agent-VERSION.zip VERSION}
BASE=/opt/super-agent
RELEASE="$BASE/releases/$VERSION"
STAGE="$(mktemp -d "$BASE/.stage.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT

unzip -q "$ARCHIVE" -d "$STAGE"
python3 - <<PY
import json
from pathlib import Path
p = Path("$STAGE/release_approval.json")
if not p.exists() or json.loads(p.read_text(encoding="utf-8")).get("decision") not in {"release_approved", "protection_approved"}:
    raise SystemExit("sealed release approval missing or rejected")
PY
python3 - <<PY
import compileall, sys
if not compileall.compile_dir("$STAGE", quiet=1):
    raise SystemExit("release compile failed")
PY

sudo mkdir -p "$BASE/releases" "$BASE/data" "$BASE/logs" "$BASE/reports"
sudo test ! -e "$RELEASE" || { echo "release exists: $RELEASE" >&2; exit 1; }
sudo mv "$STAGE" "$RELEASE"
for d in data logs reports .venv; do
  sudo ln -sfn "$BASE/$d" "$RELEASE/$d"
done
sudo chown -R superagent:superagent "$RELEASE"
sudo "$BASE/.venv/bin/python" "$RELEASE/scripts/write_runtime_manifest.py" \
  --root "$RELEASE" --params "$BASE/data/optimal_params.json"
sudo install -m 644 "$RELEASE/super-agent-api.service" /etc/systemd/system/super-agent-api.service
sudo install -m 644 "$RELEASE/super-agent-scheduler.service" /etc/systemd/system/super-agent-scheduler.service
sudo ln -sfn "$RELEASE" "$BASE/current.next"
sudo mv -Tf "$BASE/current.next" "$BASE/current"
sudo systemctl daemon-reload
sudo systemctl restart super-agent-api super-agent-scheduler
sleep 4
curl -fsS http://127.0.0.1:8000/health >/dev/null
echo "DEPLOY_OK $VERSION"
