#!/usr/bin/env python3
"""Causal weekly small-float-cap research with a market-breadth risk gate."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from collections import defaultdict
from pathlib import Path

import pandas as pd

from performance_metrics import calculate, excellent_failures
from point_in_time_universe import load_universe
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _bar(frame: pd.DataFrame, day: pd.Timestamp):
    try:
        row = frame.loc[day]
        return row.iloc[-1] if isinstance(row, pd.DataFrame) else row
    except KeyError:
        return None


def _tradable(row, previous_close: float, side: str) -> bool:
    if row is None or float(row.get("open", 0)) <= 0 or float(row.get("volume", 0)) <= 0:
        return False
    if int(row.get("trade_status", 1)) != 1 or previous_close <= 0:
        return False
    ratio = float(row["open"]) / previous_close
    # A conservative 5% limit also prevents impossible fills for historical ST names.
    return ratio < 1.048 if side == "buy" else ratio > 0.952


def _load_daily(codes: list[str], start: str, end: str) -> dict[str, pd.DataFrame]:
    bars = {}
    history_manifest = json.loads(
        (DATA_ROOT / "universe" / "stock_history_manifest.json").read_text(encoding="utf-8")
    ).get("stocks", {})
    history_start = pd.Timestamp(start) - pd.Timedelta(days=240)
    end_ts = pd.Timestamp(end)
    for number, code in enumerate(codes, 1):
        path = DATA_ROOT / "stocks" / f"{code}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(
            path, columns=["date", "open", "high", "low", "close", "volume", "trade_status"],
        )
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame[(frame["date"] >= history_start) & (frame["date"] <= end_ts)].sort_values("date")
        if len(frame) < 120:
            continue
        frame["ma20"] = frame["close"].rolling(20).mean()
        volume_multiplier = 1 if str(history_manifest.get(code, {}).get("source", "")).startswith("BaoStock") else 100
        frame["avg_value20"] = (
            frame["close"] * frame["volume"] * volume_multiplier
        ).rolling(20).mean()
        frame["history_days"] = range(1, len(frame) + 1)
        bars[code] = frame.set_index("date")
        if number % 500 == 0:
            print(f"[smallcap] loaded daily {number}/{len(codes)}", flush=True)
    return bars


def _load_liquidity(codes: list[str], start: str, end: str) -> tuple[dict, dict]:
    manifest_path = DATA_ROOT / "liquidity" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = manifest.get("stocks") or {}
    events = defaultdict(list)
    start_ts = pd.Timestamp(start) - pd.Timedelta(days=14)
    end_ts = pd.Timestamp(end)
    for code in codes:
        entry = entries.get(code) or {}
        path = DATA_ROOT / "liquidity" / f"{code}.parquet"
        if entry.get("status") != "complete" or not path.exists():
            continue
        if entry.get("sha256") != _sha256(path):
            raise RuntimeError(f"liquidity checksum mismatch: {code}")
        frame = pd.read_parquet(path, columns=["date", "turn", "float_market_cap"])
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame[(frame["date"] >= start_ts) & (frame["date"] <= end_ts)]
        frame = frame.dropna(subset=["turn", "float_market_cap"])
        for row in frame.itertuples(index=False):
            events[row.date].append((code, float(row.float_market_cap), float(row.turn)))
    return dict(events), manifest


def run(start: str, end: str, topk: int = 20) -> dict:
    universe, universe_meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end]
    if not universe_meta.get("complete") or len(days) < 120:
        raise RuntimeError("complete point-in-time universe is required")
    codes = sorted(set().union(*(universe[str(day)[:10]] for day in days)))
    bars = _load_daily(codes, start, end)
    liquidity_events, liquidity_manifest = _load_liquidity(codes, start, end)
    liquidity_days = sorted(liquidity_events)
    event_number = 0
    current_liquidity = {}

    index = pd.read_parquet(DATA_ROOT / "index" / "sh000001.parquet")
    index["date"] = pd.to_datetime(index["date"])
    index = index.sort_values("date").set_index("date")
    index["ma20"] = index["close"].rolling(20).mean()
    days = [day for day in days if day in index.index]
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, int] = {}
    last_close: dict[str, float] = {}
    pending: list[str] | None = None
    equity_history = []
    closed_trades = 0
    total_fees = 0.0
    peak = cash
    risk_off_until = -1
    breadth_history = []

    for day_number, day in enumerate(days):
        while event_number < len(liquidity_days) and liquidity_days[event_number] <= day:
            event_day = liquidity_days[event_number]
            for code, cap, turn in liquidity_events[event_day]:
                current_liquidity[code] = (event_day, cap, turn)
            event_number += 1

        if pending is not None:
            target = set(pending)
            for code in list(positions):
                if code in target:
                    continue
                row = _bar(bars[code], day)
                if not _tradable(row, last_close.get(code, 0.0), "sell"):
                    continue
                shares = positions.pop(code)
                _, proceeds, commission, tax = calc_trade_cost(
                    float(row["open"]), shares, "sell", trade_cfg,
                    market_cap=current_liquidity.get(code, (day, 0, 0))[1] / 1e8,
                )
                cash += proceeds
                total_fees += commission + tax
                closed_trades += 1

            open_value = 0.0
            for code, shares in positions.items():
                row = _bar(bars[code], day)
                price = float(row["open"]) if row is not None else last_close.get(code, 0.0)
                open_value += shares * price
            slot_value = (cash + open_value) * 0.95 / topk
            for code in pending:
                if code in positions or len(positions) >= topk:
                    continue
                row = _bar(bars[code], day)
                if not _tradable(row, last_close.get(code, 0.0), "buy"):
                    continue
                shares = int(slot_value / float(row["open"]) // 100) * 100
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
            pending = None

        value = cash
        for code, shares in positions.items():
            row = _bar(bars[code], day)
            value += shares * float(row["close"] if row is not None else last_close.get(code, 0.0))
        peak = max(peak, value)
        drawdown_pct = (1.0 - value / peak) * 100 if peak else 0.0
        equity_history.append({"date": str(day)[:10], "equity": round(value, 2)})

        for code, frame in bars.items():
            row = _bar(frame, day)
            if row is not None:
                last_close[code] = float(row["close"])

        if drawdown_pct >= 15.0 and positions:
            pending = []
            risk_off_until = day_number + 20
            continue
        next_day = days[day_number + 1] if day_number + 1 < len(days) else None
        if next_day is not None and day.isocalendar().week == next_day.isocalendar().week:
            continue
        if pending is not None:
            continue

        membership = universe[str(day)[:10]]
        candidates = []
        breadth_total = 0
        breadth_above = 0
        for code in membership:
            frame = bars.get(code)
            if frame is None:
                continue
            row = _bar(frame, day)
            if row is None or pd.isna(row["ma20"]):
                continue
            breadth_total += 1
            breadth_above += int(float(row["close"]) > float(row["ma20"]))
            liquidity = current_liquidity.get(code)
            if (liquidity is None or (day - liquidity[0]).days > 10
                    or int(row["history_days"]) < 120
                    or float(row["avg_value20"]) < 20_000_000
                    or float(row["close"]) <= float(row["ma20"])):
                continue
            _, cap, turn = liquidity
            if cap > 500_000_000 and turn > 0:
                candidates.append((code, cap, turn))

        breadth = breadth_above / breadth_total if breadth_total else 0.0
        breadth_history.append({"date": str(day)[:10], "above_ma20_ratio": round(breadth, 4)})
        market_on = (
            float(index.loc[day, "close"]) > float(index.loc[day, "ma20"])
            and breadth > 0.5 and day_number >= risk_off_until
        )
        if not market_on:
            if positions:
                pending = []
            continue
        if not candidates:
            continue
        median_turn = pd.Series([row[2] for row in candidates]).median()
        low_turnover = [row for row in candidates if row[2] <= median_turn]
        pending = [row[0] for row in sorted(low_turnover, key=lambda row: row[1])[:topk]]

    metrics = calculate(equity_history, closed_trades)
    failures = excellent_failures(metrics, require_deviation=False)
    if not liquidity_manifest.get("complete"):
        failures.append(
            f"流通市值数据覆盖{float(liquidity_manifest.get('coverage', 0)):.2%}，未达到100%"
        )
    return {
        "strategy": "causal-small-float-low-turnover-breadth-v1",
        "period": {"start": start, "end": end},
        "parameters": {
            "topk": topk,
            "rebalance": "weekly",
            "minimum_float_market_cap": 500_000_000,
            "maximum_turnover_quantile": 0.5,
            "stock_ma": 20,
            "index_ma": 20,
            "minimum_breadth": 0.5,
            "minimum_average_value20": 20_000_000,
            "circuit_drawdown_pct": 15.0,
            "circuit_cooldown_days": 20,
        },
        "metrics": metrics,
        "total_return_pct": round((equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3),
        "total_fees": round(total_fees, 2),
        "liquidity_manifest": {
            key: liquidity_manifest.get(key) for key in (
                "source", "frequency", "coverage", "complete", "expected_codes", "complete_codes",
            )
        },
        "breadth_observations": len(breadth_history),
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "2010-2017 is a consumed development domain and cannot be release evidence",
            "historical ST identity is unavailable in weekly liquidity data; 5% fill limits are conservative",
            "an incomplete liquidity manifest prevents shadow or release promotion",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument("--topk", type=int, default=20)
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "smallcap_breadth_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end, args.topk)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
