#!/usr/bin/env python3
"""Fixed-weight causal replay of a public BigQuant-style linear multifactor rule."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from collections import defaultdict
from pathlib import Path

import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS, calculate, excellent_failures
from point_in_time_universe import load_universe
from research_smallcap_breadth import _bar, _load_daily
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
PARAMETERS = {
    "factor_weights": {
        "earnings_yield": 0.30,
        "roe": 0.30,
        "momentum20": 0.20,
        "small_float_market_cap": 0.20,
    },
    "rebalance_trading_days": 20,
    "stock_num": 10,
    "liquidity_max_age_days": 10,
    "target_investment_ratio": 0.98,
}
PHASES = (
    ("2011-2013", "2011-01-04", "2013-12-31"),
    ("2014-2015", "2014-01-02", "2015-12-31"),
    ("2016-2017", "2016-01-04", "2017-12-29"),
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _add_ttm_fields(frame: pd.DataFrame) -> pd.DataFrame:
    """Build causal TTM profit/EPS; availability includes every required report."""
    output = []
    for _, group in frame.sort_values(["code", "report_date"]).groupby("code"):
        reports = {pd.Timestamp(row.report_date): row for row in group.itertuples(index=False)}
        for report_date, current in reports.items():
            prior_same = reports.get(report_date - pd.DateOffset(years=1))
            if prior_same is None:
                continue
            required = [current, prior_same]
            if report_date.month == 12:
                ttm_profit = current.net_profit
                ttm_eps = current.eps
            else:
                prior_annual = reports.get(pd.Timestamp(report_date.year - 1, 12, 31))
                if prior_annual is None:
                    continue
                required.append(prior_annual)
                ttm_profit = current.net_profit + prior_annual.net_profit - prior_same.net_profit
                ttm_eps = current.eps + prior_annual.eps - prior_same.eps
            if any(pd.isna(value) for value in (ttm_profit, ttm_eps, current.roe)):
                continue
            row = current._asdict()
            row["available_date"] = max(pd.Timestamp(item.available_date) for item in required)
            row["ttm_net_profit"] = float(ttm_profit)
            row["ttm_eps"] = float(ttm_eps)
            output.append(row)
    if not output:
        return pd.DataFrame()
    return pd.DataFrame(output).sort_values(["available_date", "code"]).reset_index(drop=True)


def _latest_as_of(frame: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    available = frame[frame["available_date"] <= day]
    if available.empty:
        return available
    return (
        available.sort_values(["report_date", "available_date", "code"])
        .drop_duplicates("code", keep="last").set_index("code")
    )


def _score_factors(cross: pd.DataFrame) -> pd.DataFrame:
    frame = cross.copy()
    numeric = ("ttm_eps", "roe", "momentum20", "close", "float_market_cap")
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    frame = frame.dropna(subset=list(numeric))
    frame = frame[
        (frame["close"] > 0)
        & (frame["float_market_cap"] > 0)
        & frame[list(numeric)].apply(lambda row: all(math.isfinite(value) for value in row), axis=1)
    ]
    if frame.empty:
        return frame
    frame["earnings_yield"] = frame["ttm_eps"] / frame["close"]
    ranks = {
        "earnings_yield": frame["earnings_yield"].rank(pct=True),
        "roe": frame["roe"].rank(pct=True),
        "momentum20": frame["momentum20"].rank(pct=True),
        "small_float_market_cap": (-frame["float_market_cap"]).rank(pct=True),
    }
    frame["score"] = sum(
        ranks[name] * weight for name, weight in PARAMETERS["factor_weights"].items()
    )
    frame["_code"] = frame.index
    return frame.sort_values(
        ["score", "_code"], ascending=[False, True],
    ).drop(columns="_code")


def _load_fundamentals() -> tuple[pd.DataFrame, dict]:
    root = DATA_ROOT / "fundamentals"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    frames = []
    for report_date, entry in sorted((manifest.get("periods") or {}).items()):
        path = Path(entry["path"])
        if not path.exists() or _sha256(path) != entry.get("sha256"):
            raise RuntimeError(f"fundamental checksum mismatch: {report_date}")
        frame = pd.read_parquet(path)
        actual = set(pd.to_datetime(frame["report_date"]).dt.strftime("%Y-%m-%d"))
        if actual != {report_date}:
            raise RuntimeError(f"fundamental report date mismatch: {report_date}")
        frames.append(frame.dropna(subset=["eps", "net_profit", "roe", "available_date"]))
    if not frames:
        raise RuntimeError("fundamental data missing")
    return _add_ttm_fields(pd.concat(frames, ignore_index=True)), manifest


def _load_liquidity(codes: list[str], start: str, end: str) -> tuple[dict, dict]:
    root = DATA_ROOT / "liquidity"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest.get("stocks") or {}
    events = defaultdict(list)
    start_ts = pd.Timestamp(start) - pd.Timedelta(days=14)
    end_ts = pd.Timestamp(end)
    for code in codes:
        entry = entries.get(code) or {}
        path = root / f"{code}.parquet"
        if entry.get("status") != "complete" or not path.exists():
            continue
        if entry.get("sha256") != _sha256(path):
            raise RuntimeError(f"liquidity checksum mismatch: {code}")
        frame = pd.read_parquet(path, columns=["date", "close", "float_market_cap"])
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame[(frame["date"] >= start_ts) & (frame["date"] <= end_ts)]
        frame = frame.dropna(subset=["close", "float_market_cap"])
        for row in frame.itertuples(index=False):
            events[row.date].append({
                "code": code,
                "close": float(row.close),
                "float_market_cap": float(row.float_market_cap),
            })
    return dict(events), manifest


def _tradable(row, side: str) -> bool:
    if row is None or float(row.get("open", 0)) <= 0 or float(row.get("volume", 0)) <= 0:
        return False
    if int(row.get("trade_status", 1)) != 1 or float(row.get("prev_close", 0)) <= 0:
        return False
    ratio = float(row["open"]) / float(row["prev_close"])
    # Historical ST identity is unavailable, so the conservative 5% band is fail-closed.
    return ratio < 1.048 if side == "buy" else ratio > 0.952


def _phase_metrics(equity_history: list[dict], trade_dates: list[str]) -> dict:
    output = {}
    for name, start, end in PHASES:
        equity = [row for row in equity_history if start <= row["date"] <= end]
        closed = sum(start <= day <= end for day in trade_dates)
        output[name] = calculate(equity, closed)
    return output


def run(start: str, end: str) -> dict:
    universe, universe_meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end]
    if not universe_meta.get("complete") or len(days) < 120:
        raise RuntimeError("complete point-in-time universe is required")
    codes = sorted(set().union(*(universe[str(day)[:10]] for day in days)))
    bars = _load_daily(codes, start, end)
    for frame in bars.values():
        frame["momentum20"] = frame["close"].pct_change(20)
        frame["prev_close"] = frame["close"].shift(1)
    fundamentals, fundamental_manifest = _load_fundamentals()
    liquidity_events, liquidity_manifest = _load_liquidity(codes, start, end)
    liquidity_days = sorted(liquidity_events)
    liquidity_number = 0
    current_liquidity = {}
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, int] = {}
    last_marks: dict[str, float] = {}
    pending: list[str] | None = None
    equity_history = []
    closed_trade_dates = []
    sell_transactions = 0
    total_fees = 0.0
    rebalance_signals = 0
    eligible_counts = []

    for day_number, day in enumerate(days):
        while liquidity_number < len(liquidity_days) and liquidity_days[liquidity_number] <= day:
            event_day = liquidity_days[liquidity_number]
            for event in liquidity_events[event_day]:
                current_liquidity[event["code"]] = (event_day, event)
            liquidity_number += 1

        if pending is not None:
            open_equity = cash
            for code, shares in positions.items():
                row = _bar(bars.get(code), day) if code in bars else None
                price = float(row["open"]) if row is not None else last_marks.get(code, 0.0)
                open_equity += shares * price
            target_value = open_equity * PARAMETERS["target_investment_ratio"] / PARAMETERS["stock_num"]
            desired = {}
            for code in pending:
                row = _bar(bars.get(code), day) if code in bars else None
                if row is not None and float(row.get("open", 0)) > 0:
                    desired[code] = int(target_value / float(row["open"]) // 100) * 100

            for code, shares in list(positions.items()):
                target_shares = desired.get(code, 0)
                sell_shares = shares - target_shares
                row = _bar(bars.get(code), day) if code in bars else None
                if sell_shares < 100 or not _tradable(row, "sell"):
                    continue
                event = current_liquidity.get(code, (day, {"float_market_cap": 0}))[1]
                _, proceeds, commission, tax = calc_trade_cost(
                    float(row["open"]), sell_shares, "sell", trade_cfg,
                    market_cap=float(event["float_market_cap"]) / 1e8,
                )
                cash += proceeds
                total_fees += commission + tax
                sell_transactions += 1
                remaining = shares - sell_shares
                if remaining:
                    positions[code] = remaining
                else:
                    positions.pop(code)
                    closed_trade_dates.append(str(day)[:10])

            for code in pending:
                row = _bar(bars.get(code), day) if code in bars else None
                buy_shares = desired.get(code, 0) - positions.get(code, 0)
                if buy_shares < 100 or not _tradable(row, "buy"):
                    continue
                event = current_liquidity.get(code, (day, {"float_market_cap": 0}))[1]
                while buy_shares >= 100:
                    _, cost, commission, tax = calc_trade_cost(
                        float(row["open"]), buy_shares, "buy", trade_cfg,
                        market_cap=float(event["float_market_cap"]) / 1e8,
                    )
                    if cost <= cash:
                        break
                    buy_shares -= 100
                if buy_shares >= 100:
                    cash -= cost
                    total_fees += commission + tax
                    positions[code] = positions.get(code, 0) + buy_shares
            pending = None

        value = cash
        for code, shares in positions.items():
            row = _bar(bars.get(code), day) if code in bars else None
            if row is not None:
                last_marks[code] = float(row["close"])
            value += shares * last_marks.get(code, 0.0)
        equity_history.append({"date": str(day)[:10], "equity": round(value, 2)})

        if (day_number + 1) % PARAMETERS["rebalance_trading_days"]:
            continue
        latest = _latest_as_of(fundamentals, day)
        rows = []
        for code in universe[str(day)[:10]]:
            if code not in latest.index or code not in bars:
                continue
            row = _bar(bars[code], day)
            liquidity = current_liquidity.get(code)
            if (
                row is None or float(row.get("volume", 0)) <= 0
                or int(row.get("trade_status", 1)) != 1
                or liquidity is None
                or (day - liquidity[0]).days > PARAMETERS["liquidity_max_age_days"]
            ):
                continue
            event = liquidity[1]
            item = latest.loc[code].to_dict()
            item.update({
                "code": code,
                "momentum20": row.get("momentum20"),
                "close": event["close"],
                "float_market_cap": event["float_market_cap"],
            })
            rows.append(item)
        cross = _score_factors(pd.DataFrame(rows).set_index("code")) if rows else pd.DataFrame()
        eligible_counts.append(len(cross))
        pending = list(cross.head(PARAMETERS["stock_num"]).index)
        rebalance_signals += 1

    metrics = calculate(equity_history, len(closed_trade_dates))
    phases = _phase_metrics(equity_history, closed_trade_dates)
    failures = excellent_failures(metrics, require_deviation=False)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    for manifest, label in (
        (fundamental_manifest, "基本面"),
        (liquidity_manifest, "流通市值"),
    ):
        if not manifest.get("complete"):
            failures.append(f"{label}数据覆盖{float(manifest.get('coverage', 0)):.2%}，未达到100%")
    return {
        "strategy": "bigquant-linear-multifactor-causal-approximation-v1",
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(
            json.dumps(PARAMETERS, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "execution": "20th trading-day close signal, next tradable open fill, A-share lots/limits/fees/slippage",
        "metrics": metrics,
        "phase_metrics": phases,
        "total_return_pct": round(
            (equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3,
        ),
        "total_fees": round(total_fees, 2),
        "sell_transactions": sell_transactions,
        "rebalance_signals": rebalance_signals,
        "average_eligible_cross_section": round(sum(eligible_counts) / len(eligible_counts), 1),
        "fundamental_coverage": fundamental_manifest.get("coverage"),
        "liquidity_coverage": liquidity_manifest.get("coverage"),
        "hard_gate": {
            "thresholds": EXCELLENT_THRESHOLDS,
            "development_failures": failures,
            "backtest_deviation_pct": None,
            "shadow_status": "not_evaluated; shadow creation requires explicit approval",
        },
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "2011-2017 is consumed development data and cannot be release evidence",
            "earnings yield uses causal TTM EPS divided by weekly unadjusted price",
            "weekly float market cap is the causal size factor",
            "historical ST identity is unavailable; conservative 5% fill limits are used",
            "any incomplete input manifest prevents shadow or release promotion",
            "2002-2008 unseen data is not read by this development replay",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "linear_multifactor_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
