#!/usr/bin/env python3
from shadow_evaluate import compare_pair, equity_sharpe


def portfolio(values, closed=20, drawdown=-5):
    return {
        "total_value": values[-1], "max_drawdown": drawdown,
        "equity_history": [{"date": f"2026-08-{i + 1:02d}", "total_value": value}
                           for i, value in enumerate(values)],
        "trade_history": [{"action": "SELL"} for _ in range(closed)],
    }


def compounded(returns):
    values = [100.0]
    for daily_return in returns:
        values.append(values[-1] * (1 + daily_return))
    return values


candidate = portfolio(compounded([0.009, 0.011] * 12), drawdown=-4)
baseline = portfolio(compounded([0.02, -0.015] * 12), drawdown=-5)
result = compare_pair(candidate, baseline, 0, 0, 100, 100)
assert result["passed"]
assert equity_sharpe(candidate) > equity_sharpe(baseline)
bad = compare_pair(portfolio([100 - i for i in range(25)], drawdown=-12), baseline, 0, 0, 100, 100)
assert not bad["passed"]
misaligned = portfolio(compounded([0.009, 0.011] * 12), drawdown=-4)
misaligned["equity_history"][0]["date"] = "2025-01-01"
assert not compare_pair(misaligned, baseline, 0, 0, 100, 100)["passed"]
print("shadow_pairing_ok")
