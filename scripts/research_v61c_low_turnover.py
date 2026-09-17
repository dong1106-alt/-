#!/usr/bin/env python3
"""Causal replay of the MIT-licensed v61c low-turnover/small-cap strategy."""
from __future__ import annotations

import argparse
import json
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


def _rank_codes(rows: list[tuple[str, float, float]]) -> list[str]:
    """Match v61c: equal-weight percentile ranks for low turnover and small float cap."""
    if not rows:
        return []
    frame = pd.DataFrame(rows, columns=["code", "cap", "turn"]).set_index("code")
    scores = (-frame["cap"]).rank(pct=True) + (-frame["turn"]).rank(pct=True)
    return list(scores.sort_values(ascending=False, kind="stable").index)


def _intraday_exit(row, entry_price: float) -> tuple[float, str] | None:
    stop = entry_price * 0.92
    take = entry_price * 1.25
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


def run(start: str, end: str) -> dict:
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
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, dict] = {}
    last_close: dict[str, float] = {}
    pending_sells: set[str] = set()
    pending_buys: list[str] = []
    equity_history = []
    closed_trades = 0
    total_fees = 0.0
    exit_reasons = Counter()

    for day_number, day in enumerate(days):
        while event_number < len(liquidity_days) and liquidity_days[event_number] <= day:
            event_day = liquidity_days[event_number]
            for code, cap, turn in liquidity_events[event_day]:
                current_liquidity[code] = (event_day, cap, turn)
            event_number += 1

        remaining_sells = set()
        for code in pending_sells:
            position = positions.get(code)
            row = _bar(bars.get(code), day) if code in bars else None
            if position is None:
                continue
            if not _tradable(row, last_close.get(code, 0.0), "sell"):
                remaining_sells.add(code)
                continue
            cap = current_liquidity.get(code, (day, 0, 0))[1] / 1e8
            _, proceeds, commission, tax = calc_trade_cost(
                float(row["open"]), position["shares"], "sell", trade_cfg, market_cap=cap,
            )
            cash += proceeds
            total_fees += commission + tax
            closed_trades += 1
            exit_reasons["rank_expired"] += 1
            del positions[code]
        pending_sells = remaining_sells

        open_value = 0.0
        for code, position in positions.items():
            row = _bar(bars[code], day)
            price = float(row["open"]) if row is not None else last_close.get(code, 0.0)
            open_value += position["shares"] * price
        slot_value = (cash + open_value) * 0.95 / 5
        bought_today = set()
        for code in pending_buys:
            if code in positions or len(positions) >= 5:
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
            if exit_ is None:
                continue
            raw_price, reason = exit_
            cap = current_liquidity.get(code, (day, 0, 0))[1] / 1e8
            _, proceeds, commission, tax = calc_trade_cost(
                raw_price, positions[code]["shares"], "sell", trade_cfg, market_cap=cap,
            )
            cash += proceeds
            total_fees += commission + tax
            closed_trades += 1
            exit_reasons[reason] += 1
            del positions[code]

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

        membership = universe[str(day)[:10]]
        candidates = []
        for code in membership:
            frame = bars.get(code)
            row = _bar(frame, day) if frame is not None else None
            liquidity = current_liquidity.get(code)
            if (row is None or liquidity is None or (day - liquidity[0]).days > 10
                    or int(row["history_days"]) < 120
                    or float(row["avg_value20"]) < 20_000_000):
                continue
            _, cap, turn = liquidity
            if cap > 0 and turn > 0:
                candidates.append((code, cap, turn))
        ranking = _rank_codes(candidates)
        top15 = set(ranking[:15])
        pending_sells |= {
            code for code, position in positions.items()
            if day_number - position["entry_day"] >= 5 and code not in top15
        }
        slots = 5 - (len(positions) - len(pending_sells))
        if slots > 0:
            excluded = set(positions) | pending_sells
            pending_buys = [code for code in ranking[:10] if code not in excluded][:slots]

    metrics = calculate(equity_history, closed_trades)
    failures = excellent_failures(metrics, require_deviation=False)
    if not liquidity_manifest.get("complete"):
        failures.append(
            f"流通市值数据覆盖{float(liquidity_manifest.get('coverage', 0)):.2%}，未达到100%"
        )
    return {
        "strategy": "github-v61c-causal-replay-v1",
        "source": "https://github.com/fkchaos/a-share-quant-sim/blob/main/scripts/strategies/v61c_turnover_size.py",
        "period": {"start": start, "end": end},
        "parameters": {
            "max_holdings": 5, "rebalance_days": 5, "stop_loss": -0.08,
            "take_profit": 0.25, "sell_out_of": 15,
            "minimum_history_days": 120, "minimum_average_value20": 20_000_000,
        },
        "metrics": metrics,
        "total_return_pct": round(
            (equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3,
        ),
        "total_fees": round(total_fees, 2),
        "exit_reasons": dict(exit_reasons),
        "liquidity_coverage": liquidity_manifest.get("coverage"),
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "2010-2017 is a consumed development domain and cannot be release evidence",
            "weekly point-in-time turnover replaces the source repository's current float-share table",
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
        default=DATA_ROOT / "research" / "v61c_low_turnover_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
