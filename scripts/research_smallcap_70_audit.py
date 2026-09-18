#!/usr/bin/env python3
"""Fail-closed audit of the public JoinQuant small-cap strategy."""
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
SOURCE_COMMIT = "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
SOURCE_BLOB_SHA = "9237456662067f2d62281eb550bf0df06778f15f"
SOURCE_SHA256 = ""
SOURCE_URL = f"https://github.com/ShenzhenLime/factor_mining/blob/{SOURCE_COMMIT}/ref/ref_code/124/0208/2022%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89%E7%AD%96%E7%95%A5/36.%E5%B9%B4%E5%8C%9670%EF%BC%8C%E8%BF%91%E4%B8%89%E5%B9%B4%E8%BF%98%E6%9C%89%E6%95%88%E7%9A%84%E5%B0%8F%E5%B8%82%E5%80%BC%E7%AD%96%E7%95%A5.py"
PARAMETERS = {
    "benchmark": "000300.XSHG", "maximum_total_market_cap_yuan": 2_000_000_000,
    "exclude": "创业板/ST/停牌/上市不足20日", "selection": "minimum total market cap",
    "holding_days": 30, "profit_trigger": 0.25, "trailing_drawdown": 0.02,
    "stop_loss": -0.08, "cooldown_days": 20, "signal": "prior-close filters",
    "fill": "next open, T+1", "rebalance": "after holding period or risk exit",
    "open_commission": 0.0003, "close_commission": 0.0003, "close_tax": 0.001,
    "minimum_commission": 5.0,
}


def monthly_schedule(trading_days):
    days = sorted(pd.Timestamp(day) for day in trading_days)
    return [{"signal": str(a)[:10], "fill": str(b)[:10]} for a, b in zip(days, days[1:])]


def _load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def audit_inputs(data_root: Path = DATA_ROOT) -> dict:
    liquidity = _load_manifest(data_root / "historical_validation" / "2010-2017" / "liquidity" / "manifest.json")
    failures = []
    if not liquidity:
        failures.append("缺少历史市值数据清单")
    elif not liquidity.get("complete"):
        failures.append(f"流通市值覆盖{float(liquidity.get('coverage', 0)):.4%}，未达到100%")
    failures.extend([
        "源码要求总市值，现有历史数据仅提供流通市值，禁止替代",
        "源码要求历史时点ST/停牌状态，本地数据无完整可证明字段",
    ])
    return {"liquidity_coverage": float(liquidity.get("coverage", 0)), "failures": failures}


def run(start: str, end: str, data_root: Path = DATA_ROOT) -> dict:
    audit = audit_inputs(data_root)
    return {
        "strategy": "smallcap-70-fixed-rule-causal-audit-v1",
        "source": {"url": SOURCE_URL, "commit": SOURCE_COMMIT, "blob_sha": SOURCE_BLOB_SHA, "sha256": SOURCE_SHA256},
        "period": {"start": start, "end": end}, "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True).encode()).hexdigest(),
        "execution": "prior-close signal, next-open T+1 fill", "input_audit": audit, "metrics": None,
        "hard_gate": {"thresholds": EXCELLENT_THRESHOLDS, "development_failures": audit["failures"]},
        "decision": "research_rejected", "replay_status": "not_run_input_contract_failed",
        "failures": audit["failures"],
        "limitations": ["总市值不得用流通市值替代", "缺失ST/停牌历史状态时fail-closed", "2002-2008未读取"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument("--output", type=Path, default=DATA_ROOT / "research" / "smallcap_70_dev_2011_2017.json")
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
