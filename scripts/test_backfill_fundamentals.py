#!/usr/bin/env python3
import pandas as pd

from backfill_fundamentals import (
    _date, _merge, _normalize_balance, _normalize_performance, _report_dates,
)


assert _report_dates("2010-01-01", "2010-12-31") == [
    "2010-03-31", "2010-06-30", "2010-09-30", "2010-12-31",
]
assert pd.isna(_date(None))

allowed = {"sh600519"}
performance = _normalize_performance([{
    "SECURITY_CODE": "600519", "SECUCODE": "600519.SH",
    "REPORTDATE": "2017-12-31", "NOTICE_DATE": "2018-03-28",
    "UPDATE_DATE": "2019-03-29", "BASIC_EPS": 21.56,
    "PARENTNETPROFIT": 27000000000, "WEIGHTAVG_ROE": 32.95,
    "SJLTZ": 61.58, "BPS": 90.0, "MGJYXJJE": 20.0,
}], allowed)
balance = _normalize_balance([{
    "SECURITY_CODE": "600519", "SECUCODE": "600519.SH",
    "REPORT_DATE": "2017-12-31", "NOTICE_DATE": "2019-04-01",
    "DEBT_ASSET_RATIO": 28.67, "TOTAL_ASSETS": 100, "TOTAL_LIABILITIES": 28.67,
}], allowed)
frame = _merge(performance, balance)
row = frame.iloc[0]
assert row["performance_available_date"] == pd.Timestamp("2019-03-29")
assert row["available_date"] == pd.Timestamp("2019-04-01")
assert abs(row["roe"] - 0.3295) < 1e-12
assert abs(row["profit_yoy"] - 0.6158) < 1e-12
assert abs(row["debt_ratio"] - 0.2867) < 1e-12
assert bool(row["complete_factors"])
print("backfill_fundamentals_ok")
