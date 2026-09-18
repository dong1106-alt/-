#!/usr/bin/env python3
import numpy as np
import pandas as pd

from research_shortline_ma_macd import (
    PARAMETERS,
    SOURCE_BLOB_SHA,
    SOURCE_COMMIT,
    SOURCE_SHA256,
    _prepare_signals,
    _rank_targets,
)


dates = pd.bdate_range("2020-01-01", periods=80)
close = 10.0 + np.arange(len(dates)) * 0.01 + np.sin(np.arange(len(dates))) * 0.05
frame = pd.DataFrame({
    "open": close * 0.999, "high": close * 1.01, "low": close * 0.99,
    "close": close, "volume": 1_000_000.0, "trade_status": 1,
}, index=dates)
signals = _prepare_signals({"sh600001": frame})
cutoff = dates[-11]
future = pd.concat([frame, pd.DataFrame({
    "open": [999.0], "high": [999.0], "low": [999.0], "close": [999.0],
    "volume": [999.0], "trade_status": [1],
}, index=[pd.Timestamp("2021-01-01")])])
future_signals = _prepare_signals({"sh600001": future})
assert bool(signals.loc[cutoff, "sh600001"]) == bool(future_signals.loc[cutoff, "sh600001"])

manual = pd.DataFrame({
    "sz000002": [True], "sh600001": [True], "sz300001": [True],
}, index=[cutoff])
assert _rank_targets(cutoff, set(manual.columns), manual) == ["sh600001", "sz000002"]
assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert SOURCE_BLOB_SHA == "082e21553eeec78321b86951a4c1428fad35eff5"
assert SOURCE_SHA256 == "2b59da01105558cd44b3aab3e23f4850aa9d552d176d299bc0c571e9fe29589d"
assert PARAMETERS["macd"] == [12, 26, 9]
assert PARAMETERS["stock_num"] == 1
assert PARAMETERS["fill"] == "next open, T+1"
print("research_shortline_ma_macd_ok")
