#!/usr/bin/env python3
"""Causal replay of the public fixed-rule Trend Trading 5.0 strategy."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS, calculate, excellent_failures
from point_in_time_universe import load_universe
from research_smallcap_breadth import _bar, _load_daily
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
HS300_ROOT = DATA_ROOT / "hs300"
SEED_MEMBERSHIP_PATH = HS300_ROOT / "weekly_memberships.json.gz"
MEMBERSHIP_PATH = HS300_ROOT / "trend_v5_weekly_memberships.json.gz"
MANIFEST_PATH = HS300_ROOT / "trend_v5_manifest.json"
SOURCE_COMMIT = "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
SOURCE_URL = (
    "https://github.com/ShenzhenLime/factor_mining/blob/"
    f"{SOURCE_COMMIT}/ref/ref_code/124/0208/2023%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89"
    "%E7%AD%96%E7%95%A5/33.%E8%B6%8B%E5%8A%BF%E4%BA%A4%E6%98%935.0%20%E6%97%A0%E6%8B%A9"
    "%E6%97%B6-2%E5%B9%B45%E5%80%8D%E4%B8%8D%E6%98%AF%E6%A2%A6.py"
)
PARAMETERS = {
    "close_window": 100,
    "ma_windows": [30, 60, 100],
    "minimum_correlation": 0.5,
    "minimum_slope_intercept": 0.005,
    "high_window": 30,
    "maximum_high_to_close": 1.1,
    "volume_short_window": 7,
    "volume_long_window": 180,
    "maximum_volume_ratio": 1.5,
    "stock_num": 5,
    "rebalance": "daily open",
}
PHASES = (
    ("2011-2013", "2011-01-04", "2013-12-31"),
    ("2014-2015", "2014-01-02", "2015-12-31"),
    ("2016-2017", "2016-01-04", "2017-12-29"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _week_key(day: str | pd.Timestamp) -> str:
    iso = pd.Timestamp(day).isocalendar()
    return f"{iso.year:04d}-{iso.week:02d}"


def _prepare_features(frame: pd.DataFrame) -> pd.DataFrame:
    active = frame[pd.to_numeric(frame["trade_status"], errors="coerce") == 1].copy()
    active = active.dropna(subset=["close", "high", "volume"]).sort_index()
    close = active["close"].astype(float)
    high = active["high"].astype(float)
    volume = active["volume"].astype(float)
    n = PARAMETERS["close_window"]
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
    output["ma30"] = close.rolling(30).mean()
    output["ma60"] = close.rolling(60).mean()
    output["ma100"] = close.rolling(100).mean()
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
        (output["close"] > output["ma100"])
        & (output["ma30"] > output["ma60"])
        & (output["ma60"] > output["ma100"])
        & (output["correlation"] > PARAMETERS["minimum_correlation"])
        & (output["slope_intercept"] > PARAMETERS["minimum_slope_intercept"])
        & (output["high_to_close"] <= PARAMETERS["maximum_high_to_close"])
        & (output["volume_ratio"] <= PARAMETERS["maximum_volume_ratio"])
    )
    return output


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


def backfill_memberships(start: str, end: str) -> dict:
    try:
        import baostock as bs
    except ImportError as exc:
        raise RuntimeError("baostock is required") from exc
    universe, metadata = load_universe()
    days = [day for day in sorted(universe) if start <= day <= end]
    if not metadata.get("complete") or not days:
        raise RuntimeError("complete historical trading calendar is required")
    weeks = defaultdict(list)
    for day in days:
        weeks[_week_key(day)].append(day)

    with gzip.open(SEED_MEMBERSHIP_PATH, "rt", encoding="utf-8") as fh:
        seed = json.load(fh)
    seed_by_week = {_week_key(day): codes for day, codes in seed.items()}
    captured = {}
    if MEMBERSHIP_PATH.exists():
        with gzip.open(MEMBERSHIP_PATH, "rt", encoding="utf-8") as fh:
            captured = json.load(fh)
    missing = [key for key in sorted(weeks) if key not in captured and key not in seed_by_week]

    login = bs.login()
    if login.error_code != "0":
        raise RuntimeError(f"BaoStock login failed: {login.error_msg}")
    try:
        for number, key in enumerate(missing, 1):
            query_day = weeks[key][0]
            result = bs.query_hs300_stocks(query_day)
            rows = []
            while result.error_code == "0" and result.next():
                rows.append(result.get_row_data())
            if result.error_code != "0" or len(rows) != 300:
                raise RuntimeError(f"HS300 query incomplete: {query_day} ({len(rows)})")
            captured[key] = sorted({row[1] for row in rows})
            print(f"[trend-v5] missing weeks {number}/{len(missing)}", flush=True)
    finally:
        bs.logout()
    for key, codes in seed_by_week.items():
        if key in weeks:
            captured[key] = codes
    captured = {key: captured[key] for key in sorted(weeks) if key in captured}
    HS300_ROOT.mkdir(parents=True, exist_ok=True)
    with gzip.open(MEMBERSHIP_PATH, "wt", encoding="utf-8") as fh:
        json.dump(captured, fh, ensure_ascii=False, separators=(",", ":"))
    complete = bool(
        len(captured) == len(weeks)
        and all(len(set(captured.get(key, []))) == 300 for key in weeks)
    )
    manifest = {
        "source": "BaoStock.query_hs300_stocks; weekly snapshots seeded from Trend Forever research",
        "source_commit": SOURCE_COMMIT,
        "start": start,
        "end": end,
        "expected_weeks": len(weeks),
        "captured_weeks": len(captured),
        "seed_sha256": _sha256(SEED_MEMBERSHIP_PATH),
        "memberships_sha256": _sha256(MEMBERSHIP_PATH),
        "complete": complete,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _load_memberships(days: list[pd.Timestamp]) -> tuple[dict[str, set[str]], dict]:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not manifest.get("complete") or manifest.get("memberships_sha256") != _sha256(MEMBERSHIP_PATH):
        raise RuntimeError("complete Trend V5 HS300 membership history is required")
    with gzip.open(MEMBERSHIP_PATH, "rt", encoding="utf-8") as fh:
        weekly = json.load(fh)
    memberships = {
        str(day)[:10]: {code.replace(".", "") for code in weekly.get(_week_key(day), [])}
        for day in days
    }
    if not memberships or any(len(codes) != 300 for codes in memberships.values()):
        raise RuntimeError("Trend V5 HS300 membership coverage mismatch")
    return memberships, manifest


def run(start: str, end: str) -> dict:
    universe, universe_meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end]
    if not universe_meta.get("complete") or len(days) < 180:
        raise RuntimeError("complete historical trading calendar is required")
    memberships, input_manifest = _load_memberships(days)
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
    average_eligible = []
    transactions = 0
    minimum_cash = cash
    maximum_positions = 0

    for number, day in enumerate(days):
        if pending is not None:
            open_equity = cash
            for code, shares in positions.items():
                row = _bar(bars.get(code), day) if code in bars else None
                open_equity += shares * (float(row["open"]) if row is not None else last_close.get(code, 0.0))
            slot_value = open_equity / len(pending) if pending else 0.0
            desired = {}
            for code in pending:
                row = _bar(bars.get(code), day) if code in bars else None
                if row is not None and float(row["open"]) > 0:
                    desired[code] = int(slot_value / float(row["open"]) // 100) * 100

            for code, shares in list(positions.items()):
                target_shares = desired.get(code, 0)
                sell_shares = shares - target_shares
                row = _bar(bars.get(code), day) if code in bars else None
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

            for code in pending:
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
        if next_day is None:
            continue
        ranked = []
        for code in memberships[str(next_day)[:10]]:
            frame = features.get(code)
            if frame is None:
                continue
            row = _bar(frame, day)
            if row is not None and bool(row["eligible"]):
                ranked.append((code, float(row["slope"])))
        ranked.sort(key=lambda item: (-item[1], item[0]))
        average_eligible.append(len(ranked))
        pending = [code for code, _ in ranked[:PARAMETERS["stock_num"]]]

    metrics = calculate(equity_history, len(close_dates))
    phases = _phase_metrics(equity_history, close_dates)
    failures = excellent_failures(metrics, require_deviation=False)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    return {
        "strategy": "trend-trading-v5-fixed-rule-causal-replay-v1",
        "source": {"url": SOURCE_URL, "commit": SOURCE_COMMIT},
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
        "average_eligible_count": round(sum(average_eligible) / len(average_eligible), 1),
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
    parser.add_argument("--output", type=Path, default=DATA_ROOT / "research" / "trend_v5_dev_2011_2017.json")
    args = parser.parse_args()
    if args.backfill:
        result = backfill_memberships(args.start, args.end)
    else:
        result = run(args.start, args.end)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("complete", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
