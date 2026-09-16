#!/usr/bin/env python3
"""Each validation fold must use parameters fitted only on its own past."""
import importlib.util
import json
from pathlib import Path

import pandas as pd


root = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("guichan_wf_test", root / "龟缠量化v6_optimized.py")
core = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(core)

dates = list(pd.bdate_range("2023-01-02", periods=700))
events = []
study_number = 0


class Trial:
    def __init__(self, number):
        self.number = number
        self.params = {}

    def suggest_float(self, name, lower, upper):
        self.params[name] = lower + (upper - lower) * self.number / 10
        return self.params[name]

    def suggest_int(self, name, lower, upper):
        self.params[name] = min(upper, lower + self.number)
        return self.params[name]


class Study:
    def __init__(self, number):
        self.trial = Trial(number)
        self.best_params = {}
        self.best_value = -10

    def optimize(self, objective, **_):
        self.best_value = objective(self.trial)
        self.best_params = self.trial.params


def create_study(**_):
    global study_number
    study_number += 1
    return Study(study_number)


def backtest(_, params, _data, *, start_date, end_date, **_kwargs):
    events.append((pd.Timestamp(start_date), pd.Timestamp(end_date), params.get("vol_ratio_high")))
    return {"sharpe": 1 if params == core.STATE_BASELINES["bull"] else 2,
            "total_return": 5, "trades": 12, "max_drawdown": -4, "win_rate": 55}


core.optuna.create_study = create_study
core.backtest_multi_stocks = backtest
result = core.optimize_for_state(
    "bull", ["sh600000"], {}, dates, n_trials=1, calendar_dates=dates,
)
folds = result["fold_metrics"]
assert len(folds) >= 3
assert result["params"] == folds[-1]["params"]
assert len({row["params"]["vol_ratio_high"] for row in folds}) == len(folds)
json.dumps(result)
for index, row in enumerate(folds):
    train, validation, baseline = events[index * 3:index * 3 + 3]
    assert train[1] < validation[0]
    assert validation[0] == baseline[0]
    assert train[2] == validation[2] == row["params"]["vol_ratio_high"]

calendar_folds, _ = core.build_walk_forward_folds(dates)
assert len(calendar_folds) == 4
events.clear()
study_number = 0
sparse_days = dates[:252] + [calendar_folds[i].validation_start for i in (0, 2, 3)]
sparse = core.optimize_for_state(
    "bull", ["sh600000"], {}, sparse_days, n_trials=1, calendar_dates=dates,
)
assert len(sparse["fold_metrics"]) == 4
assert [f["eligible"] for f in sparse["fold_metrics"]] == [True, False, True, True]
assert sparse["eligible_fold_count"] == 3
assert sparse["val_trades"] == 36
assert len(events) == 9  # No optimization or backtest for the empty-state fold.

too_sparse = core.optimize_for_state(
    "bull", ["sh600000"], {}, dates[:252] + [calendar_folds[i].validation_start for i in (0, 2)],
    n_trials=1, calendar_dates=dates,
)
assert too_sparse["eligible_fold_count"] == 2
assert too_sparse["status"] == "rejected"

no_fresh = core.optimize_for_state(
    "bull", ["sh600000"], {}, dates, n_trials=1, calendar_dates=dates,
    validation_after=str(calendar_folds[-1].validation_end)[:10],
)
assert no_fresh["status"] == "research_only"
assert no_fresh["fold_metrics"] == []

print("walk_forward_optimization_ok")
