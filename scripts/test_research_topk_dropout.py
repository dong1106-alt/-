#!/usr/bin/env python3
import pandas as pd

from research_topk_dropout import _features, _rank_scores


frame = pd.DataFrame({
    "open": range(10, 90), "high": range(11, 91), "low": range(9, 89),
    "close": range(10, 90), "volume": [1000] * 80,
})
features = _features(frame, forward_horizon=5)
assert features.loc[0, "future5"] == frame.loc[6, "open"] / frame.loc[1, "open"] - 1
assert pd.isna(features.loc[74, "future5"])

cross = pd.DataFrame({"ret60": [-0.2, 0.0, 0.2]}, index=["low", "mid", "high"])
assert list(_rank_scores(cross, "reversal60").sort_values(ascending=False).index) == [
    "low", "mid", "high",
]
print("research_topk_dropout_ok")
