#!/usr/bin/env python3
"""Verify the immutable source and main parameter snapshot at service startup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    files = sorted(
        path for path in root.rglob("*.py")
        if not any(part in {".venv", ".git", "__pycache__", "data"} for part in path.parts)
    )
    for path in files:
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def verify(root: Path | None = None) -> dict:
    root = (root or Path(__file__).resolve().parent.parent).resolve()
    manifest_path = root / "protection_manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"运行保护清单缺失：{manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema") != 1 or manifest.get("mode") != "existing_strategy_protection":
        raise RuntimeError("运行保护清单类型无效")
    params_path = root / "data" / "optimal_params.json"
    if not params_path.exists():
        raise RuntimeError(f"主参数文件缺失：{params_path}")
    params_digest = hashlib.sha256(params_path.read_bytes()).hexdigest()
    if params_digest != manifest.get("params_sha256"):
        raise RuntimeError("主参数哈希变化，拒绝启动当前发布版本")
    if source_digest(root) != manifest.get("source_sha256"):
        raise RuntimeError("源码哈希变化，拒绝启动当前发布版本")
    return manifest


def verify_if_enabled(root: Path | None = None) -> dict | None:
    if os.environ.get("SUPER_AGENT_RUNTIME_GUARD") != "1":
        return None
    return verify(root)
