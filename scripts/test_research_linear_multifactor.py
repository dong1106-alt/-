#!/usr/bin/env python3
import pandas as pd

from research_linear_multifactor import (
    PARAMETERS, _add_ttm_fields, _latest_as_of, _score_factors,
)


rows = []
for report_date, profit, eps, roe, available in (
    ("2015-03-31", 10, 0.10, 0.10, "2015-04-30"),
    ("2015-12-31", 50, 0.50, 0.20, "2016-03-31"),
    ("2016-03-31", 20, 0.20, 0.30, "2016-04-30"),
):
    rows.append({
        "code": "a", "report_date": pd.Timestamp(report_date),
        "available_date": pd.Timestamp(available), "net_profit": profit,
        "eps": eps, "roe": roe,
    })
ttm = _add_ttm_fields(pd.DataFrame(rows))
latest = _latest_as_of(ttm, pd.Timestamp("2016-05-01"))
assert latest.loc["a", "ttm_net_profit"] == 60
assert abs(latest.loc["a", "ttm_eps"] - 0.6) < 1e-12
assert latest.loc["a", "available_date"] == pd.Timestamp("2016-04-30")

future = pd.concat([pd.DataFrame(rows), pd.DataFrame([{
    "code": "a", "report_date": pd.Timestamp("2016-12-31"),
    "available_date": pd.Timestamp("2017-03-31"), "net_profit": 999,
    "eps": 9.99, "roe": 0.99,
}])], ignore_index=True)
pd.testing.assert_frame_equal(
    latest, _latest_as_of(_add_ttm_fields(future), pd.Timestamp("2016-05-01")),
)

cross = pd.DataFrame({
    "ttm_eps": [2.0, 1.0, 0.5],
    "roe": [0.3, 0.2, 0.1],
    "momentum20": [0.2, 0.1, -0.1],
    "close": [10.0, 10.0, 10.0],
    "float_market_cap": [100.0, 200.0, 300.0],
}, index=["best", "middle", "weak"])
scored = _score_factors(cross)
assert list(scored.index) == ["best", "middle", "weak"]
tied = _score_factors(pd.concat([cross.loc[["middle"]], cross.loc[["middle"]].rename(index={"middle": "aaa"})]))
assert list(tied.index) == ["aaa", "middle"]
assert PARAMETERS["factor_weights"] == {
    "earnings_yield": 0.30, "roe": 0.30,
    "momentum20": 0.20, "small_float_market_cap": 0.20,
}
assert PARAMETERS["rebalance_trading_days"] == 20
assert PARAMETERS["stock_num"] == 10
print("research_linear_multifactor_ok")
