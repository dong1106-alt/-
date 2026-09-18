#!/usr/bin/env python3
import pandas as pd

from research_smallcap_risk_control import (
    PARAMETERS,
    SOURCE_BLOB_SHA,
    SOURCE_COMMIT,
    SOURCE_SHA256,
    _rank_targets,
    _risk_signal,
    _wilder_rsi,
)


closes = pd.Series(range(1, 102), dtype=float)
assert _wilder_rsi(closes, 60) == 100.0
status, allowed, rate, rsi = _risk_signal("normal", closes)
assert status == "normal" and allowed and rate > 1 and rsi == 100.0

cutoff = 80
before = _risk_signal("normal", closes.iloc[:cutoff])
future = pd.concat([closes.iloc[:cutoff], pd.Series([9999.0] * 10)], ignore_index=True)
assert before == _risk_signal("normal", future.iloc[:cutoff])

warning_prices = pd.Series([100.0] * 100 + [20.0])
status, allowed, rate, _ = _risk_signal("normal", warning_prices)
assert status == "warning" and not allowed and rate < 0.30
recovery = pd.Series([100.0] * 100 + [50.0])
status, allowed, rate, _ = _risk_signal(status, recovery)
assert status == "normal" and allowed and 0.35 <= rate <= 0.70

day = pd.Timestamp("2017-01-06")
bars = {}
liquidity = {}
previous = {}
for number, code in enumerate(("sz002001", "sz002002", "sz002003", "sh600000"), 1):
    bars[code] = pd.DataFrame({
        "close": [10.0], "volume": [1000.0], "trade_status": [1],
    }, index=[day])
    liquidity[code] = (day, number * 1_000_000_000.0, 1.0)
    previous[code] = 10.0
assert _rank_targets(day, set(bars), bars, liquidity, previous, set()) == [
    "sz002001", "sz002002", "sz002003",
]
future_bars = {code: pd.concat([frame, pd.DataFrame({
    "close": [999.0], "volume": [1000.0], "trade_status": [1],
}, index=[pd.Timestamp("2017-01-09")])]) for code, frame in bars.items()}
assert _rank_targets(day, set(bars), future_bars, liquidity, previous, set()) == [
    "sz002001", "sz002002", "sz002003",
]

assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert SOURCE_BLOB_SHA == "6282002082716e9e56554aff6c2964ccd80532f7"
assert SOURCE_SHA256 == "f22e598f39147496c73592e9fd0e5250e85610ab02764ba49e11ecba38f62cb3"
assert PARAMETERS["stock_num"] == 5
assert PARAMETERS["maximum_float_market_cap"] == 10_000_000_000
assert PARAMETERS["causal_fill"] == "prior-close signal, next-open T+1 fill"
print("research_smallcap_risk_control_ok")
