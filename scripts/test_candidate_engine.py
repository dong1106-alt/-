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
            {"excess_sharpe_positive": True},
            {"excess_sharpe_positive": True},
            {"excess_sharpe_positive": True},
        ],
    }


good = {
    "validation_protocol": "wf-v2",
    "point_in_time_universe": {"complete": True},
    "results": [row("bull"), row("bear"), row("sideways")],
}
assert evaluate_backtest(good)["decision"] == "shadow_ready"

few_trades = {**good, "results": [row("bull", 13), row("bear", 13), row("sideways", 13)]}
assert evaluate_backtest(few_trades)["decision"] == "rejected"

bad_drawdown = {**good, "results": [row("bull", dd=-11), row("bear"), row("sideways")]}
assert evaluate_backtest(bad_drawdown)["decision"] == "rejected"

bad_sharpe = {**good, "results": [row("bull", sharpe=1.05), row("bear"), row("sideways")]}
assert evaluate_backtest(bad_sharpe)["decision"] == "rejected"

missing_universe = {**good, "point_in_time_universe": {"complete": False}}
assert evaluate_backtest(missing_universe)["decision"] == "rejected"

print("candidate_engine_smoke_ok")
