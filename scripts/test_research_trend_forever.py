#!/usr/bin/env python3
import numpy as np
import pandas as pd

from research_trend_forever import (
    PARAMETERS, SOURCE_COMMIT, _features, _momentum_score, _tradable, _wilder_atr,
)


dates = pd.bdate_range("2020-01-01", periods=130)
trend = pd.Series(10 * np.exp(np.arange(100) * 0.002), index=dates[-100:])
choppy = trend * np.where(np.arange(100) % 2, 1.08, 0.92)
assert _momentum_score(trend) > _momentum_score(choppy)

frame = pd.DataFrame({
    "open": 10.0,
    "high": trend.reindex(dates).ffill().bfill() * 1.01,
    "low": trend.reindex(dates).ffill().bfill() * 0.99,
    "close": trend.reindex(dates).ffill().bfill(),
}, index=dates)
feature = _features(frame, dates[-1])
assert feature and feature["good"] and feature["atr"] > 0 and feature["score"] > 0
assert abs(_wilder_atr(frame.tail(21)) - float((frame["high"] - frame["low"]).tail(20).mean())) < 0.01

cutoff = dates[-11]
before = _features(frame, cutoff)
future = frame.copy()
future.loc[dates[-10]:, ["high", "low", "close"]] *= 50
assert before == _features(future, cutoff)

gapped = frame.copy()
gapped.loc[dates[-1], "low"] = gapped.loc[dates[-3], "high"] * 1.16
assert not _features(gapped, dates[-1])["good"]

row = pd.Series({"open": 10.2, "volume": 1000, "trade_status": 1})
assert _tradable(row, 10.0, "buy")
assert not _tradable(row, 0.0, "buy")

assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert PARAMETERS["momentum_days"] == 90
assert PARAMETERS["rank_threshold"] == 60
assert PARAMETERS["risk_factor"] == 0.001
print("research_trend_forever_ok")
