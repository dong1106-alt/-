#!/usr/bin/env python3
"""Shared net-performance metrics and the excellent-strategy gate."""
from __future__ import annotations

import math
import statistics


EXCELLENT_THRESHOLDS = {
    "annual_return_pct": 25.0,
    "max_drawdown_pct": 10.0,
    "calmar": 2.0,
    "sortino": 2.5,
    "sharpe": 2.0,
    "closed_trades": 500,
    "backtest_deviation_pct": 10.0,
}


def calculate(equity_history: list[dict], closed_trades: int, periods_per_year: int = 250) -> dict:
    """Calculate annualized metrics from a fee-adjusted end-of-day equity series."""
    points = {}
    for row in equity_history:
        value = row.get("equity", row.get("total_value"))
        if row.get("date") is not None and value is not None and float(value) > 0:
            points[str(row["date"])[:10]] = float(value)
    values = [points[day] for day in sorted(points)]
    if len(values) < 2:
        return {
            "annual_return_pct": 0.0, "max_drawdown_pct": 0.0,
            "calmar": 0.0, "sortino": 0.0, "sharpe": 0.0,
            "closed_trades": int(closed_trades), "trading_days": len(values),
        }

    returns = [values[i] / values[i - 1] - 1.0 for i in range(1, len(values))]
    annual = (values[-1] / values[0]) ** (periods_per_year / len(returns)) - 1.0
    peak = values[0]
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, 1.0 - value / peak)

    mean = statistics.mean(returns)
    deviation = statistics.stdev(returns) if len(returns) > 1 else 0.0
    downside = math.sqrt(sum(min(ret, 0.0) ** 2 for ret in returns) / len(returns))
    sharpe = mean / deviation * math.sqrt(periods_per_year) if deviation > 0 else (999.0 if mean > 0 else 0.0)
    sortino = mean / downside * math.sqrt(periods_per_year) if downside > 0 else (999.0 if mean > 0 else 0.0)
    calmar = annual / max_drawdown if max_drawdown > 0 else (999.0 if annual > 0 else 0.0)
    return {
        "annual_return_pct": round(annual * 100, 3),
        "max_drawdown_pct": round(max_drawdown * 100, 3),
        "calmar": round(calmar, 3),
        "sortino": round(sortino, 3),
        "sharpe": round(sharpe, 3),
        "closed_trades": int(closed_trades),
        "trading_days": len(values),
    }


def backtest_deviation_pct(live_annual_return_pct: float, backtest_annual_return_pct: float) -> float:
    baseline = abs(float(backtest_annual_return_pct))
    if baseline == 0:
        return 999.0 if float(live_annual_return_pct) != 0 else 0.0
    return round(abs(float(live_annual_return_pct) - float(backtest_annual_return_pct)) / baseline * 100, 3)


def excellent_failures(metrics: dict, *, require_deviation: bool) -> list[str]:
    """Return every unmet strict threshold; an absent metric always fails closed."""
    checks = (
        ("annual_return_pct", ">", "年化收益"),
        ("max_drawdown_pct", "<", "最大回撤"),
        ("calmar", ">", "卡玛比率"),
        ("sortino", ">", "索提诺比率"),
        ("sharpe", ">", "夏普比率"),
        ("closed_trades", ">", "平仓交易数"),
    )
    failures = []
    for key, operator, label in checks:
        if key not in metrics:
            failures.append(f"缺少{label}")
            continue
        value = float(metrics[key])
        threshold = float(EXCELLENT_THRESHOLDS[key])
        passed = value > threshold if operator == ">" else value < threshold
        if not passed:
            failures.append(f"{label}{value:g}未满足{operator}{threshold:g}")
    if require_deviation:
        key = "backtest_deviation_pct"
        if key not in metrics:
            failures.append("缺少回测偏差")
        elif float(metrics[key]) >= EXCELLENT_THRESHOLDS[key]:
            failures.append(
                f"回测偏差{float(metrics[key]):g}%未满足<{EXCELLENT_THRESHOLDS[key]:g}%"
            )
    return failures
