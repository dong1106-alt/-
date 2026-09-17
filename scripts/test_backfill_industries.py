#!/usr/bin/env python3
from types import SimpleNamespace

import pandas as pd

from backfill_industries import _normalize, _pagination_complete, _weekly_snapshot_dates


days = ["2017-01-03", "2017-01-06", "2017-01-09", "2017-01-13"]
assert _weekly_snapshot_dates(days, "2017-01-01", "2017-01-31") == [
    "2017-01-06", "2017-01-13",
]

frame = _normalize([
    ["2017-01-02", "sh.600000", "bank", "J66 banking", "CSRC"],
    ["2017-01-02", "sz.000001", "bank2", "J66 banking", "CSRC"],
    ["2017-01-02", "sz.300001", "excluded", "I65 software", "CSRC"],
], {"sh600000", "sz000001"}, "2017-01-06")
assert list(frame["code"]) == ["sh600000", "sz000001"]
assert frame["snapshot_date"].eq(pd.Timestamp("2017-01-06")).all()
assert frame["updateDate"].max() <= frame["snapshot_date"].min()
assert _pagination_complete(
    SimpleNamespace(error_code="0", per_page_count="2000", data=[None] * 10), [1],
)
assert not _pagination_complete(
    SimpleNamespace(error_code="0", per_page_count="2000", data=[None] * 2000), [1],
)

try:
    _normalize([
        ["2017-01-07", "sh.600000", "bank", "J66 banking", "CSRC"],
    ], {"sh600000"}, "2017-01-06")
except RuntimeError as exc:
    assert "non-causal" in str(exc)
else:
    raise AssertionError("future industry update was accepted")

print("backfill_industries_ok")
