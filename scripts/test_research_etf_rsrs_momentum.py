#!/usr/bin/env python3
import numpy as np
import pandas as pd

from research_etf_rsrs_momentum import (
    PARAMETERS,
    SOURCE_BLOB_SHA,
    SOURCE_COMMIT,
    SOURCE_SHA256,
    mark_to_market,
    momentum_score,
    next_open_schedule,
    timing_signal,
)


dates = pd.bdate_range("2017-01-02", periods=700)
base = np.linspace(10.0, 20.0, len(dates)) + np.sin(np.arange(len(dates)) / 5) * 0.1
index = pd.DataFrame({"close": base, "low": base * 0.99, "high": base * 1.01}, index=dates)
cutoff = dates[-5]
score = momentum_score(index["close"], cutoff)
timing = timing_signal(index, cutoff)
future = pd.concat([index, pd.DataFrame({
    "close": [999.0], "low": [0.01], "high": [1000.0],
}, index=[pd.Timestamp("2021-01-04")])])
assert score == momentum_score(future["close"], cutoff)
assert timing == timing_signal(future, cutoff)
assert timing is not None

single = pd.DataFrame({"close": [10.0]}, index=[pd.Timestamp("2020-01-02")])
assert mark_to_market(100.0, "etf", 10, {"etf": single}, pd.Timestamp("2020-01-03"), {"etf": 12.5}, 10.0) == 225.0

assert next_open_schedule(
    ["2020-01-03", "2020-01-10"], pd.bdate_range("2020-01-01", "2020-01-13")
) == [
    {"signal": "2020-01-03", "fill": "2020-01-06"},
    {"signal": "2020-01-10", "fill": "2020-01-13"},
]
assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert SOURCE_BLOB_SHA == "5ae5c8669b6eb22ad86870daab03c5081895d2ed"
assert SOURCE_SHA256 == "c0bcc56927a40e3eec8c575f8678e5fa3ac8b4651cc230badc686e037dede583"
assert PARAMETERS["momentum_days"] == 29
assert PARAMETERS["rsrs_n"] == 18
assert PARAMETERS["rsrs_m"] == 600
assert PARAMETERS["rsrs_threshold"] == 0.7
assert PARAMETERS["fill"] == "next open, T+1"
print("research_etf_rsrs_momentum_ok")
