#!/usr/bin/env python3
import numpy as np
import pandas as pd

from research_contrarian_momentum import (
    PARAMETERS,
    SOURCE_BLOB_SHA,
    SOURCE_COMMIT,
    SOURCE_SHA256,
    _prepare_features,
    _rank_targets,
)


dates = pd.bdate_range("2020-01-01", periods=120)
bars = {}
for code, close in (
    ("sh600001", np.linspace(20.0, 10.0, len(dates))),
    ("sz000001", np.linspace(10.0, 20.0, len(dates))),
    ("sh600002", np.linspace(4.5, 4.0, len(dates))),
):
    bars[code] = pd.DataFrame({
        "open": close, "high": close, "low": close, "close": close,
        "volume": 1_000_000.0, "trade_status": 1,
    }, index=dates)

closes, momentums = _prepare_features(bars)
listed_since = {code: dates[0] - pd.Timedelta(days=300) for code in bars}
targets = _rank_targets(dates[-1], set(bars), closes, momentums, listed_since)
assert targets == ["sh600001", "sz000001"]

cutoff = dates[-11]
before = _rank_targets(cutoff, set(bars), closes, momentums, listed_since)
future_bars = {
    code: pd.concat([frame, pd.DataFrame({
        "open": [999.0], "high": [999.0], "low": [999.0], "close": [999.0],
        "volume": [1_000_000.0], "trade_status": [1],
    }, index=[pd.Timestamp("2021-01-01")])])
    for code, frame in bars.items()
}
future_closes, future_momentums = _prepare_features(future_bars)
assert before == _rank_targets(
    cutoff, set(bars), future_closes, future_momentums, listed_since,
)

recent = dict(listed_since)
recent["sh600001"] = dates[-1] - pd.Timedelta(days=249)
assert "sh600001" not in _rank_targets(dates[-1], set(bars), closes, momentums, recent)
assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert SOURCE_BLOB_SHA == "e7eb3b2140589450dba08e03ed5e4182400e7cd3"
assert SOURCE_SHA256 == "f7a148bca1e8cffa1ea297d22f36bb6d60a1eddb20230165bd167d1dc9cd9644"
assert PARAMETERS["momentum_observations"] == 91
assert PARAMETERS["stock_num"] == 10
assert PARAMETERS["fill"] == "next open, T+1"
print("research_contrarian_momentum_ok")
