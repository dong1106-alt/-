#!/usr/bin/env python3
"""Causal replay of the public fixed-rule Trend Risk Enhanced V5.0 strategy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS, calculate, excellent_failures
from point_in_time_universe import load_universe
from research_smallcap_breadth import _bar, _load_daily
from research_trend_v5 import _load_memberships
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
INDEX_PATH = DATA_ROOT / "index" / "sh000001.parquet"
SOURCE_COMMIT = "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
SOURCE_URL = (
    "https://github.com/ShenzhenLime/factor_mining/blob/"
    f"{SOURCE_COMMIT}/ref/ref_code/124/0208/2023%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89"
    "%E7%AD%96%E7%95%A5/40.%E5%86%8D%E6%94%B9%E8%BF%9B%E5%8F%AF%E5%AE%9E%E7%9B%98-"
    "%E9%BB%98%E9%BB%98%E8%B5%9A%E9%92%B1%E7%B3%BB%E5%88%97-%E9%A3%8E%E9%99%A9"
    "%E6%8E%A7%E5%88%B6-%E5%A2%9E%E5%BC%BA%E7%89%88%E6%9C%AC-V5.0.py"
)
PARAMETERS = {
    "maximum_close": 500.0,
    "high_window": 30,
    "maximum_high_to_close": 1.1,
    "volume_short_window": 7,
    "volume_long_window": 180,
    "maximum_volume_ratio": 1.5,
    "regression_window": 120,
    "minimum_slope_intercept": 0.005,
    "minimum_correlation": 0.9,
    "stock_num": 2,
    "index": "sh000001",
    "index_ma_window": 10,
    "regime_threshold": 0.005,
    "initial_is_bull": False,
    "rebalance": "first trading day of week open",
}
PHASES = (
    ("2011-2013", "2011-01-04", "2013-12-31"),
    ("2014-2015", "2014-01-02", "2015-12-31"),
    ("2016-2017", "2016-01-04", "2017-12-29"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _prepare_features(frame: pd.DataFrame) -> pd.DataFrame:
    active = frame[pd.to_numeric(frame["trade_status"], errors="coerce") == 1].copy()
    active = active.dropna(subset=["close", "high", "volume"]).sort_index()
    close = active["close"].astype(float)
    high = active["high"].astype(float)
    volume = active["volume"].astype(float)
    n = PARAMETERS["regression_window"]
    x = np.arange(n, dtype=float)
    sum_x = x.sum()
    sum_x2 = np.square(x).sum()
    y = close.to_numpy()
    sum_y = close.rolling(n).sum().to_numpy()
    sum_y2 = close.pow(2).rolling(n).sum().to_numpy()
    sum_xy = np.full(len(active), np.nan)
    if len(active) >= n:
        sum_xy[n - 1:] = np.correlate(y, x, mode="valid")
    covariance = n * sum_xy - sum_x * sum_y
    slope = covariance / (n * sum_x2 - sum_x ** 2)
    intercept = (sum_y - slope * sum_x) / n
    denominator = np.sqrt((n * sum_x2 - sum_x ** 2) * (n * sum_y2 - sum_y ** 2))
    correlation = np.divide(
        covariance, denominator,
        out=np.full(len(active), np.nan), where=denominator > 0,
    )
    output = pd.DataFrame(index=active.index)
    output["close"] = close
    output["slope"] = slope
    output["intercept"] = intercept
    output["correlation"] = correlation
    output["slope_intercept"] = slope / intercept
    output["high_to_close"] = high.rolling(PARAMETERS["high_window"]).max() / close
    output["volume_ratio"] = (
        volume.rolling(PARAMETERS["volume_short_window"]).mean()
        / volume.rolling(PARAMETERS["volume_long_window"]).mean()
    )
    output["eligible"] = (
        (output["close"] <= PARAMETERS["maximum_close"])
        & (output["high_to_close"] <= PARAMETERS["maximum_high_to_close"])
        & (output["volume_ratio"] <= PARAMETERS["maximum_volume_ratio"])
        & (output["slope_intercept"] > PARAMETERS["minimum_slope_intercept"])
        & (output["correlation"] > PARAMETERS["minimum_correlation"])
    )
    return output


def _update_bull_state(is_bull: bool, closes: pd.Series) -> bool:
    values = pd.to_numeric(closes, errors="coerce").dropna().tail(PARAMETERS["index_ma_window"])
    if len(values) < PARAMETERS["index_ma_window"]:
        raise RuntimeError("complete index moving-average history is required")
    current = float(values.iloc[-1])
    mean = float(values.mean())
    threshold = PARAMETERS["regime_threshold"]
    if is_bull and current * (1 + threshold) <= mean:
        return False
    if not is_bull and current > mean * (1 + threshold):
        return True
    return is_bull


def _tradable(row, previous_close: float, side: str) -> bool:
    if row is None or float(row.get("open", 0)) <= 0 or float(row.get("volume", 0)) <= 0:
        return False
    if int(row.get("trade_status", 1)) != 1 or previous_close <= 0:
        return False
    ratio = float(row["open"]) / previous_close
    return ratio < 1.048 if side == "buy" else ratio > 0.952


def _phase_metrics(equity_history: list[dict], close_dates: list[str]) -> dict:
    return {
        name: calculate(
            [row for row in equity_history if start <= row["date"] <= end],
            sum(start <= day <= end for day in close_dates),
        )
        for name, start, end in PHASES
    }


def _load_index(days: list[pd.Timestamp]) -> tuple[pd.DataFrame, dict]:
    index = pd.read_parquet(INDEX_PATH)
    index["date"] = pd.to_datetime(index["date"])
    index = index.sort_values("date").set_index("date")
    if any(day not in index.index for day in days):
        raise RuntimeError("SSE index coverage mismatch")
    return index, {
        "path": str(INDEX_PATH.relative_to(DATA_ROOT)),
        "sha256": _sha256(INDEX_PATH),
        "start": str(index.index.min())[:10],
        "end": str(index.index.max())[:10],
    }


def run(start: str, end: str) -> dict:
    universe, universe_meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end]
    if not universe_meta.get("complete") or len(days) < 180:
        raise RuntimeError("complete historical trading calendar is required")
    memberships, membership_manifest = _load_memberships(days)
    index, index_manifest = _load_index(days)
    codes = sorted(set().union(*memberships.values()))
    bars = _load_daily(codes, start, end)
    features = {code: _prepare_features(frame) for code, frame in bars.items()}
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, int] = {}
    last_close: dict[str, float] = {}
    pending: dict | None = None
    equity_history = []
    close_dates = []
    total_fees = 0.0
    transactions = 0
    minimum_cash = cash
    maximum_positions = 0
    eligible_counts = []
    rebalance_count = 0
    bull_signals = 0
    is_bull = PARAMETERS["initial_is_bull"]

    for number, day in enumerate(days):
        if pending is not None:
            target = pending["target"]
            for code, shares in list(positions.items()):
                if code in target:
                    continue
                row = _bar(bars.get(code), day) if code in bars else None
                if not _tradable(row, last_close.get(code, 0.0), "sell"):
                    continue
                _, proceeds, commission, tax = calc_trade_cost(float(row["open"]), shares, "sell", trade_cfg)
                cash += proceeds
                total_fees += commission + tax
                transactions += 1
                positions.pop(code)
                close_dates.append(str(day)[:10])

            if not pending["is_bull"] and target:
                open_equity = cash + sum(
                    shares * (
                        float(row["open"]) if (row := _bar(bars.get(code), day)) is not None
                        else last_close.get(code, 0.0)
                    )
                    for code, shares in positions.items()
                )
                slot_value = open_equity / len(target)
                desired = {}
                for code in target:
                    row = _bar(bars.get(code), day) if code in bars else None
                    if row is not None and float(row["open"]) > 0:
                        desired[code] = int(slot_value / float(row["open"]) // 100) * 100

                for code in target:
                    if code not in positions:
                        continue
                    row = _bar(bars.get(code), day)
                    current = positions[code]
                    target_shares = desired.get(code, current)
                    sell_shares = current - target_shares
                    if sell_shares < 100 or not _tradable(row, last_close.get(code, 0.0), "sell"):
                        continue
                    _, proceeds, commission, tax = calc_trade_cost(float(row["open"]), sell_shares, "sell", trade_cfg)
                    cash += proceeds
                    total_fees += commission + tax
                    transactions += 1
                    if target_shares:
                        positions[code] = target_shares
                    else:
                        positions.pop(code)
                        close_dates.append(str(day)[:10])

                for code in target:
                    row = _bar(bars.get(code), day) if code in bars else None
                    previous_close = float(features[code].loc[: day - pd.Timedelta(days=1), "close"].iloc[-1])
                    buy_shares = desired.get(code, 0) - positions.get(code, 0)
                    if buy_shares < 100 or not _tradable(row, previous_close, "buy"):
                        continue
                    while buy_shares >= 100:
                        _, cost, commission, tax = calc_trade_cost(float(row["open"]), buy_shares, "buy", trade_cfg)
                        if cost <= cash:
                            break
                        buy_shares -= 100
                    if buy_shares >= 100:
                        cash -= cost
                        total_fees += commission + tax
                        transactions += 1
                        positions[code] = positions.get(code, 0) + buy_shares
            rebalance_count += 1
            pending = None

        if cash < -0.01 or any(shares <= 0 or shares % 100 for shares in positions.values()):
            raise RuntimeError("portfolio accounting invariant failed")
        minimum_cash = min(minimum_cash, cash)
        maximum_positions = max(maximum_positions, len(positions))

        value = cash
        for code, shares in positions.items():
            row = _bar(bars.get(code), day) if code in bars else None
            if row is not None:
                last_close[code] = float(row["close"])
            value += shares * last_close.get(code, 0.0)
        equity_history.append({"date": str(day)[:10], "equity": round(value, 2)})

        next_day = days[number + 1] if number + 1 < len(days) else None
        if next_day is None or next_day.isocalendar()[:2] == day.isocalendar()[:2]:
            continue
        ranked = []
        for code in memberships[str(next_day)[:10]]:
            if code.startswith("688"):
                continue
            frame = features.get(code)
            if frame is None:
                continue
            row = _bar(frame, day)
            if row is not None and bool(row["eligible"]):
                ranked.append((code, float(row["slope"])))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        target = [code for code, _ in ranked[:PARAMETERS["stock_num"]]]
        eligible_counts.append(len(ranked))
        is_bull = _update_bull_state(is_bull, index.loc[:day, "close"])
        bull_signals += int(is_bull)
        pending = {"target": target, "is_bull": is_bull}

    metrics = calculate(equity_history, len(close_dates))
    phases = _phase_metrics(equity_history, close_dates)
    failures = excellent_failures(metrics, require_deviation=False)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    return {
        "strategy": "trend-risk-enhanced-v5-fixed-rule-causal-replay-v1",
        "source": {"url": SOURCE_URL, "commit": SOURCE_COMMIT},
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True).encode()).hexdigest(),
        "execution": "prior-close weekly signal and regime state, next-open fills, A-share lots/limits/fees/slippage",
        "metrics": metrics,
        "phase_metrics": phases,
        "total_return_pct": round((equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3),
        "total_fees": round(total_fees, 2),
        "transactions": transactions,
        "rebalance_count": rebalance_count,
        "bull_signals": bull_signals,
        "minimum_cash": round(minimum_cash, 2),
        "maximum_positions": maximum_positions,
        "average_eligible_count": round(sum(eligible_counts) / len(eligible_counts), 1),
        "input_manifest": {"memberships": membership_manifest, "index": index_manifest},
        "hard_gate": {"thresholds": EXCELLENT_THRESHOLDS, "development_failures": failures},
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "The source's incomplete same-day index bar is causally corrected to prior-close state and next-open fills",
            "The source intentionally suppresses new buys when is_bull is true; that behavior is preserved",
            "2011-2017 is consumed development data and cannot be release evidence",
            "historical ST identity is unavailable; conservative 5% fill limits are used",
            "price-based market-cap fallback is used only for the shared slippage schedule",
            "2002-2008 unseen data is not read by this development replay",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "trend_risk_v5_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
