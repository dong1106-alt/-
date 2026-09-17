#!/usr/bin/env python3
import pandas as pd

from research_v61c_low_turnover import _intraday_exit, _rank_codes


rows = [
    ("small-cold", 1.0, 0.1),
    ("small-hot", 1.0, 1.0),
    ("large-cold", 10.0, 0.1),
    ("large-hot", 10.0, 1.0),
]
assert _rank_codes(rows)[0] == "small-cold"
assert _rank_codes(rows)[-1] == "large-hot"

bar = pd.Series({"open": 91.0, "high": 95.0, "low": 90.0})
assert _intraday_exit(bar, 100.0) == (91.0, "stop_gap")
bar = pd.Series({"open": 100.0, "high": 126.0, "low": 99.0})
assert _intraday_exit(bar, 100.0) == (125.0, "take")
bar = pd.Series({"open": 100.0, "high": 110.0, "low": 95.0})
assert _intraday_exit(bar, 100.0) is None
print("research_v61c_low_turnover_ok")
