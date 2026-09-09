#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成全状态参数候选并做样本外预筛；永不覆盖 optimal_params.json。

每日模式（2026-09-09修复超时）：单日跑全部4状态曾超过30分钟被超时击杀。
现在每天只轮换优化2个状态，通过稳定断点文件跨天累积；凑满必需状态
（bull/bear/sideways，transition可选）后才做样本外评估。月度模式不变。
"""
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

ALL_STATES = ("bull", "bear", "sideways", "transition")
REQUIRED_STATES = ("bull", "bear", "sideways")  # 与 candidate_engine.REQUIRED_STATES 一致
# 按周一/三/五→A组、周二/四→B组轮换；断点会记录已完成状态，跑过的自动跳过
DAILY_ROTATION = [["bull", "bear"], ["sideways", "transition"]]
# 月度任务遇非交易日返回此码，调度器记RUN_POSTPONED并在次日重试（不记成功）
POSTPONE_EXIT = 3


def _is_trading_day(today) -> bool:
    if today.weekday() >= 5:
        return False
    return not (ROOT / "data" / f"not_trading_day_{today.isoformat()}.txt").exists()


def _load_core():
    spec = importlib.util.spec_from_file_location("guichan_v6", ROOT / "龟缠量化v6_optimized.py")
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def main(argv: list[str]) -> int:
    today = dt.date.today()
    monthly = "--monthly" in argv
    if not _is_trading_day(today):
        # 每日候选顺延无意义（轮换本就跨天累积），照旧静默跳过；
        # 月度每月只跑一次，静默跳过会丢掉整月重优化，故返回顺延码让调度器次日重试。
        if monthly:
            print("[candidate] 月度优化：今日非交易日，顺延至下一交易日")
            return POSTPONE_EXIT
        print("[candidate] 非交易日，跳过")
        return 0

    candidate_id = f"candidate-{today:%Y%m%d}" + ("-monthly" if monthly else "")
    raw_path = CANDIDATES / "raw" / f"{candidate_id}.json"
    eval_path = CANDIDATES / "evaluations" / f"{candidate_id}.json"
    n_trials, n_stocks = (50, 50) if monthly else (10, 20)

    if monthly:
        # 断点按月命名：单日超时被杀后，顺延日重跑能从已完成状态续起；跨月自动换新文件
        partial_path = CANDIDATES / "partial" / f"monthly_{today:%Y%m}.json"
        stale = CANDIDATES / "partial"
        if stale.exists():
            for old in stale.glob("monthly_*.json"):
                if old.name != partial_path.name:
                    old.unlink(missing_ok=True)
        states, keep_partial = None, False
        print(f"[candidate] {candidate_id}: {n_stocks}只股票 × {n_trials}轮，全状态样本外评估")
    else:
        # 每日断点用固定文件名以支持跨天累积（路径必须在main内取，尊重测试对CANDIDATES的替换）
        partial_path = CANDIDATES / "partial" / "daily_states.json"
        states = DAILY_ROTATION[today.weekday() % 2]
        keep_partial = True
        print(f"[candidate] {candidate_id}: {n_stocks}只×{n_trials}轮，今日轮换状态{states}（跨天累积，单日约减半耗时）")

    try:
        output = _load_core().run_optimization(
            n_trials=n_trials, n_stocks=n_stocks,
            output_path=str(raw_path), partial_path=str(partial_path),
            states=states, keep_partial=keep_partial,
        )
    except Exception as exc:
        evaluation = {"candidate_id": candidate_id, "decision": "rejected", "reason": f"优化异常：{type(exc).__name__}: {exc}"}
        write_json(eval_path, evaluation)
        print(evaluation["reason"])
        return 1

    if not output:
        evaluation = {"candidate_id": candidate_id, "decision": "rejected", "reason": "优化未返回候选"}
        write_json(eval_path, evaluation)
        print(evaluation["reason"])
        return 1

    if not monthly:
        done = {r.get("state") for r in output.get("results", [])}
        missing = [s for s in REQUIRED_STATES if s not in done]
        if missing:
            # 尚未凑满必需状态：保留断点，明日轮换继续累积，不做评估
            evaluation = {
                "candidate_id": candidate_id,
                "decision": "pending",
                "reason": (f"跨天累积中：已完成 {'、'.join(sorted(done & set(ALL_STATES)))}，"
                           f"待补齐 {'、'.join(missing)} 后评估"),
                "completed_states": sorted(done & set(ALL_STATES)),
            }
            evaluation.update({"created_at": dt.datetime.now().isoformat(timespec="seconds"), "raw_path": str(raw_path)})
            write_json(eval_path, evaluation)
            print(f"[candidate] {evaluation['reason']}；断点保留：{partial_path}")
            return 0
        if partial_path.exists():
            partial_path.unlink()
            print(f"[candidate] 必需状态已凑齐，清理断点：{partial_path}")

    evaluation = evaluate_backtest(output)
    evaluation.update({"candidate_id": candidate_id, "created_at": dt.datetime.now().isoformat(timespec="seconds"), "raw_path": str(raw_path)})
    write_json(eval_path, evaluation)

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
