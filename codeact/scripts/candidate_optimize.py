#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成全状态参数候选并做样本外预筛；永不覆盖 optimal_params.json。"""
from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from candidate_engine import evaluate_backtest, write_json

CANDIDATES = ROOT / "data" / "candidates"


def _load_core():
    spec = importlib.util.spec_from_file_location("guichan_v6", ROOT / "龟缠量化v6_optimized.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    today = dt.date.today()
    if today.weekday() >= 5:
        print("[candidate] 周末，跳过")
        return 0
    if (ROOT / "data" / f"not_trading_day_{today.isoformat()}.txt").exists():
        print("[candidate] 非交易日，跳过")
        return 0

    monthly = "--monthly" in argv
    candidate_id = f"candidate-{today:%Y%m%d}" + ("-monthly" if monthly else "")
    raw_path = CANDIDATES / "raw" / f"{candidate_id}.json"
    partial_path = CANDIDATES / "partial" / f"{candidate_id}.json"
    n_trials, n_stocks = (50, 50) if monthly else (10, 20)
    print(f"[candidate] {candidate_id}: {n_stocks}只股票 × {n_trials}轮，全状态样本外评估")

    try:
        output = _load_core().run_optimization(
            n_trials=n_trials, n_stocks=n_stocks,
            output_path=str(raw_path), partial_path=str(partial_path),
        )
    except Exception as exc:
        evaluation = {"candidate_id": candidate_id, "decision": "rejected", "reason": f"优化异常：{type(exc).__name__}: {exc}"}
        write_json(CANDIDATES / "evaluations" / f"{candidate_id}.json", evaluation)
        print(evaluation["reason"])
        return 1

    if not output:
        evaluation = {"candidate_id": candidate_id, "decision": "rejected", "reason": "优化未返回候选"}
        write_json(CANDIDATES / "evaluations" / f"{candidate_id}.json", evaluation)
        print(evaluation["reason"])
        return 1

    evaluation = evaluate_backtest(output)
    evaluation.update({"candidate_id": candidate_id, "created_at": dt.datetime.now().isoformat(timespec="seconds"), "raw_path": str(raw_path)})
    write_json(CANDIDATES / "evaluations" / f"{candidate_id}.json", evaluation)

    if evaluation["decision"] == "shadow_ready":
        active = dict(output)
        active["candidate_meta"] = {"candidate_id": candidate_id, "evaluation": evaluation}
        write_json(CANDIDATES / "active_shadow.json", active)
        print(f"[candidate] 通过预筛，下一交易日进入影子验证：{candidate_id}")
    else:
        print(f"[candidate] 已拒绝：{evaluation['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
