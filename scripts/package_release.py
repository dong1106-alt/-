#!/usr/bin/env python3
"""Build a deterministic source release without runtime state or secrets."""
from __future__ import annotations

import hashlib
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "release"
EXCLUDE_DIRS = {".git", ".venv", "__pycache__", "logs", "reports", "data", "release", "dist", ".pytest_cache"}
EXCLUDE_NAMES = {"secrets.yaml", ".env"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".zip", ".tar.gz"}
INCLUDE_TOP = {"api_service", "codeact", "config", "scripts"}
INCLUDE_FILES = {
    "scheduler.py", "daily_pipeline.py", "codeact_sdk.py", "requirements.txt",
    "super-agent-api.service", "super-agent-scheduler.service", "Caddyfile.example",
    ".gitignore", "先读我.md", "README-本地运行.md", "云端部署说明.md", "云端部署命令.md",
}


def ignored(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in EXCLUDE_DIRS for part in rel.parts):
        return True
    if path.name in EXCLUDE_NAMES or path.name == "secrets.yaml":
        return True
    return any(path.name.endswith(s) for s in EXCLUDE_SUFFIXES)


def main() -> int:
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
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (archive.with_suffix(".sha256")).write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"RELEASE={version}")
    print(f"ARCHIVE={archive}")
    print(f"FILES={len(files)}")
    print(f"SHA256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
