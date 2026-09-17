#!/usr/bin/env python3
import math

import pandas as pd

from backfill_liquidity import normalize


frame = normalize([
    ["2017-01-03", "10", "5000000", "50000000", "0.05", "1", "0"],
    ["2017-01-04", "11", "0", "0", "", "0", "1"],
])
assert frame.iloc[0]["float_shares"] == 10_000_000_000
assert frame.iloc[0]["float_market_cap"] == 100_000_000_000
assert math.isnan(frame.iloc[1]["float_market_cap"])
assert frame.iloc[1]["trade_status"] == 0
assert frame.iloc[1]["is_st"] == 1

weekly = normalize(
    [["2017-01-06", "10", "5000000", "50000000", "0.05"]],
    ("date", "close", "volume", "amount", "turn"),
)
assert weekly.iloc[0]["float_market_cap"] == 100_000_000_000
assert pd.isna(weekly.iloc[0]["trade_status"])
assert pd.isna(weekly.iloc[0]["is_st"])
print("backfill_liquidity_ok")
