#!/usr/bin/env python3
"""Fail-closed audit for QuantMind's model-dependent inverse-volatility template."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
SOURCE_COMMIT = "bf65f66cc944341d1f28e29b20750dbf1c8dc06e"
SOURCE_FILES = {
    "strategy_templates/as37_inverse_vol.py": {
        "blob_sha": "025a6edba080ac4e08aa05b0cebcf58a01e36258",
        "sha256": "c2a2acd2f44f7f9572333222446e2e7b36d54a3f82623762690e3d532314011b",
    },
    "strategy_templates/as37_inverse_vol.json": {
        "blob_sha": "8659e1d8dba4eb92e9e927dfec686012cf9487e8",
        "sha256": "16700a39893930b3b5ea4ab55705f4fc765686bcd60d24873f475b372e031786",
    },
    "scripts/ashare_backtest_results.json": {
        "blob_sha": "74038ce12b4e959958dd6aa2169bd8d8e3bc0abf",
        "sha256": "52530a8f5a94e99d84224c2847b6f62884bc2cc466700aef60572e3cfc669538",
    },
}
MODEL_ID = "mdl_cn_train_20260906023306_57ce74a7_d5a3faa7"
PARAMETERS = {
    "signal": "<PRED> from external model",
    "topk": 30,
    "rebalance_days": 5,
    "vol_window": 20,
    "weight_cap": 0.08,
    "f_total_mv_min": 3_000_000_000.0,
    "f_amount_ma_5_min_wan": 5_000,
    "max_buy_drop": -0.03,
    "stop_loss": -0.08,
    "signal_cutoff": "previous trading day",
    "fill": "next open, T+1",
}
CLAIMED_RESULT = {
    "period": ["2024-01-02", "2024-12-31"],
    "annual_return_pct": 71.14533840942399,
    "max_drawdown_pct": 13.15885336576984,
    "sharpe": 2.856692229664964,
    "closed_trades": 2136,
    "sortino": None,
    "calmar": None,
    "model_id": MODEL_ID,
    "ran_at": "2026-09-10T01:51:15",
}


def inverse_vol_weights(
    prices: pd.DataFrame,
    base_weights: dict[str, float],
    cutoff,
    *,
    vol_window: int = PARAMETERS["vol_window"],
    weight_cap: float = PARAMETERS["weight_cap"],
) -> dict[str, float]:
    """Reproduce the template's weight transform using data through ``cutoff`` only."""
    if not base_weights:
        return {}
    columns = list(base_weights)
    history = prices.sort_index().loc[:pd.Timestamp(cutoff), columns]
    vol = history.pct_change(fill_method=None).iloc[-vol_window:].std(ddof=1)
    inverse = (1.0 / vol.clip(lower=1e-4)).dropna()
    if inverse.empty:
        return dict(base_weights)
    total = float(sum(base_weights.values()))
    scaled = inverse / inverse.sum() * total
    if 0 < weight_cap < 1.0:
        scaled = scaled.clip(upper=weight_cap)
        if scaled.sum() > 0:
            scaled = scaled / scaled.sum() * total
    return {key: float(value) for key, value in scaled.items()}


def t1_open_schedule(signal_days, trading_days) -> list[dict[str, str]]:
    """Map each completed-close signal to the next available trading-day open."""
    calendar = sorted({pd.Timestamp(day) for day in trading_days})
    result = []
    for signal in sorted({pd.Timestamp(day) for day in signal_days}):
        later = [day for day in calendar if day > signal]
        if later:
            result.append({"signal": str(signal)[:10], "fill": str(later[0])[:10]})
    return result


def audit_inputs(model_manifest: dict | None = None, prediction_manifest: dict | None = None) -> dict:
    failures = []
    if not model_manifest:
        failures.append("fixed model artifact and causal training manifest are absent")
    if not prediction_manifest:
        failures.append("fixed 2011-2017 daily <PRED> matrix and hashes are absent")
    failures.extend([
        "the published 2024 result uses a model identified in 2026; its training cutoff is undisclosed",
        "the published result does not report Sortino or Calmar",
    ])
    return {
        "model_manifest_present": bool(model_manifest),
        "prediction_manifest_present": bool(prediction_manifest),
        "fixed_tree_model_or_prediction_artifacts": 0,
        "failures": failures,
    }


def run(start: str, end: str) -> dict:
    audit = audit_inputs()
    return {
        "strategy": "quantmind-as37-inverse-vol-model-audit-v1",
        "source": {
            "repository": "qusong0627/QuantMind",
            "commit": SOURCE_COMMIT,
            "files": SOURCE_FILES,
        },
        "period": {"start": start, "end": end},
        "model_id": MODEL_ID,
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(
            json.dumps(PARAMETERS, sort_keys=True).encode()
        ).hexdigest(),
        "claimed_result": CLAIMED_RESULT,
        "input_audit": audit,
        "metrics": None,
        "hard_gate": {
            "repository_thresholds": EXCELLENT_THRESHOLDS,
            "development_failures": audit["failures"],
        },
        "decision": "research_rejected",
        "replay_status": "not_run_missing_model_predictions",
        "limitations": [
            "No model scores are fabricated from price data",
            "No 2024 result is projected onto the 2011-2017 development period",
            "No 2002-2008 unseen data is read",
            "The candidate cannot enter shadow or release stages",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output",
        type=Path,
        default=DATA_ROOT / "research" / "inverse_vol_model_audit_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
