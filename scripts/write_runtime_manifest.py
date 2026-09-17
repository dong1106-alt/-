#!/usr/bin/env python3
"""Write the cloud runtime protection manifest after staging a release."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from runtime_guard import source_digest


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--params", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    params = args.params.resolve()
    if not params.exists():
        raise SystemExit(f"主参数文件不存在：{params}")
    payload = {
        "schema": 1,
        "mode": "existing_strategy_protection",
        "source_sha256": source_digest(root),
        "params_sha256": hashlib.sha256(params.read_bytes()).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    (root / "protection_manifest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
