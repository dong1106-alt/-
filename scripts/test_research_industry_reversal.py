#!/usr/bin/env python3
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import pandas as pd

from research_industry_reversal import (
    DEPRECATED_INDUSTRIES,
    ETF_POOL,
    INSTRUMENT_POOL,
    PARAMETERS,
    SOURCE_BLOB_SHA,
    SOURCE_COMMIT,
    SOURCE_SHA256,
    audit_inputs,
    monthly_schedule,
    predict_industries,
)


months = pd.date_range("2017-01-31", periods=40, freq="ME")
returns = pd.DataFrame(
    np.random.default_rng(7).normal(0.01, 0.03, (40, 6)),
    index=months,
    columns=["行业A", "行业B", "行业C", "行业D", "行业E", "行业F"],
)
cutoff = months[-3]
before = predict_industries(returns, cutoff)
future = returns.copy()
future.loc[months[-2]:, :] = 999.0
pd.testing.assert_series_equal(before, predict_industries(future, cutoff))
assert len(before) == PARAMETERS["selected_industries"] == 5

schedule = monthly_schedule(pd.to_datetime([
    "2020-01-30", "2020-01-31", "2020-02-03", "2020-02-04", "2020-03-02",
]))
assert schedule == [
    {"signal": "2020-01-31", "fill": "2020-02-03"},
    {"signal": "2020-02-04", "fill": "2020-03-02"},
]

with TemporaryDirectory() as temp:
    root = Path(temp)
    (root / "industries").mkdir()
    (root / "fundamentals").mkdir()
    (root / "stocks").mkdir()
    (root / "industries" / "manifest.json").write_text(json.dumps({
        "source": "BaoStock.query_stock_industry CSRC classification",
        "coverage": 0.997332, "complete": False,
    }), encoding="utf-8")
    (root / "fundamentals" / "manifest.json").write_text(json.dumps({
        "coverage": 0.899981, "complete": False,
    }), encoding="utf-8")
    audit = audit_inputs(root)
    assert len(audit["missing_etfs"]) == len(ETF_POOL) == 16
    assert any("行业分类不兼容" in failure for failure in audit["failures"])
    assert any("99.7332%" in failure for failure in audit["failures"])

assert SOURCE_COMMIT == "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
assert SOURCE_BLOB_SHA == "50158a06d2013610b4af19f9ec7ca6ad0c8c1713"
assert SOURCE_SHA256 == "cb39f0e494cbafd23a835ba51498b9fc2d30fc25dde2edbdb1c0a9404418fb4d"
assert PARAMETERS["rolling_months"] == 36
assert PARAMETERS["short_months"] == 3
assert PARAMETERS["medium_months"] == 6
assert PARAMETERS["fill"] == "next open, T+1"
assert PARAMETERS["fixed_slippage"] == 0.001
assert PARAMETERS["minimum_commission"] == 5.0
assert len(INSTRUMENT_POOL) == 32
assert len(ETF_POOL) == 16
assert DEPRECATED_INDUSTRIES == (
    "801060", "801070", "801090", "801100", "801190", "801220",
)
print("research_industry_reversal_ok")
