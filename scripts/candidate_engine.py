#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""候选策略的只读评估规则。绝不修改主参数或主模拟盘。"""
from __future__ import annotations

import json
from pathlib import Path

REQUIRED_STATES = ("bull", "bear", "sideways")
MIN_OOS_TRADES = 40
MIN_SHADOW_CLOSED_TRADES = 20
MAX_DRAWDOWN_PCT = 10.0
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
    if missing:
        failures.append("市场状态覆盖不足：" + "、".join(missing))

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
        if not _sharpe_pass(sharpe, baseline_sharpe):
            row_failures.append("夏普未提升10%")
        if ret < baseline_ret:
            row_failures.append("净收益低于基准")
        if dd > MAX_DRAWDOWN_PCT:
            row_failures.append(f"回撤{dd:.2f}%超过{MAX_DRAWDOWN_PCT:.0f}%")
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
        "thresholds": {
            "min_oos_closed_trades": MIN_OOS_TRADES,
            "min_shadow_closed_trades": MIN_SHADOW_CLOSED_TRADES,
            "min_sharpe_improvement": MIN_SHARPE_IMPROVEMENT,
            "max_drawdown_pct": MAX_DRAWDOWN_PCT,
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
