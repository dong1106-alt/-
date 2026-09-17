#!/usr/bin/env python3
import pandas as pd

from research_factor_ic import _features, _portfolio_row, _portfolio_summary, _rank_ic


frame = pd.DataFrame({
    "open": range(10, 90),
    "high": range(11, 91),
    "low": range(9, 89),
    "close": range(10, 90),
    "volume": [1000] * 80,
})
features = _features(frame, volume_multiplier=1)
assert features.loc[0, "future5"] == frame.loc[6, "open"] / frame.loc[1, "open"] - 1
assert pd.isna(features.loc[74, "future5"])
assert features.loc[79, "liquidity20"] == frame.loc[79, "close"] * 1000 - 9500
assert features.loc[79, "overnight5"] > 0
assert features.loc[79, "intraday1"] == 0
assert pd.isna(features.loc[79, "ivol20"])

market = pd.Series([0.001 * ((i % 7) - 3) for i in range(80)])
causal = _features(frame, volume_multiplier=1, market_returns=market)
extended = pd.concat([
    frame,
    pd.DataFrame({
        "open": [1000.0], "high": [1100.0], "low": [900.0],
        "close": [1050.0], "volume": [999999.0],
    }),
], ignore_index=True)
extended_market = pd.concat([market, pd.Series([0.5])], ignore_index=True)
extended_features = _features(extended, volume_multiplier=1, market_returns=extended_market)
for column in ("overnight5", "intraday1", "ivol20"):
    assert causal.loc[79, column] == extended_features.loc[79, column]
assert causal.loc[79, "ivol20"] >= 0

cross = pd.DataFrame({
    "factor": [float(i) for i in range(60)],
    "future5": [float(i) for i in range(60)],
})
cross.loc[0, "factor"] = float("inf")
assert _rank_ic(cross, "factor") == 1.0
portfolio, selected = _portfolio_row(cross, "factor", set())
assert portfolio is None and selected == set()
large = pd.DataFrame({
    "factor": [float(i) for i in range(100)],
    "future5": [float(100 - i) / 1000 for i in range(100)],
}, index=[f"s{i}" for i in range(100)])
portfolio, selected = _portfolio_row(large, "factor", set())
assert len(selected) == 10 and portfolio["turnover"] == 1.0
summary = _portfolio_summary([portfolio])
assert summary["mean_low_minus_high_5d_pct"] > 0
print("research_factor_ic_ok")
