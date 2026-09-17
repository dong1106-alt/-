#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成全状态参数候选并做样本外预筛；永不覆盖 optimal_params.json。

每日模式（2026-09-09修复超时）：单日跑全部4状态曾超过30分钟被超时击杀。
现在每天只轮换优化2个状态，通过稳定断点文件跨天累积；凑满必需状态
（bull/bear/sideways，transition可选）后才做样本外评估。月度模式不变。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from candidate_engine import MIN_WALK_FORWARD_FOLDS, evaluate_backtest, write_json
from causal_backtest import STRICT_EVALUATION_START, build_walk_forward_folds

DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
CANDIDATES = Path(os.environ.get("SUPER_AGENT_CANDIDATES_DIR", str(DATA_ROOT / "candidates"))).resolve()
LEDGER_PATH = Path(os.environ.get(
    "SUPER_AGENT_VALIDATION_LEDGER", str(ROOT / "data" / "candidates" / "validation_ledger.json")
)).resolve()

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


def _hash_payload(payload) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validation_periods(output: dict) -> list[tuple[str, str]]:
    return sorted({(f.get("val_start"), f.get("val_end"))
                   for r in output.get("results", []) for f in r.get("fold_metrics", [])})


def _consumed_periods(exclude_reservation: str | None = None) -> list[tuple[str, str]] | None:
    if not LEDGER_PATH.exists():
        return []
    try:
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    periods = []
    for key, entry in ledger.get("entries", {}).items():
        historical = entry.get("validation_periods")
        if not historical:
            # Pre-v3 ledgers recorded only the hash. Recover dates from their raw candidate.
            for path in (CANDIDATES / "raw").glob("*.json"):
                try:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    historical = _validation_periods(raw)
                except (OSError, ValueError):
                    continue
                if _hash_payload({"protocol": "wf-v2", "periods": historical}) == key:
                    break
            else:
                return None
        periods.extend(tuple(period) for period in historical)
    for reservation_id, reservation in ledger.get("reservations", {}).items():
        if reservation_id != exclude_reservation:
            periods.extend(tuple(period) for period in reservation.get("validation_periods", []))
    return periods


def reserve_validation_periods(reservation_id: str, periods: list[tuple[str, str]],
                               profile_sha256: str) -> dict:
    """Consume planned OOS dates before strategy results are calculated."""
    if not reservation_id or not periods:
        raise ValueError("reservation id and validation periods are required")
    try:
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        ledger = {"entries": {}, "reservations": {}}
    ledger.setdefault("entries", {})
    reservations = ledger.setdefault("reservations", {})
    existing = reservations.get(reservation_id)
    normalized = [list(period) for period in periods]
    if existing:
        if (existing.get("validation_periods") != normalized
                or existing.get("profile_sha256") != profile_sha256):
            raise RuntimeError("validation reservation changed after creation")
        return existing
    consumed = _consumed_periods()
    if consumed is None or _overlaps_consumed(periods, consumed):
        raise RuntimeError("validation periods overlap consumed or reserved dates")
    reservation = {
        "validation_periods": normalized,
        "profile_sha256": profile_sha256,
        "status": "reserved",
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    reservations[reservation_id] = reservation
    write_json(LEDGER_PATH, ledger)
    return reservation


def _overlaps_consumed(periods: list[tuple[str, str]], consumed: list[tuple[str, str]]) -> bool:
    return any(start <= old_end and old_start <= end
               for start, end in periods for old_start, old_end in consumed)


def _candidate_identity(output: dict) -> tuple[str, str]:
    code_hash = hashlib.sha256()
    for path in (
        ROOT / "龟缠量化v6_optimized.py",
        *(ROOT / "scripts" / name for name in (
            "causal_backtest.py", "candidate_engine.py", "candidate_optimize.py",
            "backfill_history.py", "point_in_time_universe.py",
            "daily_signal_scan.py", "sim_trade_tracker.py", "trading_rules.py",
            "shadow_pipeline.py", "shadow_evaluate.py",
        )),
        ROOT / "data" / "causal_quality.py", ROOT / "config" / "settings.yaml",
    ):
        code_hash.update(path.read_bytes())
    params = {r.get("state"): r.get("params") for r in output.get("results", [])}
    signature = _hash_payload({
        "code": code_hash.hexdigest(), "params": params,
        "data": output.get("data_snapshot_hash"),
        "universe": (output.get("point_in_time_universe") or {}).get("sha256"),
        "history": (output.get("point_in_time_universe") or {}).get("stock_history_manifest_sha256"),
        "validation_profile": output.get("validation_profile"),
    })
    periods = _validation_periods(output)
    # Keep the v2 ledger namespace: changing scoring rules must not unlock old periods.
    validation_key = _hash_payload({"protocol": "wf-v2", "periods": periods})
    return signature, validation_key


def _ledger_decision(output: dict, reservation_id: str | None = None) -> tuple[dict, bool]:
    signature, validation_key = _candidate_identity(output)
    try:
        ledger = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        ledger = {"entries": {}}
    previous = ledger["entries"].get(validation_key)
    if previous:
        if previous.get("candidate_signature") == signature:
            return dict(previous["evaluation"]), True
        return {
            "decision": "rejected",
            "reason": "该滚动验证区间已被其他候选消费，禁止重复试探",
            "candidate_signature": signature,
            "validation_key": validation_key,
        }, True
    reservation = ledger.get("reservations", {}).get(reservation_id) if reservation_id else None
    periods = _validation_periods(output)
    if reservation and reservation.get("validation_periods") != [list(period) for period in periods]:
        return {
            "decision": "rejected",
            "reason": "实际滚动验证区间与运行前预约不一致",
            "candidate_signature": signature,
            "validation_key": validation_key,
        }, True
    consumed = _consumed_periods(exclude_reservation=reservation_id)
    if consumed is None or _overlaps_consumed(periods, consumed):
        return {
            "decision": "rejected",
            "reason": ("验证账本缺少历史区间，禁止继续" if consumed is None
                       else "滚动验证区间与已消费区间重叠，禁止重复试探"),
            "candidate_signature": signature,
            "validation_key": validation_key,
        }, True
    evaluation = evaluate_backtest(output)
    evaluation.update({"candidate_signature": signature, "validation_key": validation_key})
    ledger["entries"][validation_key] = {
        "candidate_signature": signature,
        "evaluation": evaluation,
        "validation_periods": periods,
        "created_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    if reservation:
        reservation.update({"status": "consumed", "validation_key": validation_key})
    write_json(LEDGER_PATH, ledger)
    return evaluation, False


def main(argv: list[str]) -> int:
    today = dt.date.today()
    historical = "--historical" in argv
    monthly = "--monthly" in argv or historical
    if not historical and not _is_trading_day(today):
        # 每日候选顺延无意义（轮换本就跨天累积），照旧静默跳过；
        # 月度每月只跑一次，静默跳过会丢掉整月重优化，故返回顺延码让调度器次日重试。
        if monthly:
            print("[candidate] 月度优化：今日非交易日，顺延至下一交易日")
            return POSTPONE_EXIT
        print("[candidate] 非交易日，跳过")
        return 0

    candidate_id = os.environ.get(
        "SUPER_AGENT_CANDIDATE_ID", f"candidate-{today:%Y%m%d}" + ("-monthly" if monthly else "")
    )
    raw_path = CANDIDATES / "raw" / f"{candidate_id}.json"
    eval_path = CANDIDATES / "evaluations" / f"{candidate_id}.json"
    n_trials, n_stocks = (50, 50) if monthly else (10, 20)
    validation_after = None

    if monthly and not historical:
        consumed = _consumed_periods()
        reason = None
        if consumed is None:
            reason = "验证账本缺少历史区间，禁止继续"
        elif consumed:
            validation_after = max(end for _, end in consumed)
            core = _load_core()
            timeline = core.load_market_states()
            calendar = [r["date"] for r in timeline if r["date"] >= STRICT_EVALUATION_START]
            try:
                folds, _ = build_walk_forward_folds(calendar)
            except ValueError:
                folds = []
            fresh = [fold for fold in folds
                     if str(fold.validation_start)[:10] > validation_after]
            if len(fresh) < MIN_WALK_FORWARD_FOLDS:
                reason = f"新验证折不足{MIN_WALK_FORWARD_FOLDS}折，等待独立数据"
            else:
                for state in REQUIRED_STATES:
                    count = sum(any(r["state"] == state and
                                    str(fold.validation_start)[:10] <= r["date"] <= str(fold.validation_end)[:10]
                                    for r in timeline) for fold in fresh)
                    if count < MIN_WALK_FORWARD_FOLDS:
                        reason = f"{state}有效新折不足{MIN_WALK_FORWARD_FOLDS}折，等待独立数据"
                        break
        if reason:
            if eval_path.exists():
                print(f"[candidate] 已有本月评估记录，保留原结果；{reason}")
                return 0
            evaluation = {"candidate_id": candidate_id, "decision": "waiting_for_new_data",
                          "reason": reason}
            write_json(eval_path, evaluation)
            print(f"[candidate] {reason}")
            return 0

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
        # 每日只做研究，不跨日拼接不同数据快照，也不允许自动进入影子盘。
        partial_path = CANDIDATES / "partial" / f"daily_{today:%Y%m%d}.json"
        states = DAILY_ROTATION[today.weekday() % 2]
        keep_partial = False
        print(f"[candidate] {candidate_id}: {n_stocks}只×{n_trials}轮，今日轮换状态{states}（研究模式）")

    try:
        output = _load_core().run_optimization(
            n_trials=n_trials, n_stocks=n_stocks,
            output_path=str(raw_path), partial_path=str(partial_path),
            states=states, keep_partial=keep_partial,
            validation_after=validation_after,
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

    profile_sha256 = os.environ.get("SUPER_AGENT_VALIDATION_PROFILE_SHA256")
    if profile_sha256:
        output["validation_profile"] = {
            "id": os.environ.get("SUPER_AGENT_VALIDATION_PROFILE_ID", ""),
            "sha256": profile_sha256,
            "evaluation_start": STRICT_EVALUATION_START,
        }
        write_json(raw_path, output)

    if not monthly:
        evaluation = {
            "candidate_id": candidate_id, "decision": "research_only",
            "reason": "每日优化仅供研究；只有月度全状态候选可进入验证账本",
            "completed_states": sorted(r.get("state") for r in output.get("results", [])),
        }
        evaluation.update({"created_at": dt.datetime.now().isoformat(timespec="seconds"), "raw_path": str(raw_path)})
        write_json(eval_path, evaluation)
        print(f"[candidate] {evaluation['reason']}")
        return 0

    evaluation, reused = _ledger_decision(
        output, reservation_id=os.environ.get("SUPER_AGENT_VALIDATION_RESERVATION")
    )
    evaluation.update({"candidate_id": candidate_id, "created_at": dt.datetime.now().isoformat(timespec="seconds"), "raw_path": str(raw_path)})
    write_json(eval_path, evaluation)

    if evaluation["decision"] == "shadow_ready":
        active = dict(output)
        active["candidate_meta"] = {"candidate_id": candidate_id, "evaluation": evaluation}
        write_json(CANDIDATES / "active_shadow.json", active)
        print(f"[candidate] {'复用既有验证结果；' if reused else ''}通过预筛，下一交易日进入影子验证：{candidate_id}")
    else:
        print(f"[candidate] 已拒绝：{evaluation['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
