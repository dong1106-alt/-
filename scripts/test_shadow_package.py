#!/usr/bin/env python3
"""Cloud shadow installer must stay isolated from the production release pointer and API."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
install = (ROOT / "scripts" / "install_shadow_candidate.sh").read_text(encoding="utf-8")
remove = (ROOT / "scripts" / "remove_shadow_candidate.sh").read_text(encoding="utf-8")

assert '"$BASE/shadow-releases/$CANDIDATE_ID"' in install
assert '"$BASE/shadow-data/$CANDIDATE_ID"' in install
assert "shadow_pipeline.py" in install
assert "super-agent-api" not in install
assert "ln -sfn" not in install
assert 'readlink -f "$BASE/current"' in install
assert "release and audit data preserved" in remove
print("shadow_package_isolation_ok")
