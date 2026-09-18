#!/usr/bin/env python3
import numpy as np
import pandas as pd

from research_inverse_vol_model_audit import (
    CLAIMED_RESULT,
    MODEL_ID,
    PARAMETERS,
    SOURCE_COMMIT,
    SOURCE_FILES,
    audit_inputs,
    inverse_vol_weights,
    run,
    t1_open_schedule,
)


dates = pd.bdate_range("2020-01-01", periods=40)
prices = pd.DataFrame({
    "sh600001": 10.0 + np.arange(40) * 0.03,
    "sz000002": 12.0 + np.sin(np.arange(40)) * 0.2,
}, index=dates)
base = {"sh600001": 0.5, "sz000002": 0.5}
cutoff = dates[-6]
before = inverse_vol_weights(prices, base, cutoff)
future = pd.concat([prices, pd.DataFrame({
    "sh600001": [999.0], "sz000002": [0.01],
}, index=[pd.Timestamp("2021-01-04")])])
assert before == inverse_vol_weights(future, base, cutoff)
assert abs(sum(before.values()) - 1.0) < 1e-12

schedule = t1_open_schedule(
    ["2020-01-03", "2020-01-10"],
    pd.bdate_range("2020-01-01", "2020-01-13"),
)
assert schedule == [
    {"signal": "2020-01-03", "fill": "2020-01-06"},
    {"signal": "2020-01-10", "fill": "2020-01-13"},
]

audit = audit_inputs()
assert not audit["model_manifest_present"]
assert not audit["prediction_manifest_present"]
assert audit["fixed_tree_model_or_prediction_artifacts"] == 0
assert len(audit["failures"]) == 4
result = run("2011-01-04", "2017-12-29")
assert result["decision"] == "research_rejected"
assert result["replay_status"] == "not_run_missing_model_predictions"
assert result["metrics"] is None

assert SOURCE_COMMIT == "bf65f66cc944341d1f28e29b20750dbf1c8dc06e"
assert SOURCE_FILES["strategy_templates/as37_inverse_vol.py"]["blob_sha"] == (
    "025a6edba080ac4e08aa05b0cebcf58a01e36258"
)
assert SOURCE_FILES["strategy_templates/as37_inverse_vol.py"]["sha256"] == (
    "c2a2acd2f44f7f9572333222446e2e7b36d54a3f82623762690e3d532314011b"
)
assert MODEL_ID == "mdl_cn_train_20260906023306_57ce74a7_d5a3faa7"
assert PARAMETERS["topk"] == 30
assert PARAMETERS["rebalance_days"] == 5
assert PARAMETERS["vol_window"] == 20
assert PARAMETERS["weight_cap"] == 0.08
assert PARAMETERS["fill"] == "next open, T+1"
assert CLAIMED_RESULT["sortino"] is None
assert CLAIMED_RESULT["calmar"] is None
print("research_inverse_vol_model_audit_ok")
