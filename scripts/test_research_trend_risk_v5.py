#!/usr/bin/env python3
import numpy as np
import pandas as pd

from research_trend_risk_v5 import (
    PARAMETERS,
    SOURCE_COMMIT,
    SOURCE_URL,
    _prepare_features,
    _update_bull_state,
)


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
expected_slope, expected_intercept = np.polyfit(np.arange(120), close[-120:], 1)
expected_r = np.corrcoef(np.arange(120), close[-120:])[0, 1]
assert abs(last["slope"] - expected_slope) < 1e-10
assert abs(last["intercept"] - expected_intercept) < 1e-10
assert abs(last["correlation"] - expected_r) < 1e-10
assert bool(last["eligible"])

cutoff = dates[-11]
before = _prepare_features(frame).loc[cutoff].copy()
future = frame.copy()
future.loc[dates[-10]:, ["high", "low", "close", "volume"]] *= 50
pd.testing.assert_series_equal(before, _prepare_features(future).loc[cutoff])

assert _update_bull_state(False, pd.Series([100.0] * 9 + [106.0]))
assert _update_bull_state(True, pd.Series([100.0] * 9 + [99.6]))
assert not _update_bull_state(True, pd.Series([100.0] * 9 + [90.0]))
assert not _update_bull_state(False, pd.Series([100.0] * 9 + [100.4]))

assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert SOURCE_URL.endswith(
    "/40.%E5%86%8D%E6%94%B9%E8%BF%9B%E5%8F%AF%E5%AE%9E%E7%9B%98-"
    "%E9%BB%98%E9%BB%98%E8%B5%9A%E9%92%B1%E7%B3%BB%E5%88%97-%E9%A3%8E%E9%99%A9"
    "%E6%8E%A7%E5%88%B6-%E5%A2%9E%E5%BC%BA%E7%89%88%E6%9C%AC-V5.0.py"
)
assert PARAMETERS == {
    "maximum_close": 500.0,
    "high_window": 30,
    "maximum_high_to_close": 1.1,
    "volume_short_window": 7,
    "volume_long_window": 180,
    "maximum_volume_ratio": 1.5,
    "regression_window": 120,
    "minimum_slope_intercept": 0.005,
    "minimum_correlation": 0.9,
    "stock_num": 2,
    "index": "sh000001",
    "index_ma_window": 10,
    "regime_threshold": 0.005,
    "initial_is_bull": False,
    "rebalance": "first trading day of week open",
}
print("research_trend_risk_v5_ok")
