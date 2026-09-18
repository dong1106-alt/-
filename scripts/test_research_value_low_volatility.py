#!/usr/bin/env python3
import pandas as pd

from research_value_low_volatility import (
    PARAMETERS,
    SOURCE_BLOB_SHA,
    SOURCE_COMMIT,
    SOURCE_SHA256,
    _add_ttm_eps,
    _select_weights,
    _target_weights,
)


rows = []
for report_date, eps, bps, available in (
    ("2015-03-31", 0.2, 3.0, "2015-04-30"),
    ("2015-12-31", 1.0, 4.0, "2016-03-31"),
    ("2016-03-31", 0.4, 4.5, "2016-04-30"),
):
    rows.append({
        "code": "sh600001", "report_date": pd.Timestamp(report_date),
        "available_date": pd.Timestamp(available), "eps": eps, "bps": bps,
    })
ttm = _add_ttm_eps(pd.DataFrame(rows))
assert abs(ttm.iloc[-1]["ttm_eps"] - 1.2) < 1e-12
assert ttm.iloc[-1]["available_date"] == pd.Timestamp("2016-04-30")

day = pd.Timestamp("2017-01-06")
dates = pd.bdate_range(end=day, periods=241)
bars = {
    "sh600001": pd.DataFrame({"close": [10 + n * 0.01 for n in range(241)]}, index=dates),
    "sh600002": pd.DataFrame({"close": [10 + (n % 2) * 0.2 for n in range(241)]}, index=dates),
}
fundamentals = pd.DataFrame([
    {"code": "sh600001", "report_date": pd.Timestamp("2016-12-31"),
     "available_date": day, "ttm_eps": 1.0, "bps": 5.0},
    {"code": "sh600002", "report_date": pd.Timestamp("2016-12-31"),
     "available_date": day, "ttm_eps": 0.8, "bps": 4.0},
])
weights = _select_weights(day, set(bars), bars, fundamentals)
assert set(weights) == set(bars) and abs(sum(weights.values()) - 1) < 1e-12
assert weights["sh600001"] > weights["sh600002"]

future = {code: pd.concat([frame, pd.DataFrame(
    {"close": [999.0]}, index=[pd.Timestamp("2017-01-09")],
)]) for code, frame in bars.items()}
assert weights == _select_weights(day, set(bars), future, fundamentals)
invalid = {"sh600001": bars["sh600001"].copy()}
invalid["sh600001"].iloc[0, 0] = 0.0
assert _select_weights(day, {"sh600001"}, invalid, fundamentals) == {}

assert _target_weights({"a": 0.6, "b": 0.4}, {"a"}, False, 0.0) == {"a": 0.6}
assert _target_weights({"a": 0.6, "b": 0.4}, {"a"}, True, 0.11) == {"a": 0.3, "b": 0.2}
assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert SOURCE_BLOB_SHA == "2d112d1485fa77373531918b1427fa67bd3231f3"
assert SOURCE_SHA256 == "0eb848e4a857a394041ac7f90eafe13dfc2ef119da261c692aff0f240fc89951"
assert PARAMETERS["volatility_days"] == 241
assert PARAMETERS["drawdown_trigger"] == 0.10
assert PARAMETERS["fill"] == "next open, T+1"
print("research_value_low_volatility_ok")
