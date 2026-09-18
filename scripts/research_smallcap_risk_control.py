#!/usr/bin/env python3
"""Causal replay of the public high-return/low-drawdown small-cap strategy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS, calculate, excellent_failures
from point_in_time_universe import load_universe
from research_smallcap_breadth import _bar, _load_daily, _load_liquidity, _tradable
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
INDEX_PATH = DATA_ROOT / "hs300" / "sh000300.parquet"
SOURCE_COMMIT = "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
SOURCE_BLOB_SHA = "6282002082716e9e56554aff6c2964ccd80532f7"
SOURCE_SHA256 = "f22e598f39147496c73592e9fd0e5250e85610ab02764ba49e11ecba38f62cb3"
SOURCE_URL = (
    "https://github.com/ShenzhenLime/factor_mining/blob/"
    f"{SOURCE_COMMIT}/ref/ref_code/124/0208/2021%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89"
    "%E7%AD%96%E7%95%A5/47.%E9%AB%98%E6%94%B6%E7%9B%8A%E4%BD%8E%E5%9B%9E%E6%92%A4"
    "%E7%9A%84%E5%B0%8F%E5%B8%82%E5%80%BC%E7%AD%96%E7%95%A5.py"
)
PARAMETERS = {
    "universe": "399101.XSHE historical SME board, causally represented by listed sz002 codes",
    "maximum_float_market_cap": 10_000_000_000,
    "fundamental_limit": 10,
    "stock_num": 5,
    "rebalance": "daily",
    "source_trade_time": "14:40",
    "causal_fill": "prior-close signal, next-open T+1 fill",
    "risk_index": "000300.XSHG",
    "ma_window": 1000,
    "warning_ma_rate_high": 2.5,
    "warning_ma_rate_low": 0.30,
    "normal_ma_rate_low": 0.35,
    "normal_ma_rate_high": 0.70,
    "warning_rsi_window": 60,
    "warning_rsi_min": 47.0,
    "warning_rsi_max": 99.0,
    "allocation_denominator": "stock_num - current_position_count * 0.33",
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


def _wilder_rsi(closes: pd.Series, period: int) -> float:
    values = pd.to_numeric(closes, errors="coerce").dropna().to_numpy(dtype=float)
    if len(values) <= period:
        return float("nan")
    deltas = values[1:] - values[:-1]
    gains = deltas.clip(min=0)
    losses = (-deltas).clip(min=0)
    average_gain = gains[:period].mean()
    average_loss = losses[:period].mean()
    for gain, loss in zip(gains[period:], losses[period:]):
        average_gain = (average_gain * (period - 1) + gain) / period
        average_loss = (average_loss * (period - 1) + loss) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 0.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def _risk_signal(status: str, closes: pd.Series) -> tuple[str, bool, float, float]:
    values = pd.to_numeric(closes, errors="coerce").dropna().tail(PARAMETERS["ma_window"])
    if len(values) < 2:
        return status, False, float("nan"), float("nan")
    ma_rate = float(values.iloc[-1] / values.mean())
    if status == "normal" and (
        ma_rate > PARAMETERS["warning_ma_rate_high"]
        or ma_rate < PARAMETERS["warning_ma_rate_low"]
    ):
        status = "warning"
    elif status == "warning" and (
        PARAMETERS["normal_ma_rate_low"] <= ma_rate <= PARAMETERS["normal_ma_rate_high"]
    ):
        status = "normal"
    rsi = _wilder_rsi(values.tail(PARAMETERS["warning_rsi_window"] + 1),
                      PARAMETERS["warning_rsi_window"])
    allowed = status == "normal" or (
        PARAMETERS["warning_rsi_min"] < rsi < PARAMETERS["warning_rsi_max"]
    )
    return status, bool(allowed), ma_rate, rsi


def _rank_targets(day: pd.Timestamp, membership: set[str], bars: dict,
                  liquidity: dict, previous_closes: dict[str, float], held: set[str]) -> list[str]:
    candidates = []
    for code in membership:
        if not code.startswith("sz002"):
            continue
        row = _bar(bars.get(code), day) if code in bars else None
        event = liquidity.get(code)
        previous = previous_closes.get(code, 0.0)
        if (
            row is None or event is None or (day - event[0]).days > 10
            or int(row.get("trade_status", 1)) != 1 or float(row.get("volume", 0)) <= 0
            or float(event[1]) <= 0 or float(event[1]) >= PARAMETERS["maximum_float_market_cap"]
            or previous <= 0
        ):
            continue
        ratio = float(row["close"]) / previous
        if code not in held and not (0.952 < ratio < 1.048):
            continue
        candidates.append((code, float(event[1])))
    ranked = [code for code, _ in sorted(candidates, key=lambda item: (item[1], item[0]))]
    return ranked[:PARAMETERS["fundamental_limit"]][:PARAMETERS["stock_num"]]


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
    codes = sorted({
        code for day in days for code in universe[str(day)[:10]] if code.startswith("sz002")
    })
    bars = _load_daily(codes, start, end)
    liquidity_events, liquidity_manifest = _load_liquidity(codes, start, end)
    liquidity_days = sorted(liquidity_events)
    liquidity_number = 0
    current_liquidity = {}
    index = pd.read_parquet(INDEX_PATH)
    index["date"] = pd.to_datetime(index["date"])
    index = index.sort_values("date").set_index("date")
    if any(day not in index.index for day in days):
        raise RuntimeError("HS300 index coverage mismatch")
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
    status = "normal"
    warning_days = 0
    risk_off_days = 0

    for number, day in enumerate(days):
        while liquidity_number < len(liquidity_days) and liquidity_days[liquidity_number] <= day:
            event_day = liquidity_days[liquidity_number]
            for code, cap, turn in liquidity_events[event_day]:
                current_liquidity[code] = (event_day, cap, turn)
            liquidity_number += 1

        if pending is not None:
            target = set(pending)
            for code in list(positions):
                if code in target:
                    continue
                row = _bar(bars.get(code), day) if code in bars else None
                if not _tradable(row, last_close.get(code, 0.0), "sell"):
                    continue
                cap = current_liquidity.get(code, (day, 0, 0))[1] / 1e8
                _, proceeds, commission, tax = calc_trade_cost(
                    float(row["open"]), positions.pop(code), "sell", trade_cfg, market_cap=cap,
                )
                cash += proceeds
                total_fees += commission + tax
                transactions += 1
                close_dates.append(str(day)[:10])

            position_count = len(positions)
            denominator = PARAMETERS["stock_num"] - position_count * 0.33
            order_value = cash / denominator if denominator > 0 else 0.0
            for code in pending:
                if code in positions or len(positions) >= PARAMETERS["stock_num"]:
                    continue
                row = _bar(bars.get(code), day) if code in bars else None
                if not _tradable(row, last_close.get(code, 0.0), "buy"):
                    continue
                shares = int(order_value / float(row["open"]) // 100) * 100
                cap = current_liquidity.get(code, (day, 0, 0))[1] / 1e8
                while shares >= 100:
                    _, cost, commission, tax = calc_trade_cost(
                        float(row["open"]), shares, "buy", trade_cfg, market_cap=cap,
                    )
                    if cost <= cash:
                        break
                    shares -= 100
                if shares >= 100:
                    cash -= cost
                    positions[code] = shares
                    total_fees += commission + tax
                    transactions += 1
            pending = None

        if cash < -0.01 or any(shares <= 0 or shares % 100 for shares in positions.values()):
            raise RuntimeError("portfolio accounting invariant failed")
        minimum_cash = min(minimum_cash, cash)
        maximum_positions = max(maximum_positions, len(positions))
        value = cash
        for code, shares in positions.items():
            row = _bar(bars.get(code), day) if code in bars else None
            value += shares * float(row["close"] if row is not None else last_close.get(code, 0.0))
        equity_history.append({"date": str(day)[:10], "equity": round(value, 2)})

        previous_closes = dict(last_close)
        for code, frame in bars.items():
            row = _bar(frame, day)
            if row is not None:
                last_close[code] = float(row["close"])
        if number + 1 >= len(days):
            continue
        status, allowed, _, _ = _risk_signal(status, index.loc[:day, "close"])
        warning_days += int(status == "warning")
        risk_off_days += int(not allowed)
        pending = [] if not allowed else _rank_targets(
            day, universe[str(day)[:10]], bars, current_liquidity, previous_closes, set(positions),
        )

    metrics = calculate(equity_history, len(close_dates))
    phases = _phase_metrics(equity_history, close_dates)
    failures = excellent_failures(metrics, require_deviation=False)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    if not liquidity_manifest.get("complete"):
        failures.append(
            f"流通市值数据覆盖{float(liquidity_manifest.get('coverage', 0)):.4%}，未达到100%"
        )
    return {
        "strategy": "smallcap-risk-control-fixed-rule-causal-replay-v1",
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
        "minimum_cash": round(minimum_cash, 2),
        "maximum_positions": maximum_positions,
        "warning_days": warning_days,
        "risk_off_days": risk_off_days,
        "liquidity_coverage": liquidity_manifest.get("coverage"),
        "hard_gate": {"thresholds": EXCELLENT_THRESHOLDS, "development_failures": failures},
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "The source's 14:40 same-day ranking is causally corrected to prior close and next open",
            "Historical SME-board membership is represented by point-in-time listed sz002 codes",
            "Historical ST identity is unavailable; conservative 5% signal and fill limits are used",
            "Incomplete weekly float-market-cap coverage prevents shadow or release promotion",
            "2011-2017 is consumed development data; 2002-2008 unseen data is not read",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "smallcap_risk_control_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
