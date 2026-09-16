#!/usr/bin/env python3
"""End-to-end causality checks for indicators, signals and fills."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("guichan_causal_test", ROOT / "龟缠量化v6_optimized.py")
strategy = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(strategy)

n = 720
x = np.arange(n, dtype=float)
close = 20 + x * 0.025 + np.sin(x / 9) * 2.2
frame = pd.DataFrame({
    "date": pd.bdate_range("2021-01-01", periods=n),
    "open": close * (1 + np.sin(x / 7) * 0.002),
    "high": close * 1.02,
    "low": close * 0.98,
    "close": close,
    "volume": 1_000_000 * (1.2 + np.sin(x / 5) * 0.5 + x / n),
})
params = dict(strategy.STATE_BASELINES["bull"])
end_date = frame.loc[680, "date"]

indicator_config = dict(strategy.DEFAULT_CONFIG["strategy"])
prefix = frame.iloc[:681].copy()
extended = frame.copy()
prefix_indicators = strategy.calc_indicators_vec(prefix.copy(), indicator_config)
extended_indicators = strategy.calc_indicators_vec(extended.copy(), indicator_config).iloc[:681]
for column in ("atr", "ma20", "ma60", "dc_high", "dc_low", "risk_value"):
    pd.testing.assert_series_equal(
        prefix_indicators[column].reset_index(drop=True),
        extended_indicators[column].reset_index(drop=True), check_names=False,
    )
prefix_signals = strategy.detect_chan_signals_optimized(prefix_indicators.copy())
extended_signals = strategy.detect_chan_signals_optimized(
    strategy.calc_indicators_vec(extended.copy(), indicator_config)
).iloc[:681]
for column in ("chan_buy", "chan_sell", "chan_buy_type"):
    pd.testing.assert_series_equal(
        prefix_signals[column].reset_index(drop=True),
        extended_signals[column].reset_index(drop=True), check_names=False,
    )
extreme_future = pd.concat([prefix, pd.DataFrame([{
    "date": prefix["date"].iloc[-1] + pd.offsets.BDay(), "open": 20,
    "high": 1_000_000, "low": 0.001, "close": 20, "volume": 1_000_000,
}])], ignore_index=True)
extreme_signals = strategy.detect_chan_signals_optimized(
    strategy.calc_indicators_vec(extreme_future, indicator_config)
).iloc[:681]
for column in ("chan_buy", "chan_sell", "chan_buy_type"):
    pd.testing.assert_series_equal(
        prefix_signals[column].reset_index(drop=True),
        extreme_signals[column].reset_index(drop=True), check_names=False,
    )

baseline = strategy.fast_backtest(
    frame.copy(), params, start_date=frame.loc[260, "date"], end_date=end_date
)
changed = frame.copy()
changed.loc[681:, ["open", "high", "low", "close", "volume"]] *= 50
mutated = strategy.fast_backtest(
    changed, params, start_date=frame.loc[260, "date"], end_date=end_date
)

assert baseline["fill_records"], "fixture produced no fills"
assert baseline["fill_records"] == mutated["fill_records"]
assert baseline["trade_records"] == mutated["trade_records"]
for fill in baseline["fill_records"]:
    assert fill["fill_date"] > fill["signal_date"]
    if fill["fill_source"] == "next_open":
        assert abs(fill["fill_price"] - fill["bar_open"]) / fill["bar_open"] < 0.01

# A signal on the final bar must remain unfilled.
short = frame.iloc[:181].copy()
forced_params = dict(params)
result = strategy.fast_backtest(short, forced_params, start_date=short.loc[170, "date"])
assert all(fill["fill_date"] > fill["signal_date"] for fill in result["fill_records"])

# A suspended bar is not an executable session; the pending signal rolls forward.
suspended = frame.copy()
suspended.loc[270, "volume"] = 0
base_suspended = strategy.fast_backtest(
    suspended, params, start_date=frame.loc[260, "date"], end_date=end_date
)
assert all(fill["fill_date"] != frame.loc[270, "date"] for fill in base_suspended["fill_records"])

print("strategy_causality_ok")
