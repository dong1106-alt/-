#!/usr/bin/env python3
import pandas as pd

from research_quality_value import _future_open_return, _latest_as_of, _score


fundamentals = pd.DataFrame([
    {"code": "a", "report_date": pd.Timestamp("2016-12-31"),
     "available_date": pd.Timestamp("2017-03-01"), "eps": 1},
    {"code": "a", "report_date": pd.Timestamp("2017-03-31"),
     "available_date": pd.Timestamp("2017-05-01"), "eps": 2},
    {"code": "a", "report_date": pd.Timestamp("2015-12-31"),
     "available_date": pd.Timestamp("2017-06-01"), "eps": 0.5},
])
assert _latest_as_of(fundamentals, pd.Timestamp("2017-04-01")).loc["a", "eps"] == 1
assert _latest_as_of(fundamentals, pd.Timestamp("2017-07-01")).loc["a", "eps"] == 2

prices = pd.DataFrame({"open": range(10, 50)})
future = _future_open_return(prices)
assert future.iloc[0] == prices.loc[21, "open"] / prices.loc[1, "open"] - 1
assert future.iloc[-21:].isna().all()

cross = pd.DataFrame({
    "industry": pd.Series(["制造业", "制造业"], index=["good", "weak"], dtype="string[pyarrow]"),
    "roe": [0.2, 0.1], "profit_yoy": [0.2, 0.1],
    "debt_ratio": [0.2, 0.6], "ocf_per_share": [2.0, 0.5],
    "eps": [1.0, 1.0], "bps": [5.0, 2.0], "price": [10.0, 10.0],
}, index=["good", "weak"])
scored = _score(cross)
assert scored.loc["good", "quality_value_score"] > scored.loc["weak", "quality_value_score"]
assert scored.loc["good", "earnings_yield"] == 0.1
print("research_quality_value_ok")
