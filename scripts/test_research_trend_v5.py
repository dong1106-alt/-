#!/usr/bin/env python3
import numpy as np
import pandas as pd

from research_trend_v5 import PARAMETERS, SOURCE_COMMIT, _prepare_features, _week_key


dates = pd.bdate_range("2020-01-01", periods=230)
x = np.arange(len(dates), dtype=float)
close = 10.0 * np.exp(0.006 * x)
frame = pd.DataFrame({
    "open": close,
    "high": close * 1.005,
    "low": close * 0.995,
    "close": close,
    "volume": 1_000_000.0,
    "trade_status": 1,
}, index=dates)
features = _prepare_features(frame)
last = features.iloc[-1]
expected_slope, expected_intercept = np.polyfit(np.arange(100), close[-100:], 1)
expected_r = np.corrcoef(np.arange(100), close[-100:])[0, 1]
assert abs(last["slope"] - expected_slope) < 1e-10
assert abs(last["intercept"] - expected_intercept) < 1e-10
assert abs(last["correlation"] - expected_r) < 1e-10
assert bool(last["eligible"])

cutoff = dates[-11]
before = _prepare_features(frame).loc[cutoff].copy()
future = frame.copy()
future.loc[dates[-10]:, ["high", "low", "close", "volume"]] *= 50
pd.testing.assert_series_equal(before, _prepare_features(future).loc[cutoff])

surge = frame.copy()
surge.loc[dates[-2], "high"] = surge.loc[dates[-1], "close"] * 1.11
assert not bool(_prepare_features(surge).iloc[-1]["eligible"])
assert _week_key("2020-01-01") == "2020-01"
assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert PARAMETERS["stock_num"] == 5
assert PARAMETERS["maximum_volume_ratio"] == 1.5
print("research_trend_v5_ok")
