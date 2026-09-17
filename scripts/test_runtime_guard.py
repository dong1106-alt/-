#!/usr/bin/env python3
"""Runtime protection accepts an intact release and rejects source drift."""
import hashlib
import json
import tempfile
from pathlib import Path

from runtime_guard import source_digest, verify

with tempfile.TemporaryDirectory(prefix="runtime_guard_") as tmp:
    root = Path(tmp)
    (root / "data").mkdir()
    (root / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    params = root / "data" / "optimal_params.json"
    params.write_text("{}\n", encoding="utf-8")
    manifest = {
        "schema": 1,
        "mode": "existing_strategy_protection",
        "source_sha256": source_digest(root),
        "params_sha256": hashlib.sha256(params.read_bytes()).hexdigest(),
    }
    (root / "protection_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    verify(root)
    (root / "main.py").write_text("VALUE = 2\n", encoding="utf-8")
    try:
        verify(root)
    except RuntimeError as exc:
        assert "源码哈希变化" in str(exc)
    else:
        raise AssertionError("source drift was accepted")
print("runtime_guard_ok")
