#!/usr/bin/env python3
"""Causal replay of the fixed public ETF momentum plus RSRS strategy."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from performance_metrics import calculate, excellent_failures

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
ETF_ROOT = DATA_ROOT / "etf_rsrs"
MANIFEST_PATH = ETF_ROOT / "manifest.json"
INDEX_PATH = DATA_ROOT / "hs300" / "sh000300.parquet"
SOURCE_COMMIT = "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
SOURCE_BLOB_SHA = "5ae5c8669b6eb22ad86870daab03c5081895d2ed"
SOURCE_SHA256 = "c0bcc56927a40e3eec8c575f8678e5fa3ac8b4651cc230badc686e037dede583"
PARAMETERS = {
    "etfs": ["sh510180", "sz159915", "sh513100", "sh510500"],
    "topk": 1,
    "momentum_days": 29,
    "rsrs_n": 18,
    "rsrs_m": 600,
    "rsrs_threshold": 0.7,
    "ma_days": 20,
    "ma_difference_days": 3,
    "commission": 0.0003,
    "minimum_commission": 5.0,
    "fixed_slippage_yuan": 0.001,
    "stop_loss_pct": 80.0,
    "fill": "next open, T+1",
}
GOAL_THRESHOLDS = {
    "annual_return_pct": 20.0,
    "max_drawdown_pct": 20.0,
    "calmar": 1.5,
    "sortino": 1.5,
    "sharpe": 1.5,
    "closed_trades": 500,
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ols(x: np.ndarray, y: np.ndarray) -> tuple[float, float]:
    slope, intercept = np.polyfit(x, y, 1)
    denominator = (len(y) - 1) * np.var(y, ddof=1)
    r2 = 1.0 - float(np.square(y - (slope * x + intercept)).sum()) / denominator
    return float(slope), float(r2)


def momentum_score(close: pd.Series, cutoff) -> float | None:
    values = pd.to_numeric(close.loc[:pd.Timestamp(cutoff)], errors="coerce").dropna()
    values = values.tail(PARAMETERS["momentum_days"])
    if len(values) < PARAMETERS["momentum_days"] or (values <= 0).any():
        return None
    y = np.log(values.to_numpy(dtype=float))
    slope, r2 = _ols(np.arange(len(y), dtype=float), y)
    return (math.exp(slope) ** 250 - 1.0) * r2


def timing_signal(index: pd.DataFrame, cutoff) -> tuple[str, dict] | None:
    data = index.sort_index().loc[:pd.Timestamp(cutoff)]
    n, m = PARAMETERS["rsrs_n"], PARAMETERS["rsrs_m"]
    if len(data) < n + m:
        return None
    slopes = []
    for end in range(len(data) - m + 1, len(data) + 1):
        window = data.iloc[end - n:end]
        slope, _ = _ols(window["low"].to_numpy(), window["high"].to_numpy())
        slopes.append(slope)
    current = data.iloc[-n:]
    slope, r2 = _ols(current["low"].to_numpy(), current["high"].to_numpy())
    standard = float(np.std(slopes))
    if standard <= 0:
        return None
    rsrs = (slopes[-1] - float(np.mean(slopes))) / standard * r2
    close = data["close"].tail(PARAMETERS["ma_days"] + PARAMETERS["ma_difference_days"])
    current_ma = float(close.iloc[PARAMETERS["ma_difference_days"]:].mean())
    previous_ma = float(close.iloc[:-PARAMETERS["ma_difference_days"]].mean())
    if rsrs > PARAMETERS["rsrs_threshold"] and current_ma > previous_ma:
        signal = "BUY"
    elif rsrs < -PARAMETERS["rsrs_threshold"] and current_ma < previous_ma:
        signal = "SELL"
    else:
        signal = "KEEP"
    return signal, {"rsrs": rsrs, "r2": r2, "current_ma": current_ma, "previous_ma": previous_ma}


def next_open_schedule(signal_days, trading_days) -> list[dict[str, str]]:
    calendar = sorted({pd.Timestamp(day) for day in trading_days})
    result = []
    for signal in sorted({pd.Timestamp(day) for day in signal_days}):
        later = [day for day in calendar if day > signal]
        if later:
            result.append({"signal": str(signal)[:10], "fill": str(later[0])[:10]})
    return result


def backfill(start: str = "2010-01-04", end: str = "2017-12-29") -> dict:
    import backfill_history as history

    ETF_ROOT.mkdir(parents=True, exist_ok=True)
    history.STOCK_DIR = ETF_ROOT
    entries = {
        code: history.fetch_history(code, start, end)
        for code in PARAMETERS["etfs"]
    }
    manifest = {
        "source": "Tencent daily kline, forward adjusted",
        "start": start,
        "end": end,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries": entries,
        "complete": all(entries.get(code, {}).get("status") == "complete" for code in PARAMETERS["etfs"]),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _load_inputs() -> tuple[pd.DataFrame, dict[str, pd.DataFrame], dict]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not manifest.get("complete"):
        raise RuntimeError("complete ETF manifest is required")
    bars = {}
    for code in PARAMETERS["etfs"]:
        path = ETF_ROOT / f"{code}.parquet"
        entry = manifest["entries"].get(code) or {}
        if not path.exists() or entry.get("sha256") != _sha256(path):
            raise RuntimeError(f"ETF checksum mismatch: {code}")
        frame = pd.read_parquet(path)
        frame["date"] = pd.to_datetime(frame["date"])
        bars[code] = frame.sort_values("date").set_index("date")
    index = pd.read_parquet(INDEX_PATH)
    index["date"] = pd.to_datetime(index["date"])
    return index.sort_values("date").set_index("date"), bars, manifest


def _tradable(frame: pd.DataFrame, day: pd.Timestamp, previous_close: float, side: str) -> bool:
    if day not in frame.index or previous_close <= 0:
        return False
    row = frame.loc[day]
    if float(row["open"]) <= 0 or float(row["volume"]) <= 0 or int(row["trade_status"]) != 1:
        return False
    ratio = float(row["open"]) / previous_close
    return ratio < 1.098 if side == "buy" else ratio > 0.902


def mark_to_market(cash: float, holding: str | None, shares: int,
                   bars: dict[str, pd.DataFrame], day: pd.Timestamp,
                   previous_close: dict[str, float], entry_cost: float) -> float:
    if not holding:
        return cash
    frame = bars[holding]
    mark = float(frame.loc[day, "close"]) if day in frame.index else previous_close.get(holding, entry_cost)
    return cash + shares * mark


def _goal_failures(metrics: dict) -> list[str]:
    checks = (("annual_return_pct", ">"), ("max_drawdown_pct", "<"), ("calmar", ">"),
              ("sortino", ">"), ("sharpe", ">"), ("closed_trades", ">"))
    failures = []
    for key, operator in checks:
        value, threshold = float(metrics.get(key, -math.inf)), GOAL_THRESHOLDS[key]
        passed = value > threshold if operator == ">" else value < threshold
        if not passed:
            failures.append(f"{key}={value:g} does not satisfy {operator}{threshold:g}")
    return failures


def run(start: str, end: str) -> dict:
    index, bars, manifest = _load_inputs()
    days = [day for day in index.index if pd.Timestamp(start) <= day <= pd.Timestamp(end)]
    cash = 1_000_000.0
    holding = None
    shares = 0
    entry_cost = 0.0
    pending = None
    previous_close = {}
    equity_history = []
    closed_dates = []
    total_fees = 0.0
    signal_counts = {"BUY": 0, "SELL": 0, "KEEP": 0, "WARMUP": 0}

    for day in days:
        if pending != holding:
            if holding and _tradable(bars[holding], day, previous_close.get(holding, 0.0), "sell"):
                price = max(0.001, float(bars[holding].loc[day, "open"]) - PARAMETERS["fixed_slippage_yuan"])
                notional = price * shares
                commission = max(PARAMETERS["minimum_commission"], notional * PARAMETERS["commission"])
                cash += notional - commission
                total_fees += commission
                closed_dates.append(str(day)[:10])
                holding, shares, entry_cost = None, 0, 0.0
            if pending and holding is None and _tradable(
                bars[pending], day, previous_close.get(pending, 0.0), "buy"
            ):
                price = float(bars[pending].loc[day, "open"]) + PARAMETERS["fixed_slippage_yuan"]
                shares = int(cash / (price * (1 + PARAMETERS["commission"])) // 100) * 100
                if shares:
                    notional = price * shares
                    commission = max(PARAMETERS["minimum_commission"], notional * PARAMETERS["commission"])
                    cash -= notional + commission
                    total_fees += commission
                    holding, entry_cost = pending, price

        value = mark_to_market(cash, holding, shares, bars, day, previous_close, entry_cost)
        equity_history.append({"date": str(day)[:10], "equity": round(value, 2)})

        for code, frame in bars.items():
            if day in frame.index:
                previous_close[code] = float(frame.loc[day, "close"])

        timing = timing_signal(index, day)
        scores = {code: momentum_score(frame["close"], day) for code, frame in bars.items()}
        scores = {code: score for code, score in scores.items() if score is not None}
        if timing is None or not scores:
            signal_counts["WARMUP"] += 1
            pending = holding
            continue
        signal = timing[0]
        signal_counts[signal] += 1
        if holding and entry_cost and previous_close.get(holding, entry_cost) / entry_cost - 1 <= -0.8:
            pending = None
        elif signal == "SELL":
            pending = None
        else:
            pending = max(scores, key=scores.get)

    metrics = calculate(equity_history, len(closed_dates))
    goal_failures = _goal_failures(metrics)
    return {
        "strategy": "etf-rsrs-momentum-fixed-rule-causal-v1",
        "source": {
            "repository": "ShenzhenLime/factor_mining", "commit": SOURCE_COMMIT,
            "blob_sha": SOURCE_BLOB_SHA, "sha256": SOURCE_SHA256,
        },
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True).encode()).hexdigest(),
        "metrics": metrics,
        "total_return_pct": round((equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3),
        "total_fees": round(total_fees, 2),
        "signal_counts": signal_counts,
        "input_manifest": manifest,
        "goal_thresholds": GOAL_THRESHOLDS,
        "goal_failures": goal_failures,
        "repository_failures": excellent_failures(metrics, require_deviation=False),
        "decision": "research_pass" if not goal_failures else "research_rejected",
        "limitations": [
            "Signals use completed daily bars and fill at the next trading-day open",
            "The 2009-11-30 index start is retained; no 2002-2008 data is read",
            "ETFs become eligible only after 29 observed sessions",
            "Shadow deviation is not evaluated unless every development gate passes",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--output", type=Path,
                        default=ETF_ROOT / "etf_rsrs_momentum_dev_2011_2017.json")
    args = parser.parse_args()
    if args.backfill:
        print(json.dumps(backfill(end=args.end), ensure_ascii=False, indent=2))
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
