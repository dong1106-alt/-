#!/usr/bin/env python3
"""Deterministic causal-v2 regression plus local-cache timing checks."""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
BASELINE = json.loads((ROOT / "config" / "regression_baseline_v2.json").read_text(encoding="utf-8"))
spec = importlib.util.spec_from_file_location("guichan_regression", ROOT / "龟缠量化v6_optimized.py")
strategy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(strategy)


def fixture() -> pd.DataFrame:
    n = 720
    x = np.arange(n, dtype=float)
    close = 20 + x * 0.025 + np.sin(x / 9) * 2.2
    return pd.DataFrame({
        "date": pd.bdate_range("2021-01-01", periods=n),
        "open": close * (1 + np.sin(x / 7) * 0.002),
        "high": close * 1.02,
        "low": close * 0.98,
        "close": close,
        "volume": 1_000_000 * (1.2 + np.sin(x / 5) * 0.5 + x / n),
    })


def assert_timing(result: dict) -> None:
    for fill in result.get("fill_records", []):
        assert fill["fill_date"] > fill["signal_date"], fill
        if fill["fill_source"] == "next_open":
            slip = abs(float(fill["fill_price"]) / float(fill["bar_open"]) - 1)
            assert slip <= 0.003, fill


frame = fixture()
params = dict(strategy.STATE_BASELINES["bull"])
result = strategy.fast_backtest(
    frame, params, start_date=frame.loc[260, "date"], end_date=frame.loc[680, "date"]
)
assert_timing(result)
expected = BASELINE["metrics"]
tolerance = BASELINE["tolerance"]
for key in ("total_return", "max_drawdown", "sharpe"):
    assert abs(float(result[key]) - float(expected[key])) <= float(tolerance[key]), (key, result[key])
assert result["trades"] == expected["trades"]

checked = 0
for code in strategy.get_all_local_codes()[:20]:
    local = strategy.load_stock_data(code)
    if local is None or len(local) < 200:
        continue
    start = local.iloc[-180]["date"]
    real_result = strategy.fast_backtest(local, params, start_date=start, end_date=local.iloc[-1]["date"])
    assert_timing(real_result)
    checked += 1
    if checked >= 3:
        break
if not os.environ.get("CI"):
    assert checked >= 1, "no usable local cache for regression"

print(f"regression_v2_ok real_cache={checked}")
