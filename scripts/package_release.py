#!/usr/bin/env python3
"""Build a deterministic source release without runtime state or secrets."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from candidate_optimize import _candidate_identity
from point_in_time_universe import load_history_manifest, load_universe

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "release"
EXCLUDE_DIRS = {".git", ".venv", "__pycache__", "logs", "reports", "data", "release", "dist", ".pytest_cache"}
EXCLUDE_NAMES = {"secrets.yaml", ".env"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip", ".tar.gz"}
INCLUDE_TOP = {"api_service", "codeact", "config", "scripts"}
INCLUDE_FILES = {
    "scheduler.py", "daily_pipeline.py", "codeact_sdk.py", "requirements.txt",
    "super-agent-api.service", "super-agent-scheduler.service", "Caddyfile.example",
    ".gitignore", "AGENTS.md", "龟缠量化v6_optimized.py", "先读我.md",
    "README-本地运行.md", "云端部署说明.md", "云端部署命令.md",
}


def ignored(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if path.name in EXCLUDE_NAMES or path.name == "secrets.yaml":
        return True
    return any(path.name.endswith(s) for s in EXCLUDE_SUFFIXES)


def main() -> int:
    gate = subprocess.run([sys.executable, str(ROOT / "scripts" / "quality_gate.py"), "--full"], cwd=ROOT)
    if gate.returncode:
        raise SystemExit("quality gate failed; release blocked")
    active_path = ROOT / "data" / "candidates" / "active_shadow.json"
    if not active_path.exists():
        raise SystemExit("sealed release approval missing; release blocked")
    active = json.loads(active_path.read_text(encoding="utf-8"))
    active_signature = active.get("candidate_meta", {}).get("evaluation", {}).get("candidate_signature")
    if not active_signature or _candidate_identity(active)[0] != active_signature:
        raise SystemExit("candidate code or parameters changed; release blocked")
    universe, universe_meta = load_universe()
    if (not universe_meta.get("complete") or
            (active.get("point_in_time_universe") or {}).get("sha256") != universe_meta.get("sha256")):
        raise SystemExit("historical universe changed or incomplete; release blocked")
    active_universe = active.get("point_in_time_universe") or {}
    history_meta = load_history_manifest(
        universe_meta.get("start", ""), universe_meta.get("end", ""), universe_meta.get("sha256", ""),
        universe_by_date=universe,
    )
    if (not history_meta.get("complete")
            or active_universe.get("stock_history_manifest_sha256") != history_meta.get("manifest_sha256")):
        raise SystemExit("stock history manifest changed or incomplete; release blocked")
    approval_path = ROOT / "data" / "candidates" / f"release_gate_{active_signature[:12]}.json"
    if not approval_path.exists():
        raise SystemExit("sealed release approval missing; release blocked")
    approval = json.loads(approval_path.read_text(encoding="utf-8"))
    if approval.get("decision") != "release_approved" or approval.get("candidate_signature") != active_signature:
        raise SystemExit("release approval does not match active candidate")
    version = sys.argv[1] if len(sys.argv) > 1 else datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    OUT.mkdir(exist_ok=True)
    archive = OUT / f"super-agent-{version}.zip"
    files = []
    for p in ROOT.rglob("*"):
        if not p.is_file() or ignored(p):
            continue
        rel = p.relative_to(ROOT)
        if rel.parts[0] in INCLUDE_TOP or str(rel) in INCLUDE_FILES:
            files.append((p, rel.as_posix()))
    files.sort(key=lambda x: x[1])
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for p, rel in files:
            z.write(p, rel)
        z.writestr("release_approval.json", json.dumps(approval, ensure_ascii=False, indent=2))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (archive.with_suffix(".sha256")).write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"RELEASE={version}")
    print(f"ARCHIVE={archive}")
    print(f"FILES={len(files)}")
    print(f"SHA256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
