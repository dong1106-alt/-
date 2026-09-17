#!/usr/bin/env python3
import pandas as pd

from research_v27_price_volume import _add_features, _intraday_exit, _rank


frame = pd.DataFrame({
    "open": range(10, 90), "high": range(11, 91), "low": range(9, 89),
    "close": range(10, 90), "volume": [1000 + (i % 7) * 100 for i in range(80)],
})
causal = _add_features(frame)
extended = pd.concat([frame, pd.DataFrame({
    "open": [1000], "high": [1100], "low": [900], "close": [1050], "volume": [999999],
})], ignore_index=True)
extended_features = _add_features(extended)
for column in ("momentum5", "gap", "pv_corr10", "pv_corr20", "bollinger_width"):
    left, right = causal.loc[79, column], extended_features.loc[79, column]
    assert (pd.isna(left) and pd.isna(right)) or left == right

rows = []
for i in range(20):
    rows.append({
        "code": f"s{i}", "momentum5": 0.06 + i / 1000,
        "pv_corr10": 0.0, "pv_corr20": 0.1, "gap": 0.0,
        "bollinger_width": 0.2, "price_level": 10 + i,
        "price_trend": 0.1, "volume_shrink": 1.0,
        "volatility_abnormal": 1.0,
    })
ranking = _rank(rows)
assert ranking and ranking[0] == "s19"

both = pd.Series({"open": 100.0, "high": 105.0, "low": 98.0})
assert _intraday_exit(both, 100.0) == (98.5, "stop")
gap = pd.Series({"open": 97.0, "high": 100.0, "low": 96.0})
assert _intraday_exit(gap, 100.0) == (97.0, "stop_gap")
print("research_v27_price_volume_ok")
