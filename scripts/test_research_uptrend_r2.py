#!/usr/bin/env python3
import numpy as np
import pandas as pd

from research_trend_risk_v5 import _prepare_features
from research_uptrend_r2 import (
    PARAMETERS,
    SOURCE_BLOB_SHA,
    SOURCE_COMMIT,
    SOURCE_SHA256,
    _rank_targets,
)


dates = pd.bdate_range("2020-01-01", periods=210)
x = np.arange(len(dates), dtype=float)
smooth = 10.0 * np.exp(0.006 * x)
noisy = smooth * (1 + 0.005 * np.sin(x))
bars = {}
for code, close in (("smooth", smooth), ("noisy", noisy)):
    frame = pd.DataFrame({
        "open": close, "high": close * 1.005, "low": close * 0.995,
        "close": close, "volume": 1_000_000.0, "trade_status": 1,
    }, index=dates)
    bars[code] = _prepare_features(frame)

targets = _rank_targets(dates[-1], set(bars), bars)
assert targets == ["smooth", "noisy"]
cutoff = dates[-11]
before = _rank_targets(cutoff, set(bars), bars)
future = {code: pd.concat([frame, pd.DataFrame({
    column: [999.0] for column in frame.columns
}, index=[pd.Timestamp("2021-01-01")])]) for code, frame in bars.items()}
assert before == _rank_targets(cutoff, set(future), future)

rejected = bars["smooth"].copy()
rejected.loc[dates[-1], "high_to_close"] = 1.11
assert _rank_targets(dates[-1], {"smooth"}, {"smooth": rejected}) == []
assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert SOURCE_BLOB_SHA == "6d4a021b382e35f0e50452181e8ed677934d472b"
assert SOURCE_SHA256 == "e019d6f363ad7bae907350e9b0bec0952e72b044038ee5472f803f41aa2a8525"
assert PARAMETERS["minimum_r_squared"] == 0.8
assert PARAMETERS["stock_num"] == 2
assert PARAMETERS["fill"] == "next open, T+1"
print("research_uptrend_r2_ok")
