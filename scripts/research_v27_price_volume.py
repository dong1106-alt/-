#!/usr/bin/env python3
"""Causal replay of GitHub v27 price-volume resonance."""
from __future__ import annotations

import argparse
import json
import math
import os
from collections import Counter
from pathlib import Path

import pandas as pd

from performance_metrics import calculate, excellent_failures
from point_in_time_universe import load_universe
from research_smallcap_breadth import _bar, _load_daily, _load_liquidity, _tradable
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
PARAMETERS = {
    "momentum_threshold": 0.05,
    "pv_corr_10_min": -0.5,
    "stop_loss": -0.015,
    "take_profit": 0.03,
    "maximum_hold_days": 5,
    "maximum_holdings": 8,
    "maximum_daily_buys": 4,
    "minimum_history_days": 120,
    "minimum_average_value20": 20_000_000,
}
PHASES = (
    ("2011-2013", "2011-01-04", "2013-12-31"),
    ("2014-2015", "2014-01-02", "2015-12-31"),
    ("2016-2017", "2016-01-04", "2017-12-29"),
)


def _add_features(frame: pd.DataFrame) -> pd.DataFrame:
    output = frame.copy()
    returns = output["close"].pct_change()
    volume5 = output["volume"].rolling(5).mean()
    volume_ratio = volume5 / output["volume"].rolling(20).mean()

    def correlation(window: int) -> pd.Series:
        mean_return = returns.rolling(window).mean()
        mean_volume = volume_ratio.rolling(window).mean()
        covariance = (
            (returns - mean_return) * (volume_ratio - mean_volume)
        ).rolling(window).mean()
        return covariance / (
            returns.rolling(window).std() * volume_ratio.rolling(window).std()
        )

    output["momentum5"] = output["close"].pct_change(5)
    output["gap"] = output["open"] / output["close"].shift(1) - 1
    output["pv_corr10"] = correlation(10)
    output["pv_corr20"] = correlation(20)
    output["price_level"] = output["close"].rolling(20).mean()
    output["price_trend"] = output["close"].pct_change(20)
    output["volume_shrink"] = volume_ratio
    output["volatility_abnormal"] = (
        returns.rolling(5).std() / returns.rolling(60).std()
    )
    output["bollinger_width"] = (
        4 * output["close"].rolling(20).std() / output["price_level"]
    )
    return output


def _finite(value) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _rank(rows: list[dict]) -> list[str]:
    if not rows:
        return []
    frame = pd.DataFrame(rows).set_index("code")
    risk_parts = []
    for column, sign in (
        ("price_level", -1), ("price_trend", -1),
        ("volume_shrink", -1), ("volatility_abnormal", 1),
    ):
        values = pd.to_numeric(frame[column], errors="coerce")
        deviation = values.std()
        risk_parts.append(sign * (values - values.mean()) / deviation if deviation > 0 else values * 0)
    frame["delist_risk"] = pd.concat(risk_parts, axis=1).mean(axis=1)
    risk_limit = frame["delist_risk"].quantile(0.9)
    frame = frame[
        (frame["momentum5"] > PARAMETERS["momentum_threshold"])
        & (frame["pv_corr10"] >= PARAMETERS["pv_corr_10_min"])
        & (frame["delist_risk"] <= risk_limit)
    ].copy()
    if frame.empty:
        return []
    frame["score"] = (
        frame["momentum5"] * 100
        + (frame["pv_corr20"] > 0).astype(float) * 0.5
        + (frame["gap"] > 0.02).astype(float) * 0.5
        + 0.8
        + (frame["bollinger_width"] > 1.2).astype(float) * 0.3
    )
    return list(frame["score"].sort_values(ascending=False, kind="stable").index)


def _intraday_exit(row, entry_price: float) -> tuple[float, str] | None:
    stop = entry_price * (1 + PARAMETERS["stop_loss"])
    take = entry_price * (1 + PARAMETERS["take_profit"])
    open_price = float(row["open"])
    if open_price <= stop:
        return open_price, "stop_gap"
    if float(row["low"]) <= stop:
        return stop, "stop"
    if open_price >= take:
        return open_price, "take_gap"
    if float(row["high"]) >= take:
        return take, "take"
    return None


def _phase_metrics(equity_history: list[dict], trades: list[str]) -> dict:
    output = {}
    for name, start, end in PHASES:
        equity = [row for row in equity_history if start <= row["date"] <= end]
        closed = sum(start <= day <= end for day in trades)
        output[name] = calculate(equity, closed)
    return output


def run(start: str, end: str) -> dict:
    universe, universe_meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end]
    if not universe_meta.get("complete") or len(days) < 120:
        raise RuntimeError("complete point-in-time universe is required")
    codes = sorted(set().union(*(universe[str(day)[:10]] for day in days)))
    bars = _load_daily(codes, start, end)
    for code in list(bars):
        bars[code] = _add_features(bars[code])
    liquidity_events, liquidity_manifest = _load_liquidity(codes, start, end)
    liquidity_days = sorted(liquidity_events)
    liquidity_number = 0
    current_liquidity = {}
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, dict] = {}
    pending_buys: list[str] = []
    last_close: dict[str, float] = {}
    equity_history = []
    closed_trade_dates = []
    total_fees = 0.0
    exit_reasons = Counter()

    def sell(code: str, raw_price: float, reason: str, day: pd.Timestamp) -> None:
        nonlocal cash, total_fees
        position = positions.pop(code)
        cap = current_liquidity.get(code, (day, 0, 0))[1] / 1e8
        _, proceeds, commission, tax = calc_trade_cost(
            raw_price, position["shares"], "sell", trade_cfg, market_cap=cap,
        )
        cash += proceeds
        total_fees += commission + tax
        closed_trade_dates.append(str(day)[:10])
        exit_reasons[reason] += 1

    for day_number, day in enumerate(days):
        while liquidity_number < len(liquidity_days) and liquidity_days[liquidity_number] <= day:
            event_day = liquidity_days[liquidity_number]
            for code, cap, turn in liquidity_events[event_day]:
                current_liquidity[code] = (event_day, cap, turn)
            liquidity_number += 1

        for code in list(positions):
            position = positions[code]
            if day_number - position["entry_day"] < PARAMETERS["maximum_hold_days"]:
                continue
            row = _bar(bars.get(code), day) if code in bars else None
            if _tradable(row, last_close.get(code, 0.0), "sell"):
                sell(code, float(row["open"]), "timeout", day)

        open_value = 0.0
        for code, position in positions.items():
            row = _bar(bars[code], day)
            price = float(row["open"]) if row is not None else last_close.get(code, 0.0)
            open_value += position["shares"] * price
        slot_value = (cash + open_value) * 0.95 / PARAMETERS["maximum_holdings"]
        bought_today = set()
        for code in pending_buys[:PARAMETERS["maximum_daily_buys"]]:
            if code in positions or len(positions) >= PARAMETERS["maximum_holdings"]:
                continue
            row = _bar(bars.get(code), day) if code in bars else None
            if not _tradable(row, last_close.get(code, 0.0), "buy"):
                continue
            shares = int(slot_value / float(row["open"]) // 100) * 100
            cap = current_liquidity.get(code, (day, 0, 0))[1] / 1e8
            while shares >= 100:
                execution, cost, commission, tax = calc_trade_cost(
                    float(row["open"]), shares, "buy", trade_cfg, market_cap=cap,
                )
                if cost <= cash:
                    break
                shares -= 100
            if shares >= 100:
                cash -= cost
                total_fees += commission + tax
                positions[code] = {
                    "shares": shares, "entry_price": execution, "entry_day": day_number,
                }
                bought_today.add(code)
        pending_buys = []

        for code in list(positions):
            if code in bought_today:
                continue
            row = _bar(bars[code], day)
            if not _tradable(row, last_close.get(code, 0.0), "sell"):
                continue
            exit_ = _intraday_exit(row, positions[code]["entry_price"])
            if exit_ is not None:
                sell(code, exit_[0], exit_[1], day)

        value = cash
        for code, position in positions.items():
            row = _bar(bars[code], day)
            value += position["shares"] * float(
                row["close"] if row is not None else last_close.get(code, 0.0)
            )
        equity_history.append({"date": str(day)[:10], "equity": round(value, 2)})
        for code, frame in bars.items():
            row = _bar(frame, day)
            if row is not None:
                last_close[code] = float(row["close"])

        rows = []
        for code in universe[str(day)[:10]]:
            frame = bars.get(code)
            row = _bar(frame, day) if frame is not None else None
            liquidity = current_liquidity.get(code)
            if (
                row is None or liquidity is None or (day - liquidity[0]).days > 10
                or int(row["history_days"]) < PARAMETERS["minimum_history_days"]
                or float(row["avg_value20"]) < PARAMETERS["minimum_average_value20"]
            ):
                continue
            fields = (
                "momentum5", "pv_corr10", "pv_corr20", "gap", "bollinger_width",
                "price_level", "price_trend", "volume_shrink", "volatility_abnormal",
            )
            if all(_finite(row.get(field)) for field in fields):
                item = {field: float(row[field]) for field in fields}
                item["code"] = code
                rows.append(item)
        ranking = _rank(rows)
        pending_buys = [code for code in ranking if code not in positions][
            :PARAMETERS["maximum_daily_buys"]
        ]

    metrics = calculate(equity_history, len(closed_trade_dates))
    failures = excellent_failures(metrics, require_deviation=False)
    phases = _phase_metrics(equity_history, closed_trade_dates)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    if not liquidity_manifest.get("complete"):
        failures.append(
            f"流通市值数据覆盖{float(liquidity_manifest.get('coverage', 0)):.2%}，未达到100%"
        )
    return {
        "strategy": "github-v27-price-volume-causal-replay-v1",
        "source": "https://github.com/fkchaos/a-share-quant-sim/blob/56f284784d4c7861165f192f4ede089cce45359e/scripts/strategies/v27_select.py",
        "source_commit": "56f284784d4c7861165f192f4ede089cce45359e",
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "execution": "close signal, next tradable open fill, A-share T+1 sell",
        "metrics": metrics,
        "phase_metrics": phases,
        "total_return_pct": round(
            (equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3,
        ),
        "total_fees": round(total_fees, 2),
        "exit_reasons": dict(exit_reasons),
        "liquidity_coverage": liquidity_manifest.get("coverage"),
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "2011-2017 is consumed development data and cannot be release evidence",
            "the source backtest's same-day-close signal/same-day-open fill is replaced by next-open execution",
            "historical ST identity is unavailable; conservative 5% fill limits are used",
            "an incomplete liquidity manifest prevents shadow or release promotion",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "v27_price_volume_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
