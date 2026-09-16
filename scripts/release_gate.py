#!/usr/bin/env python3
"""One-time sealed holdout gate. It never deploys or changes main parameters."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

from candidate_engine import MAX_DRAWDOWN_PCT, write_json
from candidate_optimize import _candidate_identity
from point_in_time_universe import load_history_manifest, load_universe

ROOT = Path(__file__).resolve().parent.parent
CANDIDATES = ROOT / "data" / "candidates"
REQUIRED_STATES = ("bull", "bear", "sideways")


def _load_core():
    spec = importlib.util.spec_from_file_location("guichan_release_gate", ROOT / "龟缠量化v6_optimized.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def _aggregate(rows: list[dict]) -> dict:
    return {
        "sharpe": round(sum(float(r["sharpe"]) for r in rows) / len(rows), 3),
        "return": round(sum(float(r["total_return"]) for r in rows) / len(rows), 2),
        "drawdown": round(min(float(r["max_drawdown"]) for r in rows), 2),
        "trades": sum(int(r["trades"]) for r in rows),
    }


def _sharpe_pass(candidate: float, baseline: float) -> bool:
    return candidate >= baseline * 1.1 if baseline > 0 else candidate >= baseline + 0.1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidate", type=Path, default=CANDIDATES / "active_shadow.json")
    parser.add_argument("--recommendation", type=Path, default=CANDIDATES / "promotion_recommendation.json")
    args = parser.parse_args(argv)
    candidate = json.loads(args.candidate.read_text(encoding="utf-8"))
    recommendation = json.loads(args.recommendation.read_text(encoding="utf-8"))
    evaluation = candidate.get("candidate_meta", {}).get("evaluation", {})
    signature = evaluation.get("candidate_signature")
    candidate_id = candidate.get("candidate_meta", {}).get("candidate_id")
    if not signature or evaluation.get("decision") != "shadow_ready":
        raise SystemExit("candidate did not pass walk-forward gate")
    if _candidate_identity(candidate)[0] != signature:
        raise SystemExit("candidate code or parameters changed since validation")
    if (recommendation.get("candidate_id") != candidate_id
            or recommendation.get("candidate_signature") != signature
            or not recommendation.get("paired_baseline", {}).get("passed")):
        raise SystemExit("candidate did not pass paired shadow gate")
    universe, universe_meta = load_universe()
    if not universe_meta.get("complete"):
        raise SystemExit("point-in-time universe is incomplete")
    candidate_universe = candidate.get("point_in_time_universe") or {}
    if (candidate_universe.get("sha256") != universe_meta.get("sha256")
            or candidate_universe.get("stock_data_coverage") != 1.0):
        raise SystemExit("candidate historical universe or stock bars are incomplete or changed")

    rows = {row.get("state"): row for row in candidate.get("results", [])}
    if any(state not in rows for state in REQUIRED_STATES):
        raise SystemExit("required state missing")
    periods = [rows[state].get("sealed_period") for state in REQUIRED_STATES]
    if any(not period or period.get("status") != "unread" for period in periods):
        raise SystemExit("sealed period is unavailable")
    period_key = f"{periods[0]['start']}~{periods[0]['end']}"
    if any(f"{period['start']}~{period['end']}" != period_key for period in periods):
        raise SystemExit("sealed periods differ by state")

    ledger_path = CANDIDATES / "sealed_ledger.json"
    try:
        ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        ledger = {"periods": {}}
    consumed = ledger["periods"].get(period_key)
    if consumed:
        if consumed.get("candidate_signature") == signature:
            print(json.dumps(consumed["result"], ensure_ascii=False, indent=2))
            return 0 if consumed["result"].get("decision") == "release_approved" else 1
        raise SystemExit("sealed period was already consumed by another candidate")

    lock_dir = CANDIDATES / "sealed_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / hashlib.sha256(period_key.encode("ascii")).hexdigest()
    try:
        with lock_path.open("x", encoding="utf-8") as fh:
            fh.write(signature)
    except FileExistsError:
        raise SystemExit("sealed period is reserved or previously consumed")
    ledger["periods"][period_key] = {"candidate_signature": signature, "result": {
        "decision": "release_rejected", "reason": "sealed period reserved before reading prices",
    }}
    write_json(ledger_path, ledger)

    core = _load_core()
    data = {}
    for code in candidate.get("stock_codes", []):
        frame = core.load_stock_data(code)
        if frame is not None and len(frame) >= 120:
            data[code] = frame
    if len(data) != len(candidate.get("stock_codes", [])) or len(data) < 3:
        raise SystemExit("sealed evaluation has fewer than 3 stocks")
    timeline = core.load_market_states()
    start, end = periods[0]["start"], periods[0]["end"]
    history_meta = load_history_manifest(start, end, universe_meta["sha256"],
                                         universe_by_date=universe)
    if (not history_meta.get("complete")
            or history_meta.get("manifest_sha256") != candidate_universe.get("stock_history_manifest_sha256")):
        raise SystemExit("candidate stock history manifest changed or is incomplete")
    candidate_results = []
    baseline_results = []
    for state in REQUIRED_STATES:
        entry_dates = {item["date"] for item in timeline
                       if item.get("state") == state and start <= item.get("date", "") <= end}
        candidate_results.append(core.backtest_multi_stocks(
            list(data), rows[state]["params"], data,
            start_date=start, end_date=end, entry_dates=entry_dates,
            universe_by_date=universe,
        ))
        baseline_results.append(core.backtest_multi_stocks(
            list(data), core.STATE_BASELINES.get(state, core.BASELINE_PARAMS), data,
            start_date=start, end_date=end, entry_dates=entry_dates,
            universe_by_date=universe,
        ))
    candidate_metrics = _aggregate(candidate_results)
    baseline_metrics = _aggregate(baseline_results)
    failures = []
    if candidate_metrics["trades"] < 20:
        failures.append("封存集平仓少于20笔")
    if not _sharpe_pass(candidate_metrics["sharpe"], baseline_metrics["sharpe"]):
        failures.append("封存集夏普未提升10%")
    if candidate_metrics["return"] < baseline_metrics["return"]:
        failures.append("封存集收益低于基准")
    if abs(candidate_metrics["drawdown"]) > MAX_DRAWDOWN_PCT:
        failures.append("封存集回撤超过10%")
    if abs(candidate_metrics["drawdown"]) > abs(baseline_metrics["drawdown"]):
        failures.append("封存集回撤劣于基准")
    result = {
        "candidate_id": candidate_id,
        "candidate_signature": signature,
        "sealed_period": period_key,
        "candidate_metrics": candidate_metrics,
        "baseline_metrics": baseline_metrics,
        "decision": "release_approved" if not failures else "release_rejected",
        "failures": failures,
        "main_params_unchanged": True,
    }
    ledger["periods"][period_key] = {"candidate_signature": signature, "result": result}
    write_json(ledger_path, ledger)
    write_json(CANDIDATES / f"release_gate_{signature[:12]}.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
