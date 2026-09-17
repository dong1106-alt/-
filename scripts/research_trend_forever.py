#!/usr/bin/env python3
"""Causal replay of the public fixed-rule "Trend Forever" momentum strategy."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np
import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS, calculate, excellent_failures
from point_in_time_universe import load_universe
from research_smallcap_breadth import _bar, _load_daily
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
SOURCE_URL = (
    "https://github.com/ShenzhenLime/factor_mining/blob/"
    "f33c78ef6f5d203059785c5d7e51db2bf01a54ba/"
    "ref/ref_code/124/0208/2024%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89%E7%AD%96%E7%95%A52/"
    "31.%E3%80%8A%E8%B6%8B%E5%8A%BF%E6%B0%B8%E5%AD%98%E3%80%8B%E6%8C%81%E7%BB%AD"
    "16%E5%B9%B4%E8%B7%91%E8%B5%A2%E5%A4%A7%E7%9B%98%E7%9A%84%E7%9C%9F%E6%AD%A3"
    "%E9%9D%A0%E8%B0%B1%E7%AD%96%E7%95%A5.py"
)
SOURCE_COMMIT = "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
PARAMETERS = {
    "momentum_days": 90,
    "stock_ma_days": 100,
    "index_ma_days": 200,
    "atr_days": 20,
    "gap_threshold": 0.15,
    "rank_threshold": 60,
    "risk_factor": 0.001,
    "position_difference_threshold": 0.10,
    "cash_threshold": 500.0,
    "rebalance": "Wednesday open; ATR resize every other week",
}
PHASES = (
    ("2011-2013", "2011-01-04", "2013-12-31"),
    ("2014-2015", "2014-01-02", "2015-12-31"),
    ("2016-2017", "2016-01-04", "2017-12-29"),
)
HS300_ROOT = DATA_ROOT / "hs300"
MEMBERSHIP_PATH = HS300_ROOT / "weekly_memberships.json.gz"
MANIFEST_PATH = HS300_ROOT / "manifest.json"
INDEX_PATH = HS300_ROOT / "sh000300.parquet"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _momentum_score(close: pd.Series) -> float:
    values = pd.to_numeric(close, errors="coerce").dropna().tail(PARAMETERS["momentum_days"])
    if len(values) < PARAMETERS["momentum_days"] or (values <= 0).any():
        return float("nan")
    x = np.arange(len(values), dtype=float)
    y = np.log(values.to_numpy(dtype=float))
    slope, intercept = np.polyfit(x, y, 1)
    variance = np.var(y, ddof=1)
    if variance <= 0:
        return 0.0
    residual = np.sum((y - (slope * x + intercept)) ** 2)
    r_squared = max(0.0, 1.0 - residual / ((len(y) - 1) * variance))
    return (math.exp(slope) ** 250 - 1.0) * r_squared


def _wilder_atr(frame: pd.DataFrame, period: int = 20) -> float:
    data = frame.tail(period + 1)
    if len(data) < period + 1:
        return float("nan")
    previous = data["close"].shift(1)
    true_range = pd.concat(
        [data["high"] - data["low"], (data["high"] - previous).abs(),
         (data["low"] - previous).abs()], axis=1,
    ).max(axis=1)
    return float(true_range.iloc[1:].mean())


def _features(frame: pd.DataFrame, day: pd.Timestamp) -> dict | None:
    history = frame.loc[:day].tail(PARAMETERS["stock_ma_days"])
    if len(history) < PARAMETERS["stock_ma_days"]:
        return None
    close = float(history["close"].iloc[-1])
    ma100 = float(history["close"].mean())
    max_gap = float((history["low"] / history["high"].shift(2) - 1.0).max())
    atr = _wilder_atr(history, PARAMETERS["atr_days"])
    score = _momentum_score(history["close"])
    if not all(math.isfinite(value) for value in (close, ma100, max_gap, atr, score)):
        return None
    return {
        "close": close,
        "ma100": ma100,
        "max_gap": max_gap,
        "atr": atr,
        "score": score,
        "good": close >= ma100 and max_gap <= PARAMETERS["gap_threshold"] and atr > 0,
    }


def _tradable(row, previous_close: float, side: str) -> bool:
    if row is None or float(row.get("open", 0)) <= 0 or float(row.get("volume", 0)) <= 0:
        return False
    if int(row.get("trade_status", 1)) != 1 or previous_close <= 0:
        return False
    ratio = float(row["open"]) / previous_close
    return ratio < 1.048 if side == "buy" else ratio > 0.952


def _open_mark(frame: pd.DataFrame | None, day: pd.Timestamp, fallback: float) -> float:
    row = _bar(frame, day) if frame is not None else None
    return float(row["open"]) if row is not None else fallback


def _phase_metrics(equity_history: list[dict], close_dates: list[str]) -> dict:
    return {
        name: calculate(
            [row for row in equity_history if start <= row["date"] <= end],
            sum(start <= day <= end for day in close_dates),
        )
        for name, start, end in PHASES
    }


def _load_hs300() -> tuple[dict[str, set[str]], pd.DataFrame, dict]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not manifest.get("complete"):
        raise RuntimeError("complete HS300 history is required")
    if manifest.get("memberships_sha256") != _sha256(MEMBERSHIP_PATH):
        raise RuntimeError("HS300 membership checksum mismatch")
    if manifest.get("index_sha256") != _sha256(INDEX_PATH):
        raise RuntimeError("HS300 index checksum mismatch")
    with gzip.open(MEMBERSHIP_PATH, "rt", encoding="utf-8") as fh:
        raw = json.load(fh)
    memberships = {day: {code.replace(".", "") for code in codes} for day, codes in raw.items()}
    index = pd.read_parquet(INDEX_PATH)
    index["date"] = pd.to_datetime(index["date"])
    index = index.sort_values("date").set_index("date")
    index["ma200"] = index["close"].rolling(PARAMETERS["index_ma_days"]).mean()
    return memberships, index, manifest


def backfill_hs300(start: str, end: str) -> dict:
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required") from exc

    universe, metadata = load_universe()
    days = [day for day in sorted(universe) if start <= day <= end]
    expected = [day for day in days if pd.Timestamp(day).weekday() == 2]
    HS300_ROOT.mkdir(parents=True, exist_ok=True)
    memberships = {}
    if MEMBERSHIP_PATH.exists():
        with gzip.open(MEMBERSHIP_PATH, "rt", encoding="utf-8") as fh:
            memberships = json.load(fh)

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    try:
        missing = [day for day in expected if day not in memberships]
        for number, day in enumerate(missing, 1):
            result = bs.query_hs300_stocks(day)
            rows = []
            while result.error_code == "0" and result.next():
                rows.append(result.get_row_data())
            if result.error_code != "0" or len(rows) != 300:
                raise RuntimeError(f"HS300 query incomplete: {day} ({len(rows)})")
            memberships[day] = sorted({row[1] for row in rows})
            if number % 10 == 0 or number == len(missing):
                print(f"[hs300] memberships {number}/{len(missing)}", flush=True)

        result = bs.query_history_k_data_plus(
            "sh.000300", "date,open,high,low,close,preclose,volume,tradestatus",
            start_date=str(pd.Timestamp(start) - pd.Timedelta(days=400))[:10],
            end_date=end, frequency="d", adjustflag="3",
        )
        rows = []
        while result.error_code == "0" and result.next():
            rows.append(result.get_row_data())
        if result.error_code != "0" or not rows:
            raise RuntimeError(f"HS300 index query failed: {result.error_msg}")
    finally:
        bs.logout()

    with gzip.open(MEMBERSHIP_PATH, "wt", encoding="utf-8") as fh:
        json.dump(dict(sorted(memberships.items())), fh, ensure_ascii=False, separators=(",", ":"))
    columns = ["date", "open", "high", "low", "close", "prev_close", "volume", "trade_status"]
    index = pd.DataFrame(rows, columns=columns)
    index["date"] = pd.to_datetime(index["date"])
    for column in columns[1:]:
        index[column] = pd.to_numeric(index[column], errors="coerce")
    index = index.dropna(subset=["date", "open", "high", "low", "close"])
    index.to_parquet(INDEX_PATH, index=False)
    complete = bool(
        expected and metadata.get("complete")
        and all(day in memberships and len(memberships[day]) == 300 for day in expected)
        and len(index[index["date"] < pd.Timestamp(start)]) >= PARAMETERS["index_ma_days"]
    )
    manifest = {
        "source": "BaoStock.query_hs300_stocks+query_history_k_data_plus(sh.000300)",
        "source_commit": SOURCE_COMMIT,
        "start": start,
        "end": end,
        "expected_rebalance_days": len(expected),
        "captured_rebalance_days": sum(day in memberships for day in expected),
        "memberships_sha256": _sha256(MEMBERSHIP_PATH),
        "index_sha256": _sha256(INDEX_PATH),
        "complete": complete,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def run(start: str, end: str) -> dict:
    memberships, index, input_manifest = _load_hs300()
    universe, universe_meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end and pd.Timestamp(day) in index.index]
    if not universe_meta.get("complete") or len(days) < 200:
        raise RuntimeError("complete point-in-time calendar is required")
    rebalance_days = {pd.Timestamp(day) for day in memberships if start <= day <= end}
    if rebalance_days != {day for day in days if day.weekday() == 2}:
        raise RuntimeError("HS300 rebalance-day coverage mismatch")
    codes = sorted(set().union(*(memberships[str(day)[:10]] for day in rebalance_days)))
    bars = _load_daily(codes, start, end)
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, int] = {}
    last_close: dict[str, float] = {}
    pending: dict | None = None
    equity_history = []
    close_dates = []
    total_fees = 0.0
    rebalance_count = 0
    resize_next = True
    eligible_counts = []

    for number, day in enumerate(days):
        if pending is not None:
            features = pending["features"]
            target = pending["target"]
            for code in list(positions):
                if code in target:
                    continue
                row = _bar(bars.get(code), day) if code in bars else None
                if not _tradable(row, last_close.get(code, 0.0), "sell"):
                    continue
                shares = positions.pop(code)
                _, proceeds, commission, tax = calc_trade_cost(float(row["open"]), shares, "sell", trade_cfg)
                cash += proceeds
                total_fees += commission + tax
                close_dates.append(str(day)[:10])

            open_equity = cash + sum(
                shares * _open_mark(bars.get(code), day, last_close.get(code, 0.0))
                for code, shares in positions.items()
            )
            desired = {}
            expected_values = {}
            for code in target:
                row = _bar(bars.get(code), day) if code in bars else None
                item = features[code]
                if row is None or item["atr"] <= 0:
                    continue
                expected_value = open_equity * PARAMETERS["risk_factor"] * item["close"] / item["atr"]
                expected_values[code] = expected_value
                desired[code] = int(expected_value / float(row["open"]) // 100) * 100

            # The source trade() visits the ranked pool in order and stops when
            # available cash is no longer larger than a full target position.
            if pending["market_on"]:
                for code in target:
                    if cash <= expected_values.get(code, float("inf")):
                        break
                    row = _bar(bars.get(code), day) if code in bars else None
                    current = positions.get(code, 0)
                    target_shares = desired.get(code, 0)
                    delta = target_shares - current
                    if delta < 0 and _tradable(row, last_close.get(code, features[code]["close"]), "sell"):
                        sell_shares = -delta
                        _, proceeds, commission, tax = calc_trade_cost(float(row["open"]), sell_shares, "sell", trade_cfg)
                        cash += proceeds
                        total_fees += commission + tax
                        if target_shares:
                            positions[code] = target_shares
                        else:
                            positions.pop(code, None)
                            close_dates.append(str(day)[:10])
                    elif delta >= 100 and _tradable(row, features[code]["close"], "buy"):
                        buy_shares = delta
                        while buy_shares >= 100:
                            _, cost, commission, tax = calc_trade_cost(float(row["open"]), buy_shares, "buy", trade_cfg)
                            if cost <= cash:
                                break
                            buy_shares -= 100
                        if buy_shares >= 100:
                            cash -= cost
                            total_fees += commission + tax
                            positions[code] = current + buy_shares

            # The separate adjust_position() callback runs every other week,
            # including when the index filter blocks new positions.
            if resize_next:
                for code, shares in list(positions.items()):
                    if code not in desired:
                        continue
                    row = _bar(bars.get(code), day) if code in bars else None
                    target_shares = desired[code]
                    current_value = shares * float(row["open"])
                    expected_value = expected_values[code]
                    if expected_value <= 0 or abs(current_value / expected_value - 1) <= PARAMETERS["position_difference_threshold"]:
                        continue
                    delta = target_shares - shares
                    if delta < 0 and _tradable(row, last_close.get(code, features[code]["close"]), "sell"):
                        sell_shares = -delta
                        _, proceeds, commission, tax = calc_trade_cost(float(row["open"]), sell_shares, "sell", trade_cfg)
                        cash += proceeds
                        total_fees += commission + tax
                        if target_shares:
                            positions[code] = target_shares
                        else:
                            positions.pop(code, None)
                            close_dates.append(str(day)[:10])
                    elif (
                        delta >= 100 and cash > expected_value - current_value
                        and cash > PARAMETERS["cash_threshold"]
                        and _tradable(row, features[code]["close"], "buy")
                    ):
                        buy_shares = delta
                        while buy_shares >= 100:
                            _, cost, commission, tax = calc_trade_cost(float(row["open"]), buy_shares, "buy", trade_cfg)
                            if cost <= cash:
                                break
                            buy_shares -= 100
                        if buy_shares >= 100:
                            cash -= cost
                            total_fees += commission + tax
                            positions[code] = shares + buy_shares
            resize_next = not resize_next
            rebalance_count += 1
            pending = None

        value = cash
        for code, shares in positions.items():
            row = _bar(bars.get(code), day) if code in bars else None
            if row is not None:
                last_close[code] = float(row["close"])
            value += shares * last_close.get(code, 0.0)
        equity_history.append({"date": str(day)[:10], "equity": round(value, 2)})

        next_day = days[number + 1] if number + 1 < len(days) else None
        if next_day not in rebalance_days:
            continue
        rows = []
        feature_map = {}
        for code in memberships[str(next_day)[:10]]:
            frame = bars.get(code)
            if frame is None:
                continue
            item = _features(frame, day)
            if item is None:
                continue
            feature_map[code] = item
            rows.append((code, item["score"]))
        ranked = [code for code, _ in sorted(rows, key=lambda row: (-row[1], row[0]))]
        top = ranked[:PARAMETERS["rank_threshold"]]
        target = [code for code in top if feature_map[code]["good"]]
        eligible_counts.append(len(target))
        index_row = _bar(index, day)
        market_on = bool(
            index_row is not None and pd.notna(index_row["ma200"])
            and float(index_row["close"]) > float(index_row["ma200"])
        )
        pending = {"target": target, "features": feature_map, "market_on": market_on}

    metrics = calculate(equity_history, len(close_dates))
    phases = _phase_metrics(equity_history, close_dates)
    failures = excellent_failures(metrics, require_deviation=False)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    return {
        "strategy": "trend-forever-fixed-rule-causal-replay-v1",
        "source": {"url": SOURCE_URL, "commit": SOURCE_COMMIT},
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True).encode()).hexdigest(),
        "execution": "prior-close signal, Wednesday next-open fills, A-share lots/limits/fees/slippage",
        "metrics": metrics,
        "phase_metrics": phases,
        "total_return_pct": round((equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3),
        "total_fees": round(total_fees, 2),
        "rebalance_count": rebalance_count,
        "average_eligible_count": round(sum(eligible_counts) / len(eligible_counts), 1),
        "input_manifest": input_manifest,
        "hard_gate": {"thresholds": EXCELLENT_THRESHOLDS, "development_failures": failures},
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
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
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--output", type=Path, default=DATA_ROOT / "research" / "trend_forever_dev_2011_2017.json")
    args = parser.parse_args()
    if args.backfill:
        result = backfill_hs300(args.start, args.end)
    else:
        result = run(args.start, args.end)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("complete", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
