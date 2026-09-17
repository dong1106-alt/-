#!/usr/bin/env python3
"""Historical validation must use an isolated data root and a frozen profile."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROFILE = ROOT / "config" / "historical_validation_2018_2021.json"
profile = json.loads(PROFILE.read_text(encoding="utf-8"))

assert profile["profile_id"] == "historical-2018-2021-v1"
assert profile["evaluation_start"] == "2018-01-01"
assert profile["sealed_start"] == "2021-06-30"
assert len(profile["validation_periods"]) == 8

with tempfile.TemporaryDirectory(prefix="historical_paths_") as tmp:
    env = dict(os.environ, SUPER_AGENT_DATA_ROOT=tmp)
    env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "scripts")])
    code = (
        "import json; from config.loader import load_config; "
        "from point_in_time_universe import DEFAULT_SNAPSHOT,DEFAULT_STOCK_DIR; "
        "c=load_config(); print(json.dumps({'data':c['paths']['data_dir'],"
        "'stocks':c['paths']['stock_data_dir'],'snapshot':str(DEFAULT_SNAPSHOT),"
        "'default_stocks':str(DEFAULT_STOCK_DIR)}))"
    )
    output = subprocess.check_output([sys.executable, "-c", code], cwd=ROOT, env=env, text=True)
    paths = json.loads(output)
    expected = str(Path(tmp).resolve())
    assert paths["data"] == expected
    assert paths["stocks"] == str(Path(expected) / "stocks")
    assert paths["snapshot"] == str(Path(expected) / "universe" / "point_in_time.json.gz")
    assert paths["default_stocks"] == str(Path(expected) / "stocks")

env = dict(os.environ)
env.pop("SUPER_AGENT_DATA_ROOT", None)
env["PYTHONPATH"] = os.pathsep.join([str(ROOT), str(ROOT / "scripts")])
default_data = subprocess.check_output(
    [sys.executable, "-c", "from config.loader import load_config; print(load_config()['paths']['data_dir'])"],
    cwd=ROOT, env=env, text=True,
).strip()
assert Path(default_data) == ROOT / "data"
print("historical_validation_isolation_ok")
