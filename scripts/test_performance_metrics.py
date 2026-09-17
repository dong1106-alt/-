#!/usr/bin/env python3
from performance_metrics import (
    backtest_deviation_pct, calculate, excellent_failures,
)


curve = [{"date": f"2026-01-{i + 1:02d}", "equity": 100_000 * (1.002 ** i)} for i in range(20)]
metrics = calculate(curve, 501)
assert metrics["annual_return_pct"] > 25
assert metrics["max_drawdown_pct"] == 0
assert metrics["closed_trades"] == 501
assert backtest_deviation_pct(27, 25) == 8

passing = {
    "annual_return_pct": 25.01, "max_drawdown_pct": 19.99,
    "calmar": 1.51, "sortino": 1.51, "sharpe": 1.51,
    "closed_trades": 501, "backtest_deviation_pct": 9.99,
}
assert excellent_failures(passing, require_deviation=True) == []
for ratio in ("calmar", "sortino", "sharpe"):
    assert len(excellent_failures({**passing, ratio: 1.5}, require_deviation=True)) == 1
assert len(excellent_failures({**passing, "max_drawdown_pct": 20}, require_deviation=True)) == 1
assert len(excellent_failures({**passing, "closed_trades": 500}, require_deviation=True)) == 1
assert len(excellent_failures({**passing, "backtest_deviation_pct": 10}, require_deviation=True)) == 1
print("performance_metrics_ok")
