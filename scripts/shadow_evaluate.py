#!/usr/bin/env python3
"""Evaluate a candidate shadow account against its paired baseline."""
from __future__ import annotations

import datetime as dt
import json
import math
import os
import statistics
from pathlib import Path

from candidate_engine import MAX_DRAWDOWN_PCT, MIN_SHADOW_CLOSED_TRADES, write_json

ROOT = Path(__file__).resolve().parent.parent
SHADOW = Path(os.environ.get("SHADOW_ROOT", str(ROOT / "data" / "shadow")))
PAIRED = Path(os.environ["PAIRED_BASELINE_ROOT"]) if os.environ.get("PAIRED_BASELINE_ROOT") else None


def closed_trades(portfolio: dict, baseline_count: int) -> int:
    return sum(1 for trade in portfolio.get("trade_history", [])[baseline_count:]
               if trade.get("action") == "SELL")


def equity_sharpe(portfolio: dict) -> float:
    values = [float(row["total_value"]) for row in portfolio.get("equity_history", [])
              if float(row.get("total_value", 0)) > 0]
    returns = [values[i] / values[i - 1] - 1 for i in range(1, len(values))]
    if len(returns) < 20:
        return 0.0
    deviation = statistics.stdev(returns)
    return statistics.mean(returns) / deviation * math.sqrt(250) if deviation > 0 else 0.0


def _sharpe_pass(candidate: float, baseline: float) -> bool:
    return candidate >= baseline * 1.1 if baseline > 0 else candidate >= baseline + 0.1


def compare_pair(candidate: dict, baseline: dict, candidate_start_count: int,
                 baseline_start_count: int, candidate_start_value: float,
                 baseline_start_value: float) -> dict:
    candidate_dates = [row.get("date") for row in candidate.get("equity_history", [])]
    baseline_dates = [row.get("date") for row in baseline.get("equity_history", [])]
    candidate_closed = closed_trades(candidate, candidate_start_count)
    baseline_closed = closed_trades(baseline, baseline_start_count)
    candidate_delta = float(candidate.get("total_value", 0)) - candidate_start_value
    baseline_delta = float(baseline.get("total_value", 0)) - baseline_start_value
    candidate_dd = abs(float(candidate.get("max_drawdown", 0) or 0))
    baseline_dd = abs(float(baseline.get("max_drawdown", 0) or 0))
    candidate_sharpe = equity_sharpe(candidate)
    baseline_sharpe = equity_sharpe(baseline)
    failures = []
    if candidate_dates != baseline_dates or len(candidate_dates) != len(set(candidate_dates)):
        failures.append("候选与基准权益日期不一致")
    if candidate_closed < MIN_SHADOW_CLOSED_TRADES:
        failures.append(f"候选平仓{candidate_closed}笔，少于{MIN_SHADOW_CLOSED_TRADES}笔")
    if len(candidate.get("equity_history", [])) < 21:
        failures.append("候选权益历史少于21个交易日")
    if candidate_delta < baseline_delta:
        failures.append("候选收益低于配对基准")
    if not _sharpe_pass(candidate_sharpe, baseline_sharpe):
        failures.append("候选夏普未较配对基准提升10%")
    if candidate_dd > MAX_DRAWDOWN_PCT:
        failures.append(f"候选回撤{candidate_dd:.2f}%超过{MAX_DRAWDOWN_PCT:.0f}%")
    if candidate_dd > baseline_dd:
        failures.append("候选回撤劣于配对基准")
    return {
        "passed": not failures,
        "failures": failures,
        "candidate_closed_trades": candidate_closed,
        "baseline_closed_trades": baseline_closed,
        "candidate_delta": round(candidate_delta, 2),
        "baseline_delta": round(baseline_delta, 2),
        "candidate_sharpe": round(candidate_sharpe, 3),
        "baseline_sharpe": round(baseline_sharpe, 3),
        "candidate_drawdown": candidate_dd,
        "baseline_drawdown": baseline_dd,
    }


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    ds = dt.date.today().isoformat()
    portfolio_path = SHADOW / "sim_trades" / "portfolio.json"
    meta_path = SHADOW / "shadow_meta.json"
    if not portfolio_path.exists() or not meta_path.exists():
        print("[shadow_evaluate] 候选影子数据不完整")
        return 1
    candidate = _read(portfolio_path)
    meta = _read(meta_path)
    candidate_id = meta.get("candidate_id", "v6-risk-baseline")

    pair = None
    if candidate_id != "v6-risk-baseline" and PAIRED:
        pair_portfolio = PAIRED / "sim_trades" / "portfolio.json"
        pair_meta = PAIRED / "shadow_meta.json"
        if pair_portfolio.exists() and pair_meta.exists():
            baseline = _read(pair_portfolio)
            baseline_meta = _read(pair_meta)
            same_start = (bool(meta.get("initial_portfolio_hash"))
                          and meta.get("initial_portfolio_hash") == baseline_meta.get("initial_portfolio_hash")
                          and float(meta.get("baseline_main_total_value", 0))
                          == float(baseline_meta.get("baseline_main_total_value", -1)))
            if same_start:
                pair = compare_pair(
                    candidate, baseline,
                    int(meta.get("baseline_trade_count", 0)),
                    int(baseline_meta.get("baseline_trade_count", 0)),
                    float(meta.get("baseline_main_total_value", 0)),
                    float(baseline_meta.get("baseline_main_total_value", 0)),
                )

    if candidate_id == "v6-risk-baseline":
        decision = "基准影子持续观察"
    elif pair is None:
        decision = "拒绝：缺少同起点配对基准"
    elif pair["passed"]:
        decision = "达到配对影子门槛：允许进入封存集发布检查"
    else:
        decision = "待观察/拒绝：" + "；".join(pair["failures"])

    result = {
        "date": ds, "strategy": "v6-shadow", "candidate_id": candidate_id,
        "decision": decision, "paired_baseline": pair,
        "main_params_unchanged": True,
    }
    out_json = SHADOW / "evaluation" / f"candidate_{ds}.json"
    out_txt = SHADOW / "reports" / f"候选评估_{ds}.txt"
    write_json(out_json, result)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    out_txt.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    if pair and pair["passed"]:
        active = ROOT / "data" / "candidates" / "active_shadow.json"
        active_payload = _read(active) if active.exists() else {}
        result["candidate_signature"] = active_payload.get("candidate_meta", {}).get("evaluation", {}).get("candidate_signature")
        write_json(ROOT / "data" / "candidates" / "promotion_recommendation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
