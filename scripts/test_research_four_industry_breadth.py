#!/usr/bin/env python3
import pandas as pd

from research_four_industry_breadth import (
    _add_ttm_roa, _closed_at_limit, _industry_breadth, _latest_as_of,
    _risk_industry, _select_targets,
)


assert _risk_industry("J66 banking") == "bank"
assert _risk_industry("C31 steel") == "steel"
assert _risk_industry("C32 nonferrous") == "nonferrous"
assert _risk_industry("B06 coal") == "coal"
assert _risk_industry("金融保险业-银行业") == "bank"
assert _risk_industry("制造业-黑色金属冶炼及压延加工业") == "steel"
assert _risk_industry("制造业-有色金属冶炼及压延加工业") == "nonferrous"
assert _risk_industry("采掘业-煤炭开采业-煤炭采选业") == "coal"
assert _risk_industry("I65 software") is None
assert _closed_at_limit(pd.Series({"close": 10.5}), 10.0)
assert not _closed_at_limit(pd.Series({"close": 10.4}), 10.0)

fundamental_rows = []
for report_date, profit, assets, available in (
    ("2015-03-31", 10, 100, "2015-04-30"),
    ("2015-12-31", 50, 120, "2016-03-31"),
    ("2016-03-31", 20, 140, "2016-04-30"),
):
    fundamental_rows.append({
        "code": "sz002001", "report_date": pd.Timestamp(report_date),
        "available_date": pd.Timestamp(available), "net_profit": profit,
        "total_assets": assets, "roe": 0.20,
    })
ttm = _add_ttm_roa(pd.DataFrame(fundamental_rows))
row = ttm.iloc[-1]
assert row["available_date"] == pd.Timestamp("2016-04-30")
assert abs(row["roa_ttm"] - 60 / 120) < 1e-12

future = pd.concat([pd.DataFrame(fundamental_rows), pd.DataFrame([{
    "code": "sz002001", "report_date": pd.Timestamp("2016-12-31"),
    "available_date": pd.Timestamp("2017-03-31"), "net_profit": 999,
    "total_assets": 150, "roe": 0.99,
}])], ignore_index=True)
before = _latest_as_of(ttm, pd.Timestamp("2016-06-01"))
after = _latest_as_of(_add_ttm_roa(future), pd.Timestamp("2016-06-01"))
pd.testing.assert_frame_equal(before, after)

day = pd.Timestamp("2017-01-06")
bars = {
    "a": pd.DataFrame({"close": [9, 11], "ma20": [10, 10]}, index=pd.to_datetime(["2017-01-05", "2017-01-06"])),
    "b": pd.DataFrame({"close": [11, 9], "ma20": [10, 10]}, index=pd.to_datetime(["2017-01-05", "2017-01-06"])),
    "c": pd.DataFrame({"close": [11, 12], "ma20": [10, 10]}, index=pd.to_datetime(["2017-01-05", "2017-01-06"])),
}
top, ratios, count = _industry_breadth({"a", "b", "c"}, bars, {"a": "J66", "b": "J66", "c": "I65"}, day)
assert top == "I65" and ratios == {"J66": 0.5, "I65": 1.0} and count == 3
extended = {
    code: pd.concat([frame, pd.DataFrame(
        {"close": [999.0], "ma20": [1.0]}, index=[pd.Timestamp("2017-01-09")],
    )]) for code, frame in bars.items()
}
assert _industry_breadth(
    {"a", "b", "c"}, extended, {"a": "J66", "b": "J66", "c": "I65"}, day,
) == (top, ratios, count)

cross = pd.DataFrame([
    {"code": "sz002001", "roe": 0.16, "roa_ttm": 0.11},
    {"code": "sz002002", "roe": 0.20, "roa_ttm": 0.20},
    {"code": "sh600000", "roe": 0.30, "roa_ttm": 0.30},
]).set_index("code")
liquidity = {
    "sz002001": (day, 200.0, 1.0), "sz002002": (day, 100.0, 1.0),
    "sh600000": (day, 1.0, 1.0),
}
listed = {code: pd.Timestamp("2010-01-01") for code in cross.index}
assert _select_targets(day, set(cross.index), cross, liquidity, listed) == [
    "sz002002", "sz002001",
]

print("research_four_industry_breadth_ok")
