#!/usr/bin/env python3
from datetime import date, timedelta

from causal_backtest import (
    assert_causal_fills,
    build_walk_forward_folds,
    execute_fill,
    first_tradable_after,
    stop_fill_price,
)


dates = [date(2020, 1, 1) + timedelta(days=i) for i in range(700)]
folds, holdout = build_walk_forward_folds(dates)
assert len(folds) >= 3
assert len(holdout) == 126
assert folds[0].validation_start > folds[0].train_end
for previous, current in zip(folds, folds[1:]):
    assert current.train_end < current.validation_start
    assert previous.validation_end < current.validation_start
assert stop_fill_price(8.0, 7.5, 9.0) == 8.0
assert stop_fill_price(10.0, 8.5, 9.0) == 9.0
assert stop_fill_price(10.0, 9.5, 9.0) is None
bars = [
    {"date": dates[1], "open": 10, "volume": 0},
    {"date": dates[2], "open": 11, "volume": 100},
]
assert first_tradable_after(bars, dates[0]) == bars[1]
assert first_tradable_after(bars[:1], dates[0]) is None
cost, record = execute_fill(
    side="sell", signal_date=dates[0], fill_date=dates[1],
    fill_source="gap_stop", open_price=8.0, low_price=7.5,
    stop_price=9.0, shares=100, trade_cfg={}, market_cap=80,
)
assert record["raw_price"] == 8.0 and cost[0] < 8.0
_, record = execute_fill(
    side="sell", signal_date=dates[0], fill_date=dates[1],
    fill_source="intraday_stop", open_price=10.0, low_price=8.5,
    stop_price=9.0, shares=100, trade_cfg={}, market_cap=80,
)
assert record["raw_price"] == 9.0
try:
    execute_fill(side="buy", signal_date=dates[0], fill_date=dates[0],
                 fill_source="next_open", open_price=10, shares=100,
                 trade_cfg={}, market_cap=80)
except ValueError:
    pass
else:
    raise AssertionError("same-day execution accepted")
assert_causal_fills([{
    "signal_date": date(2024, 1, 2),
    "fill_date": date(2024, 1, 3),
    "fill_source": "next_open",
}])
try:
    assert_causal_fills([{
        "signal_date": date(2024, 1, 2),
        "fill_date": date(2024, 1, 2),
        "fill_source": "next_open",
    }])
except AssertionError:
    pass
else:
    raise AssertionError("same-bar fill was accepted")

print("causal_backtest_ok")
