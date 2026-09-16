#!/usr/bin/env python3
"""因果预评分最小回归：评分不得依赖历史模拟交易列。"""
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.causal_quality import score_stock  # noqa: E402


n = 120
close = pd.Series(np.linspace(10.0, 16.0, n))
df = pd.DataFrame({
    "close": close,
    "low": close * 0.98,
    "ma5": close.rolling(5, min_periods=1).mean(),
    "ma20": close.rolling(20, min_periods=1).mean(),
    "ma60": close.rolling(60, min_periods=1).mean(),
    "valuation_percentile": 0.5,
    "dc_high": close * 2,
    "exit_low": close * 0.5,
})

score, detail = score_stock(df)
mutated = df.copy()
mutated["dc_high"] = 0.0
mutated["exit_low"] = close * 2
score_mutated, detail_mutated = score_stock(mutated)

assert score == score_mutated
assert detail == detail_mutated
assert detail["sim_trades"] == 0
print("causal_quality_ok")
