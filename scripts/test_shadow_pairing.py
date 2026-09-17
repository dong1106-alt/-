#!/usr/bin/env python3
import datetime as dt

from performance_metrics import calculate
from shadow_evaluate import compare_pair, equity_sharpe


def portfolio(values, closed=20, drawdown=-5):
    start = dt.date(2023, 1, 2)
    return {
        "total_value": values[-1], "max_drawdown": drawdown,
        "equity_history": [{"date": str(start + dt.timedelta(days=i)), "total_value": value}
                           for i, value in enumerate(values)],
        "trade_history": [{"action": "SELL"} for _ in range(closed)],
    }


def compounded(returns):
    values = [100.0]
    for daily_return in returns:
        values.append(values[-1] * (1 + daily_return))
    return values


candidate = portfolio(compounded([0.0015, 0.0025] * 260), closed=501, drawdown=-4)
baseline = portfolio(compounded([0.003, -0.002] * 260), closed=501, drawdown=-5)
expected = calculate(candidate["equity_history"], 501)["annual_return_pct"]
result = compare_pair(candidate, baseline, 0, 0, 100, 100, expected)
assert result["passed"], result
assert equity_sharpe(candidate) > equity_sharpe(baseline)
bad = compare_pair(portfolio([100 - i for i in range(25)], drawdown=-12), baseline, 0, 0, 100, 100)
assert not bad["passed"]
misaligned = portfolio(compounded([0.0015, 0.0025] * 260), closed=501, drawdown=-4)
misaligned["equity_history"][0]["date"] = "2025-01-01"
assert not compare_pair(misaligned, baseline, 0, 0, 100, 100, expected)["passed"]

rolling = portfolio(compounded([0.0015, 0.0025] * 260), closed=501, drawdown=-4)
rolling["equity_history"] = rolling["equity_history"][-500:]
assert compare_pair(
    rolling, baseline, 0, 0, 100, 100, expected,
    candidate_start_equity_date="2023-01-02",
)["candidate_closed_trades"] == 501
print("shadow_pairing_ok")
