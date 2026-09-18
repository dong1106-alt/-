#!/usr/bin/env python3
"""Causal replay of the public 91-day contrarian momentum strategy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS, calculate, excellent_failures
from point_in_time_universe import load_history_manifest, load_universe
from research_smallcap_breadth import _bar, _load_daily, _tradable
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
SOURCE_COMMIT = "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
SOURCE_BLOB_SHA = "e7eb3b2140589450dba08e03ed5e4182400e7cd3"
SOURCE_SHA256 = "f7a148bca1e8cffa1ea297d22f36bb6d60a1eddb20230165bd167d1dc9cd9644"
SOURCE_URL = (
    "https://github.com/ShenzhenLime/factor_mining/blob/"
    f"{SOURCE_COMMIT}/ref/ref_code/124/0208/2024%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89"
    "%E7%AD%96%E7%95%A51/49.%E5%B9%B4%E5%8C%9662%25%E7%9A%84%E5%8A%A8%E9%87%8F"
    "%E7%AD%96%E7%95%A5.py"
)
PARAMETERS = {
    "universe": "historical A-share main board (60/00 prefixes)",
    "minimum_listing_calendar_days": 250,
    "momentum_observations": 91,
    "minimum_close": 5.0,
    "stock_num": 10,
    "rank": "ascending 91-observation close return (source min_dict rule)",
    "rebalance": "daily open",
    "signal_cutoff": "previous trading day close",
    "fill": "next open, T+1",
    "allocation": "sell removed names, split available cash across new names",
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


def _prepare_features(bars: dict[str, pd.DataFrame]) -> tuple[pd.DataFrame, pd.DataFrame]:
    closes = {}
    momentums = {}
    for code, frame in bars.items():
        valid = frame[
            (pd.to_numeric(frame["close"], errors="coerce") > 0)
            & (pd.to_numeric(frame["trade_status"], errors="coerce").fillna(1) == 1)
        ]
        closes[code] = valid["close"]
        momentums[code] = valid["close"] / valid["close"].shift(
            PARAMETERS["momentum_observations"] - 1
        ) - 1.0
    return pd.DataFrame(closes), pd.DataFrame(momentums)


def _rank_targets(day: pd.Timestamp, membership: set[str], closes: pd.DataFrame,
                  momentums: pd.DataFrame, listed_since: dict[str, pd.Timestamp]) -> list[str]:
    if day not in momentums.index:
        return []
    scores = momentums.loc[day].dropna()
    prices = closes.loc[day] if day in closes.index else pd.Series(dtype=float)
    eligible = [
        code for code in scores.index
        if code in membership
        and code.startswith(("sh60", "sz00"))
        and code in listed_since
        and (day - listed_since[code]).days >= PARAMETERS["minimum_listing_calendar_days"]
        and float(prices.get(code, float("nan"))) >= PARAMETERS["minimum_close"]
    ]
    ranked = sorted(eligible, key=lambda code: (float(scores[code]), code))
    return ranked[:PARAMETERS["stock_num"]]


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
    if not universe_meta.get("complete") or len(days) < 120:
        raise RuntimeError("complete point-in-time universe is required")
    history_manifest = load_history_manifest(
        str(pd.Timestamp(start) - pd.Timedelta(days=240))[:10], end,
        universe_meta.get("sha256", ""), verify_files=False, universe_by_date=universe,
    )
    if not history_manifest.get("complete"):
        raise RuntimeError(history_manifest.get("reason", "stock history is incomplete"))

    listed_since = {}
    for date, members in universe.items():
        day = pd.Timestamp(date)
        for code in members:
            listed_since.setdefault(code, day)
    codes = sorted(set().union(*(universe[str(day)[:10]] for day in days)))
    bars = _load_daily(codes, start, end)
    closes, momentums = _prepare_features(bars)
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

    for number, day in enumerate(days):
        if pending is not None:
            target = set(pending)
            for code in list(positions):
                if code in target:
                    continue
                row = _bar(bars.get(code), day) if code in bars else None
                if not _tradable(row, last_close.get(code, 0.0), "sell"):
                    continue
                shares = positions.pop(code)
                _, proceeds, commission, tax = calc_trade_cost(
                    float(row["open"]), shares, "sell", trade_cfg,
                )
                cash += proceeds
                total_fees += commission + tax
                transactions += 1
                close_dates.append(str(day)[:10])

            new_targets = [code for code in pending if code not in positions]
            order_value = cash / len(new_targets) if new_targets else 0.0
            for code in new_targets:
                row = _bar(bars.get(code), day) if code in bars else None
                if not _tradable(row, last_close.get(code, 0.0), "buy"):
                    continue
                shares = int(order_value / float(row["open"]) // 100) * 100
                while shares >= 100:
                    _, cost, commission, tax = calc_trade_cost(
                        float(row["open"]), shares, "buy", trade_cfg,
                    )
                    if cost <= cash:
                        break
                    shares -= 100
                if shares >= 100:
                    cash -= cost
                    positions[code] = shares
                    total_fees += commission + tax
                    transactions += 1
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

        if day in closes.index:
            last_close.update(closes.loc[day].dropna().astype(float).to_dict())
        if number + 1 < len(days):
            pending = _rank_targets(
                day, universe[str(day)[:10]], closes, momentums, listed_since,
            )

    metrics = calculate(equity_history, len(close_dates))
    phases = _phase_metrics(equity_history, close_dates)
    failures = excellent_failures(metrics, require_deviation=False)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    return {
        "strategy": "contrarian-momentum-fixed-rule-causal-replay-v1",
        "source": {
            "url": SOURCE_URL, "commit": SOURCE_COMMIT,
            "blob_sha": SOURCE_BLOB_SHA, "sha256": SOURCE_SHA256,
        },
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True).encode()).hexdigest(),
        "execution": "prior-close daily signal, next-open fills, A-share lots/limits/fees/slippage",
        "metrics": metrics,
        "phase_metrics": phases,
        "total_return_pct": round((equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3),
        "total_fees": round(total_fees, 2),
        "transactions": transactions,
        "rebalance_count": rebalance_count,
        "minimum_cash": round(minimum_cash, 2),
        "maximum_positions": maximum_positions,
        "input_manifest": {
            key: history_manifest.get(key) for key in (
                "source", "start", "end", "coverage", "complete",
                "expected_codes", "complete_codes", "manifest_sha256",
            )
        },
        "hard_gate": {"thresholds": EXCELLENT_THRESHOLDS, "development_failures": failures},
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "The source's all-securities call is corrected to the point-in-time listed universe",
            "Historical ST identity is unavailable; conservative 5% fill limits are used",
            "The source's fund fee declaration is replaced by actual A-share fees and slippage",
            "2011-2017 is consumed development data; 2002-2008 unseen data is not read",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "contrarian_momentum_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
