#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选策略的只读评估规则。绝不修改主参数或主模拟盘。"""
from __future__ import annotations

import json
from pathlib import Path

from performance_metrics import EXCELLENT_THRESHOLDS, excellent_failures

REQUIRED_STATES = ("bull", "bear", "sideways")
MIN_OOS_TRADES = 40
MIN_STATE_OOS_TRADES = 10
MIN_WALK_FORWARD_FOLDS = 3
MIN_SHADOW_CLOSED_TRADES = 20
MAX_DRAWDOWN_PCT = 20.0
MIN_SHARPE_IMPROVEMENT = 0.10


def _sharpe_pass(candidate: float, baseline: float) -> bool:
    """正夏普需提升10%；非正基准需至少改善0.1，避免除零/负数失真。"""
    if baseline > 0:
        return candidate >= baseline * (1 + MIN_SHARPE_IMPROVEMENT)
    return candidate >= baseline + 0.1


def evaluate_backtest(output: dict) -> dict:
    """对全市场状态的样本外结果进行晋级预筛，不产生任何策略切换。"""
    by_state = {r.get("state"): r for r in output.get("results", [])}
    missing = [s for s in REQUIRED_STATES if s not in by_state]
    failures: list[str] = []
    excellent_metrics = output.get("excellent_metrics") or {}
    excellent_gate_failures = excellent_failures(excellent_metrics, require_deviation=False)
    if excellent_gate_failures:
        failures.append("优秀门槛：" + "、".join(excellent_gate_failures))
    if output.get("validation_protocol") != "wf-v3":
        failures.append("验证协议不是wf-v3")
    universe = output.get("point_in_time_universe") or {}
    if not universe.get("complete"):
        failures.append("历史时点股票池不完整")
    if missing:
        skipped = {r.get("state"): r.get("reason") for r in output.get("skipped_states", [])}
        failures.append("市场状态未评估：" + "、".join(
            f"{state}（{skipped[state]}）" if skipped.get(state) else state for state in missing))

    rows = [by_state[s] for s in REQUIRED_STATES if s in by_state]
    total_trades = sum(int(r.get("val_trades", 0) or 0) for r in rows)
    if total_trades < MIN_OOS_TRADES:
        failures.append(f"样本外交易{total_trades}笔，少于{MIN_OOS_TRADES}笔")

    state_metrics = []
    for state, r in ((s, by_state[s]) for s in REQUIRED_STATES if s in by_state):
        sharpe = float(r.get("val_sharpe", 0) or 0)
        baseline_sharpe = float(r.get("baseline_val_sharpe", 0) or 0)
        ret = float(r.get("val_return", 0) or 0)
        baseline_ret = float(r.get("baseline_val_return", 0) or 0)
        dd = abs(float(r.get("val_drawdown", 0) or 0))
        baseline_dd = abs(float(r.get("baseline_val_drawdown", 0) or 0))
        row_failures = []
        if r.get("status") != "adopted":
            row_failures.append("walk-forward未通过")
        fold_metrics = r.get("fold_metrics") or []
        if any(f.get("eligible") is not (int(f.get("validation_state_days", 0)) > 0)
               for f in fold_metrics):
            row_failures.append("滚动验证折状态标记不完整")
        eligible_folds = [f for f in fold_metrics if f.get("eligible") is True
                          and int(f.get("validation_state_days", 0)) > 0]
        if len(eligible_folds) < MIN_WALK_FORWARD_FOLDS:
            row_failures.append(f"有效滚动验证少于{MIN_WALK_FORWARD_FOLDS}折")
        required_positive = (len(eligible_folds) * 2 + 2) // 3 if eligible_folds else MIN_WALK_FORWARD_FOLDS
        positive = sum(bool(f.get("excess_sharpe_positive")) for f in eligible_folds)
        if positive < required_positive:
            row_failures.append("超额夏普为正的折数不足三分之二")
        if int(r.get("val_trades", 0) or 0) < MIN_STATE_OOS_TRADES:
            row_failures.append(f"样本外交易少于{MIN_STATE_OOS_TRADES}笔")
        if not _sharpe_pass(sharpe, baseline_sharpe):
            row_failures.append("夏普未提升10%")
        if ret < baseline_ret:
            row_failures.append("净收益低于基准")
        if dd >= MAX_DRAWDOWN_PCT:
            row_failures.append(f"回撤{dd:.2f}%未低于{MAX_DRAWDOWN_PCT:.0f}%")
        if dd > baseline_dd:
            row_failures.append("回撤劣于基准")
        if row_failures:
            failures.append(f"{state}：{'、'.join(row_failures)}")
        state_metrics.append({
            "state": state, "sharpe": sharpe, "baseline_sharpe": baseline_sharpe,
            "return": ret, "baseline_return": baseline_ret,
            "drawdown": dd, "baseline_drawdown": baseline_dd,
            "trades": int(r.get("val_trades", 0) or 0),
            "win_rate": float(r.get("val_win_rate", 0) or 0),
            "baseline_win_rate": float(r.get("baseline_val_win_rate", 0) or 0),
        })

    return {
        "decision": "shadow_ready" if not failures else "rejected",
        "reason": "通过样本外预筛，进入影子验证" if not failures else "；".join(failures),
        "required_states": list(REQUIRED_STATES),
        "oos_closed_trades": total_trades,
        "state_metrics": state_metrics,
        "excellent_metrics": excellent_metrics,
        "thresholds": {
            "min_oos_closed_trades": MIN_OOS_TRADES,
            "min_state_oos_closed_trades": MIN_STATE_OOS_TRADES,
            "min_walk_forward_folds": MIN_WALK_FORWARD_FOLDS,
            "min_shadow_closed_trades": MIN_SHADOW_CLOSED_TRADES,
            "min_sharpe_improvement": MIN_SHARPE_IMPROVEMENT,
            "max_drawdown_pct": MAX_DRAWDOWN_PCT,
            "excellent": EXCELLENT_THRESHOLDS,
        },
    }


def closed_shadow_trades(portfolio: dict, baseline_trade_count: int) -> int:
    return sum(
        1 for trade in portfolio.get("trade_history", [])[baseline_trade_count:]
        if trade.get("action") == "SELL"
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
