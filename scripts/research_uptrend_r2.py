#!/usr/bin/env python3
"""Causal replay of the public weekly rising-trend strategy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS, calculate, excellent_failures
from point_in_time_universe import load_universe
from research_smallcap_breadth import _bar, _load_daily, _tradable
from research_trend_risk_v5 import _prepare_features
from research_trend_v5 import _load_memberships
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
SOURCE_COMMIT = "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
SOURCE_BLOB_SHA = "6d4a021b382e35f0e50452181e8ed677934d472b"
SOURCE_SHA256 = "e019d6f363ad7bae907350e9b0bec0952e72b044038ee5472f803f41aa2a8525"
SOURCE_URL = (
    "https://github.com/ShenzhenLime/factor_mining/blob/"
    f"{SOURCE_COMMIT}/ref/ref_code/124/0208/2023%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89"
    "%E7%AD%96%E7%95%A5/14.2020%E5%B9%B4%20267%25%E5%B9%B4%E5%8C%96%2013%25%E5%9B%9E"
    "%E6%92%A4%20%E4%B8%8A%E6%B6%A8%E8%B6%8B%E5%8A%BF%E7%AD%96%E7%95%A5%E4%BF%AE"
    "%E6%94%B9%E7%89%88.py"
)
PARAMETERS = {
    "universe": "historical HS300",
    "regression_window": 120,
    "minimum_r_squared": 0.8,
    "minimum_slope_intercept": 0.005,
    "maximum_close": 500.0,
    "high_window": 30,
    "maximum_high_to_close": 1.1,
    "volume_short_window": 7,
    "volume_long_window": 180,
    "maximum_volume_ratio": 1.5,
    "stock_num": 2,
    "rank": "descending linear-regression R-squared",
    "rebalance": "first trading day of week open",
    "signal_cutoff": "previous trading day close",
    "fill": "next open, T+1",
    "fixed_slippage": 0.02,
    "open_commission": 0.0003,
    "close_commission": 0.0003,
    "close_tax": 0.001,
    "minimum_commission": 5.0,
}
PHASES = (
    ("2011-2013", "2011-01-04", "2013-12-31"),
    ("2014-2015", "2014-01-02", "2015-12-31"),
    ("2016-2017", "2016-01-04", "2017-12-29"),
)


def _rank_targets(day: pd.Timestamp, membership: set[str], features: dict) -> list[str]:
    ranked = []
    for code in membership:
        frame = features.get(code)
        row = _bar(frame, day) if frame is not None else None
        if row is None:
            continue
        correlation = float(row["correlation"])
        if (
            pd.isna(correlation)
            or float(row["close"]) > PARAMETERS["maximum_close"]
            or float(row["high_to_close"]) > PARAMETERS["maximum_high_to_close"]
            or float(row["volume_ratio"]) > PARAMETERS["maximum_volume_ratio"]
            or float(row["slope_intercept"]) <= PARAMETERS["minimum_slope_intercept"]
            or correlation * correlation <= PARAMETERS["minimum_r_squared"]
        ):
            continue
        ranked.append((code, correlation * correlation))
    ranked.sort(key=lambda item: (-item[1], item[0]))
    return [code for code, _ in ranked[:PARAMETERS["stock_num"]]]


def _phase_metrics(equity_history: list[dict], close_dates: list[str]) -> dict:
    return {
        name: calculate(
            [row for row in equity_history if start <= row["date"] <= end],
            sum(start <= day <= end for day in close_dates),
        )
        for name, start, end in PHASES
    }


def run(start: str, end: str) -> dict:
    universe, universe_meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end]
    if not universe_meta.get("complete") or len(days) < 180:
        raise RuntimeError("complete historical trading calendar is required")
    memberships, membership_manifest = _load_memberships(days)
    codes = sorted(set().union(*memberships.values()))
    bars = _load_daily(codes, start, end)
    features = {code: _prepare_features(frame) for code, frame in bars.items()}
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, int] = {}
    last_close: dict[str, float] = {}
    pending: list[str] | None = None
    equity_history = []
    close_dates = []
    total_fees = 0.0
    transactions = 0
    minimum_cash = cash
    maximum_positions = 0
    rebalance_count = 0
    eligible_counts = []

    for number, day in enumerate(days):
        if pending is not None:
            open_equity = cash + sum(
                shares * (
                    float(row["open"]) if (row := _bar(bars.get(code), day)) is not None
                    and float(row["open"]) > 0 else last_close.get(code, 0.0)
                )
                for code, shares in positions.items()
            )
            slot_value = open_equity / len(pending) if pending else 0.0
            desired = {}
            for code in pending:
                row = _bar(bars.get(code), day) if code in bars else None
                if row is not None and float(row["open"]) > 0:
                    desired[code] = int(slot_value / float(row["open"]) // 100) * 100

            for code, current in list(positions.items()):
                target = desired.get(code, 0)
                shares = current - target
                row = _bar(bars.get(code), day) if code in bars else None
                if shares < 100 or not _tradable(row, last_close.get(code, 0.0), "sell"):
                    continue
                _, proceeds, commission, tax = calc_trade_cost(
                    float(row["open"]), shares, "sell", trade_cfg,
                )
                cash += proceeds
                total_fees += commission + tax
                transactions += 1
                if target:
                    positions[code] = target
                else:
                    positions.pop(code)
                    close_dates.append(str(day)[:10])

            for code, target in desired.items():
                shares = target - positions.get(code, 0)
                row = _bar(bars.get(code), day) if code in bars else None
                if shares < 100 or not _tradable(row, last_close.get(code, 0.0), "buy"):
                    continue
                while shares >= 100:
                    _, cost, commission, tax = calc_trade_cost(
                        float(row["open"]), shares, "buy", trade_cfg,
                    )
                    if cost <= cash:
                        break
                    shares -= 100
                if shares >= 100:
                    cash -= cost
                    total_fees += commission + tax
                    transactions += 1
                    positions[code] = positions.get(code, 0) + shares
            rebalance_count += 1
            pending = None

        if cash < -0.01 or any(shares <= 0 or shares % 100 for shares in positions.values()):
            raise RuntimeError("portfolio accounting invariant failed")
        minimum_cash = min(minimum_cash, cash)
        maximum_positions = max(maximum_positions, len(positions))
        value = cash
        for code, shares in positions.items():
            row = _bar(bars.get(code), day) if code in bars else None
            price = float(row["close"]) if row is not None and float(row["close"]) > 0 else last_close.get(code, 0.0)
            value += shares * price
        equity_history.append({"date": str(day)[:10], "equity": round(value, 2)})
        for code, frame in bars.items():
            row = _bar(frame, day)
            if row is not None and float(row["close"]) > 0:
                last_close[code] = float(row["close"])

        next_day = days[number + 1] if number + 1 < len(days) else None
        if next_day is None or next_day.isocalendar()[:2] == day.isocalendar()[:2]:
            continue
        target = _rank_targets(day, memberships[str(day)[:10]], features)
        eligible_counts.append(len(target))
        pending = target

    metrics = calculate(equity_history, len(close_dates))
    phases = _phase_metrics(equity_history, close_dates)
    failures = excellent_failures(metrics, require_deviation=False)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    return {
        "strategy": "uptrend-r2-fixed-rule-causal-replay-v1",
        "source": {
            "url": SOURCE_URL, "commit": SOURCE_COMMIT,
            "blob_sha": SOURCE_BLOB_SHA, "sha256": SOURCE_SHA256,
        },
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True).encode()).hexdigest(),
        "execution": "prior-close weekly signal, next-open fills, A-share lots/limits/fees/slippage",
        "metrics": metrics,
        "phase_metrics": phases,
        "total_return_pct": round((equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3),
        "total_fees": round(total_fees, 2),
        "transactions": transactions,
        "rebalance_count": rebalance_count,
        "minimum_cash": round(minimum_cash, 2),
        "maximum_positions": maximum_positions,
        "average_selected_count": round(sum(eligible_counts) / len(eligible_counts), 2),
        "input_manifest": membership_manifest,
        "hard_gate": {"thresholds": EXCELLENT_THRESHOLDS, "development_failures": failures},
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "Historical ST identity is unavailable; conservative 5% fill limits are used",
            "The source's active risk-management callbacks are commented out and remain inactive",
            "2011-2017 is consumed development data; 2002-2008 unseen data is not read",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "uptrend_r2_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
