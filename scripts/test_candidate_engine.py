#!/usr/bin/env python3
"""候选晋级规则烟雾测试：只使用内存数据，不访问网络或主策略文件。"""
from candidate_engine import evaluate_backtest


def row(state, trades=20, sharpe=1.2, baseline=1.0, ret=5.0, base_ret=4.0, dd=-5.0, base_dd=-6.0):
    return {
        "state": state, "status": "adopted", "val_trades": trades,
        "val_sharpe": sharpe, "baseline_val_sharpe": baseline,
        "val_return": ret, "baseline_val_return": base_ret,
        "val_drawdown": dd, "baseline_val_drawdown": base_dd,
        "val_win_rate": 55, "baseline_val_win_rate": 50,
        "fold_metrics": [
            {"excess_sharpe_positive": True, "eligible": True, "validation_state_days": 20},
            {"excess_sharpe_positive": True, "eligible": True, "validation_state_days": 20},
            {"excess_sharpe_positive": True, "eligible": True, "validation_state_days": 20},
        ],
    }


good = {
    "validation_protocol": "wf-v3",
    "point_in_time_universe": {"complete": True},
    "excellent_metrics": {
        "annual_return_pct": 26, "max_drawdown_pct": 19.99,
        "calmar": 1.51, "sortino": 1.51, "sharpe": 1.51,
        "closed_trades": 501,
    },
    "results": [row("bull"), row("bear"), row("sideways")],
}
assert evaluate_backtest(good)["decision"] == "shadow_ready"

missing_excellent_metrics = {key: value for key, value in good.items() if key != "excellent_metrics"}
assert evaluate_backtest(missing_excellent_metrics)["decision"] == "rejected"

few_trades = {**good, "results": [row("bull", 13), row("bear", 13), row("sideways", 13)]}
assert evaluate_backtest(few_trades)["decision"] == "rejected"

near_drawdown_limit = {
    **good,
    "results": [row("bull", dd=-19.99, base_dd=-19.99), row("bear"), row("sideways")],
}
assert evaluate_backtest(near_drawdown_limit)["decision"] == "shadow_ready"

bad_drawdown = {
    **good,
    "results": [row("bull", dd=-20, base_dd=-20), row("bear"), row("sideways")],
}
assert evaluate_backtest(bad_drawdown)["decision"] == "rejected"

bad_sharpe = {**good, "results": [row("bull", sharpe=1.05), row("bear"), row("sideways")]}
assert evaluate_backtest(bad_sharpe)["decision"] == "rejected"

missing_universe = {**good, "point_in_time_universe": {"complete": False}}
assert evaluate_backtest(missing_universe)["decision"] == "rejected"

old_protocol = {**good, "validation_protocol": "wf-v2"}
assert evaluate_backtest(old_protocol)["decision"] == "rejected"

sparse_bear = row("bear")
sparse_bear["fold_metrics"] = sparse_bear["fold_metrics"][:2] + [
    {"eligible": False, "validation_state_days": 0, "excess_sharpe_positive": False},
]
sparse = {**good, "results": [row("bull"), sparse_bear, row("sideways")]}
assert evaluate_backtest(sparse)["decision"] == "rejected"
assert "有效滚动验证少于3折" in evaluate_backtest(sparse)["reason"]

missing_state = {**good, "results": [row("bull"), row("bear")],
                 "skipped_states": [{"state": "sideways", "reason": "前置状态未通过，提前停止"}]}
assert "sideways（前置状态未通过，提前停止）" in evaluate_backtest(missing_state)["reason"]

print("candidate_engine_smoke_ok")
