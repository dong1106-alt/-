#!/usr/bin/env python3
"""Fail-closed audit and causal rule model for the public industry reversal strategy."""
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
SOURCE_BLOB_SHA = "50158a06d2013610b4af19f9ec7ca6ad0c8c1713"
SOURCE_SHA256 = "cb39f0e494cbafd23a835ba51498b9fc2d30fc25dde2edbdb1c0a9404418fb4d"
SOURCE_URL = (
    "https://github.com/ShenzhenLime/factor_mining/blob/"
    f"{SOURCE_COMMIT}/ref/ref_code/124/0208/2024%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89"
    "%E7%AD%96%E7%95%A52/51.%E8%A1%8C%E4%B8%9A%E5%8F%8D%E8%BD%AC%E6%95%88%E5%BA%94"
    "%EF%BC%88%E5%B9%B4%E5%8C%9632%25%E5%9B%9E%E6%92%A48%25%29.py"
)
PARAMETERS = {
    "benchmark": "000300.XSHG",
    "fixed_slippage": 0.001,
    "open_tax": 0.0,
    "close_tax": 0.0,
    "open_commission": 0.0003,
    "close_commission": 0.0003,
    "minimum_commission": 5.0,
    "rolling_months": 36,
    "short_months": 3,
    "medium_months": 6,
    "selected_industries": 5,
    "stocks_per_unmapped_industry": 10,
    "industry_standard": "sw_l1",
    "stock_filters": "roe > 0 and pb_ratio > 0",
    "stock_rank": "ascending rank(pb_ratio) + ascending rank(1 / roe)",
    "industry_rank": "ascending bottom 5 after source descending sort then iloc[-5:]",
    "rebalance": "first trading day of month",
    "signal_cutoff": "previous trading day close",
    "fill": "next open, T+1",
    "allocation": "equal cash by selected industry; equal cash within stock basket",
    "existing_positions": "retain without target-value resizing while still selected",
}
DEPRECATED_INDUSTRIES = ("801060", "801070", "801090", "801100", "801190", "801220")
INSTRUMENT_POOL = {
    "交通运输I": None, "休闲服务I": None, "传媒I": "sh512980", "公用事业I": None,
    "农林牧渔I": "sz159825", "化工I": "sh516120", "医药生物I": "sz159929",
    "商业贸易I": None, "国防军工I": "sh512810", "家用电器I": None,
    "建筑材料I": "sz159944", "建筑装饰I": None, "房地产I": "sh512200",
    "有色金属I": "sh512400", "机械设备I": None, "汽车I": None,
    "煤炭I": "sh515220", "环保I": None, "电子I": "sz159997", "电气设备I": None,
    "石油石化I": None, "纺织服装I": None, "综合I": None, "美容护理I": None,
    "计算机I": "sh512720", "轻工制造I": None, "通信I": "sh515880",
    "钢铁I": "sh515210", "银行I": "sh512800", "非银金融I": "sz159931",
    "采掘I": None, "食品饮料I": "sz159843",
}
ETF_POOL = {industry: code for industry, code in INSTRUMENT_POOL.items() if code}


def _factor_coefficient(excess_returns: pd.DataFrame, months: int) -> pd.Series:
    rows = []
    for number in range(months, len(excess_returns)):
        previous = excess_returns.iloc[number - months:number]
        rows.append(previous.rdiv(excess_returns.iloc[number]).mean())
    if not rows:
        raise ValueError("insufficient monthly history")
    return pd.DataFrame(rows).mean()


def predict_industries(monthly_returns: pd.DataFrame, cutoff=None) -> pd.Series:
    """Reproduce the source ranking using only rows available by ``cutoff``."""
    data = monthly_returns.sort_index()
    if cutoff is not None:
        data = data.loc[:pd.Timestamp(cutoff)]
    rolling = PARAMETERS["rolling_months"]
    if len(data) < rolling:
        raise ValueError(f"at least {rolling} complete months are required")
    excess = data.tail(rolling) - data.tail(rolling).mean()
    short = _factor_coefficient(excess, PARAMETERS["short_months"])
    medium = _factor_coefficient(excess, PARAMETERS["medium_months"])
    prediction = (
        short * excess.tail(PARAMETERS["short_months"]).mean()
        + medium * excess.tail(PARAMETERS["medium_months"]).mean()
    ) / 2
    return prediction.sort_values(ascending=False).iloc[-PARAMETERS["selected_industries"]:]


def monthly_schedule(trading_days) -> list[dict[str, str]]:
    """Pair prior-close signals with the next month's first trading-day open."""
    days = sorted(pd.Timestamp(day) for day in trading_days)
    return [
        {"signal": str(previous)[:10], "fill": str(current)[:10]}
        for previous, current in zip(days, days[1:])
        if previous.to_period("M") != current.to_period("M")
    ]


def _load_manifest(path: Path) -> dict:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def audit_inputs(data_root: Path = DATA_ROOT) -> dict:
    industry = _load_manifest(data_root / "industries" / "manifest.json")
    fundamentals = _load_manifest(data_root / "fundamentals" / "manifest.json")
    industry_source = str(industry.get("source", ""))
    failures = []
    if "sw_l1" not in industry_source.lower() and "shenwan" not in industry_source.lower():
        failures.append(
            f"行业分类不兼容：策略要求申万一级，现有数据为{industry_source or '缺失'}"
        )
    if not industry.get("complete"):
        failures.append(f"行业数据覆盖{float(industry.get('coverage', 0)):.4%}，未达到100%")
    if not fundamentals.get("complete"):
        failures.append(
            f"PB/ROE基础数据覆盖{float(fundamentals.get('coverage', 0)):.4%}，未达到100%"
        )
    missing_etfs = sorted(code for code in ETF_POOL.values()
                          if not (data_root / "stocks" / f"{code}.parquet").exists())
    if missing_etfs:
        failures.append(f"缺少固定ETF历史日线：{','.join(missing_etfs)}")
    return {
        "industry_source": industry_source,
        "industry_coverage": float(industry.get("coverage", 0)),
        "fundamental_coverage": float(fundamentals.get("coverage", 0)),
        "missing_etfs": missing_etfs,
        "failures": failures,
    }


def run(start: str, end: str, data_root: Path = DATA_ROOT) -> dict:
    audit = audit_inputs(data_root)
    failures = list(audit["failures"])
    if not failures:
        failures.append("输入契约通过后才允许实现并运行真实资金回放")
    return {
        "strategy": "industry-reversal-fixed-rule-causal-audit-v1",
        "source": {
            "url": SOURCE_URL, "commit": SOURCE_COMMIT,
            "blob_sha": SOURCE_BLOB_SHA, "sha256": SOURCE_SHA256,
        },
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(
            json.dumps(PARAMETERS, ensure_ascii=False, sort_keys=True).encode()
        ).hexdigest(),
        "execution": "prior-close monthly signal, next-open T+1 fill",
        "input_audit": audit,
        "metrics": None,
        "hard_gate": {"thresholds": EXCELLENT_THRESHOLDS, "development_failures": failures},
        "decision": "research_rejected",
        "replay_status": "not_run_input_contract_failed",
        "failures": failures,
        "limitations": [
            "No CSRC-to-Shenwan substitution is made because it would change the strategy",
            "No stock basket substitutes for missing fixed ETFs are fabricated",
            "2002-2008 unseen data is not read",
            "Incomplete inputs are forbidden from shadow or release stages",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "industry_reversal_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
