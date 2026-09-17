#!/usr/bin/env python3
"""Prepare and consume the fixed 2018-2021 historical validation profile once."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
PROFILE_PATH = ROOT / "config" / "historical_validation_2018_2021.json"
DATA_ROOT = ROOT / "data" / "historical_validation" / "2018-2021"
PREPARED_PATH = DATA_ROOT / "prepared.json"
GLOBAL_LEDGER = ROOT / "data" / "candidates" / "validation_ledger.json"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _profile() -> dict:
    profile = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    if profile.get("schema") != 1 or profile.get("profile_id") != "historical-2018-2021-v1":
        raise RuntimeError("historical validation profile is invalid")
    return profile


def _set_environment(profile: dict) -> None:
    os.environ.update({
        "SUPER_AGENT_DATA_ROOT": str(DATA_ROOT),
        "SUPER_AGENT_CANDIDATES_DIR": str(DATA_ROOT / "candidates"),
        "SUPER_AGENT_VALIDATION_LEDGER": str(GLOBAL_LEDGER),
        "SUPER_AGENT_EVALUATION_START": profile["evaluation_start"],
        "SUPER_AGENT_CANDIDATE_ID": profile["candidate_id"],
        "SUPER_AGENT_VALIDATION_RESERVATION": profile["profile_id"],
        "SUPER_AGENT_VALIDATION_PROFILE_ID": profile["profile_id"],
        "SUPER_AGENT_VALIDATION_PROFILE_SHA256": _sha256(PROFILE_PATH),
    })


def _load_core():
    spec = importlib.util.spec_from_file_location("guichan_historical", ROOT / "龟缠量化v6_optimized.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _git_head() -> str:
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()


def _require_clean_code() -> None:
    changed = subprocess.check_output(
        ["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT, text=True
    ).strip()
    if changed:
        raise RuntimeError("tracked files changed; commit and pass CI before freezing validation data")


def _fold_audit(profile: dict, timeline: list[dict]) -> tuple[list[list[str]], dict[str, int]]:
    from causal_backtest import build_walk_forward_folds

    dates = [pd.Timestamp(row["date"]) for row in timeline
             if profile["evaluation_start"] <= row["date"] <= profile["evaluation_end"]]
    folds, sealed = build_walk_forward_folds(dates)
    periods = [[str(f.validation_start)[:10], str(f.validation_end)[:10]] for f in folds]
    if periods != profile["validation_periods"]:
        raise RuntimeError("market calendar changed the frozen validation periods")
    if [str(sealed[0])[:10], str(sealed[-1])[:10]] != [profile["sealed_start"], profile["sealed_end"]]:
        raise RuntimeError("sealed period changed")
    eligible = {state: 0 for state in ("bull", "bear", "sideways")}
    for fold in folds:
        states = {row["state"] for row in timeline
                  if str(fold.validation_start)[:10] <= row["date"] <= str(fold.validation_end)[:10]}
        for state in eligible:
            eligible[state] += int(state in states)
    if eligible != profile["required_eligible_folds"]:
        raise RuntimeError(f"market-state coverage changed: {eligible}")
    return periods, eligible


def _prepared_payload(profile: dict) -> dict:
    files = {
        "universe": DATA_ROOT / "universe" / "point_in_time.json.gz",
        "universe_metadata": DATA_ROOT / "universe" / "metadata.json",
        "history_manifest": DATA_ROOT / "universe" / "stock_history_manifest.json",
        "index": DATA_ROOT / "index" / "sh000001.parquet",
        "timeline": DATA_ROOT / "market_state_timeline.json",
    }
    missing = [str(path) for path in files.values() if not path.exists()]
    if missing:
        raise RuntimeError("historical data files missing: " + ", ".join(missing))
    timeline = json.loads(files["timeline"].read_text(encoding="utf-8"))
    periods, eligible = _fold_audit(profile, timeline)
    return {
        "profile_id": profile["profile_id"],
        "profile_sha256": _sha256(PROFILE_PATH),
        "git_head": _git_head(),
        "validation_periods": periods,
        "eligible_folds": eligible,
        "files": {name: {"path": str(path), "sha256": _sha256(path)}
                  for name, path in files.items()},
    }


def prepare(profile: dict, workers: int) -> int:
    _require_clean_code()
    from backfill_history import main as backfill
    from point_in_time_universe import build_universe, load_history_manifest, load_universe, update_index_history

    universe, metadata = load_universe()
    if (not metadata.get("complete") or metadata.get("start") != profile["evaluation_start"]
            or metadata.get("end") != profile["evaluation_end"]):
        metadata = build_universe(profile["evaluation_start"], profile["evaluation_end"])
        universe, metadata = load_universe()
    if not metadata.get("complete"):
        raise RuntimeError("historical point-in-time universe is incomplete")

    index_path = DATA_ROOT / "index" / "sh000001.parquet"
    index_ok = False
    if index_path.exists():
        index = pd.read_parquet(index_path)
        index_dates = pd.to_datetime(index["date"])
        index_ok = (index_dates.min().strftime("%Y-%m-%d") <= profile["data_start"]
                    and index_dates.max().strftime("%Y-%m-%d") >= profile["evaluation_end"])
    if not index_ok:
        update_index_history(profile["data_start"], profile["evaluation_end"], index_path)
    _load_core().generate_timeline()

    result = backfill([
        "--start", profile["data_start"], "--end", profile["evaluation_end"],
        "--workers", str(workers),
    ])
    if result:
        return result
    history = load_history_manifest(
        profile["evaluation_start"], profile["evaluation_end"], metadata["sha256"],
        universe_by_date=universe,
    )
    if not history.get("complete"):
        raise RuntimeError(history.get("reason", "historical stock data is incomplete"))
    payload = _prepared_payload(profile)
    PREPARED_PATH.parent.mkdir(parents=True, exist_ok=True)
    PREPARED_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _verify_prepared(profile: dict) -> dict:
    _require_clean_code()
    prepared = json.loads(PREPARED_PATH.read_text(encoding="utf-8"))
    if prepared.get("profile_sha256") != _sha256(PROFILE_PATH):
        raise RuntimeError("validation profile changed after data preparation")
    if prepared.get("git_head") != _git_head():
        raise RuntimeError("code changed after data preparation")
    for item in prepared.get("files", {}).values():
        path = Path(item["path"])
        if not path.exists() or _sha256(path) != item["sha256"]:
            raise RuntimeError(f"prepared data changed: {path}")
    current = _prepared_payload(profile)
    if current["validation_periods"] != prepared.get("validation_periods"):
        raise RuntimeError("prepared validation periods changed")
    return prepared


def evaluate(profile: dict) -> int:
    prepared = _verify_prepared(profile)
    gate = subprocess.run([sys.executable, str(ROOT / "scripts" / "quality_gate.py"), "--full"], cwd=ROOT)
    if gate.returncode:
        raise RuntimeError("full quality gate failed")

    import candidate_optimize

    try:
        ledger = json.loads(GLOBAL_LEDGER.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        ledger = {}
    existing = ledger.get("reservations", {}).get(profile["profile_id"])
    if existing:
        evaluation_path = DATA_ROOT / "candidates" / "evaluations" / f"{profile['candidate_id']}.json"
        if existing.get("status") == "consumed" and evaluation_path.exists():
            print(evaluation_path.read_text(encoding="utf-8"))
            return 0
        raise RuntimeError("historical validation was reserved but not completed; rerun is forbidden")

    candidate_optimize.reserve_validation_periods(
        profile["profile_id"], [tuple(period) for period in prepared["validation_periods"]],
        prepared["profile_sha256"],
    )
    return candidate_optimize.main(["--historical"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("prepare", "evaluate", "status"))
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)
    profile = _profile()
    _set_environment(profile)
    if args.command == "prepare":
        return prepare(profile, args.workers)
    if args.command == "evaluate":
        return evaluate(profile)
    if PREPARED_PATH.exists():
        print(PREPARED_PATH.read_text(encoding="utf-8"))
    else:
        print(json.dumps({"profile_id": profile["profile_id"], "status": "not_prepared"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
