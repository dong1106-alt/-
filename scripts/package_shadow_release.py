#!/usr/bin/env python3
"""Package a walk-forward-approved candidate for isolated cloud shadowing only."""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

from candidate_optimize import _candidate_identity
from package_release import INCLUDE_FILES, INCLUDE_TOP, ignored

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CANDIDATE = ROOT / "data" / "historical_validation" / "2018-2021" / "candidates" / "active_shadow.json"
OUT = ROOT / "release"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, default=DEFAULT_CANDIDATE)
    args = parser.parse_args(argv)
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    meta = candidate.get("candidate_meta", {})
    evaluation = meta.get("evaluation", {})
    signature = evaluation.get("candidate_signature")
    candidate_id = meta.get("candidate_id", "")
    if evaluation.get("decision") != "shadow_ready" or not signature:
        raise SystemExit("candidate did not pass walk-forward gate")
    if _candidate_identity(candidate)[0] != signature:
        raise SystemExit("candidate code or parameters changed since validation")
    if not candidate_id.replace("-", "").replace("_", "").isalnum():
        raise SystemExit("candidate id is unsafe")
    gate = subprocess.run([sys.executable, str(ROOT / "scripts" / "quality_gate.py"), "--full"], cwd=ROOT)
    if gate.returncode:
        raise SystemExit("quality gate failed; shadow package blocked")

    version = f"{candidate_id}-{signature[:12]}"
    OUT.mkdir(exist_ok=True)
    archive = OUT / f"super-agent-shadow-{version}.zip"
    files = []
    for path in ROOT.rglob("*"):
        if not path.is_file() or ignored(path):
            continue
        relative = path.relative_to(ROOT)
        if relative.parts[0] in INCLUDE_TOP or str(relative) in INCLUDE_FILES:
            files.append((path, relative.as_posix()))
    files.sort(key=lambda item: item[1])
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path, relative in files:
            bundle.write(path, relative)
        bundle.writestr("shadow_candidate.json", json.dumps(candidate, ensure_ascii=False, indent=2))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    archive.with_suffix(".sha256").write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    print(f"CANDIDATE={candidate_id}")
    print(f"ARCHIVE={archive}")
    print(f"SHA256={digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
