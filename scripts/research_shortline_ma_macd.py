#!/usr/bin/env python3
"""Causal replay of the public short-line MA/MACD strategy."""
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
SOURCE_BLOB_SHA = "082e21553eeec78321b86951a4c1428fad35eff5"
SOURCE_SHA256 = "2b59da01105558cd44b3aab3e23f4850aa9d552d176d299bc0c571e9fe29589d"
SOURCE_URL = (
    "https://github.com/ShenzhenLime/factor_mining/blob/"
    f"{SOURCE_COMMIT}/ref/ref_code/124/0208/2024%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89"
    "%E7%AD%96%E7%95%A52/80.%E7%9F%AD%E7%BA%BF%E7%AD%96%E7%95%A5-2022%E5%B9%B4"
    "%E4%B8%A4%E4%B8%AA%E5%8D%8A%E6%9C%88%E6%94%B6%E7%9B%8A45%25-%E6%97%A0%E6%9C%AA"
    "%E6%9D%A5%E5%87%BD%E6%95%B0.py"
)
PARAMETERS = {
    "universe": "historical A-share main board excluding 30/688/8 prefixes",
    "stock_num": 1,
    "close_ma_windows": [5, 10, 20, 30],
    "volume_ma_windows": [5, 10],
    "volume_ma5_multiplier": 1.2,
    "macd": [12, 26, 9],
    "macd_hist_min": 0.0,
    "macd_hist_max": 0.1,
    "rank": "lexicographic code order",
    "rebalance": "daily open",
    "signal_cutoff": "previous trading day close, including that day's open gap",
    "fill": "next open, T+1",
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


def _signal_series(frame: pd.DataFrame) -> pd.Series:
    close = pd.to_numeric(frame["close"], errors="coerce")
    open_ = pd.to_numeric(frame["open"], errors="coerce")
    volume = pd.to_numeric(frame["volume"], errors="coerce")
    status = pd.to_numeric(frame["trade_status"], errors="coerce").fillna(1)
    ma5, ma10 = close.rolling(5).mean(), close.rolling(10).mean()
    ma20, ma30 = close.rolling(20).mean(), close.rolling(30).mean()
    vma5, vma10 = volume.rolling(5).mean(), volume.rolling(10).mean()
    dif = close.ewm(span=12, adjust=False).mean() - close.ewm(span=26, adjust=False).mean()
    dea = dif.ewm(span=9, adjust=False).mean()
    histogram = dif - dea
    returns = close.pct_change()
    return (
        (status == 1)
        & (volume > 0)
        & (ma5.shift(1) > ma10.shift(1))
        & (ma5 < ma10)
        & (ma20 > ma20.shift(1))
        & (ma30 > ma30.shift(1))
        & (vma5.shift(1) > vma10.shift(1))
        & (vma5 * PARAMETERS["volume_ma5_multiplier"] > vma10)
        & (dif > 0)
        & (dea > 0)
        & (histogram > PARAMETERS["macd_hist_min"])
        & (histogram < PARAMETERS["macd_hist_max"])
        & (returns * returns.shift(1) < 0)
        & (open_ < close.shift(1))
    ).fillna(False)


def _prepare_signals(bars: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return pd.DataFrame({code: _signal_series(frame) for code, frame in bars.items()})


def _rank_targets(day: pd.Timestamp, membership: set[str], signals: pd.DataFrame) -> list[str]:
    if day not in signals.index:
        return []
    row = signals.loc[day]
    return sorted(
        code for code, selected in row.items()
        if bool(selected) and code in membership and code.startswith(("sh60", "sz00"))
    )


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
    if not universe_meta.get("complete") or len(days) < 60:
        raise RuntimeError("complete point-in-time universe is required")
    history_manifest = load_history_manifest(
        str(pd.Timestamp(start) - pd.Timedelta(days=240))[:10], end,
        universe_meta.get("sha256", ""), verify_files=False, universe_by_date=universe,
    )
    if not history_manifest.get("complete"):
        raise RuntimeError(history_manifest.get("reason", "stock history is incomplete"))

    codes = sorted(set().union(*(universe[str(day)[:10]] for day in days)))
    bars = _load_daily(codes, start, end)
    signals = _prepare_signals(bars)
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
    signal_days = 0

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

            if len(positions) < PARAMETERS["stock_num"]:
                for code in pending:
                    if code in positions:
                        continue
                    row = _bar(bars.get(code), day) if code in bars else None
                    if not _tradable(row, last_close.get(code, 0.0), "buy"):
                        continue
                    shares = int(cash / float(row["open"]) // 100) * 100
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
                        break
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

        for code, frame in bars.items():
            row = _bar(frame, day)
            if row is not None and float(row["close"]) > 0:
                last_close[code] = float(row["close"])
        if number + 1 < len(days):
            pending = _rank_targets(day, universe[str(day)[:10]], signals)
            signal_days += int(bool(pending))

    metrics = calculate(equity_history, len(close_dates))
    phases = _phase_metrics(equity_history, close_dates)
    failures = excellent_failures(metrics, require_deviation=False)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    return {
        "strategy": "shortline-ma-macd-fixed-rule-causal-replay-v1",
        "source": {
            "url": SOURCE_URL, "commit": SOURCE_COMMIT,
            "blob_sha": SOURCE_BLOB_SHA, "sha256": SOURCE_SHA256,
        },
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True).encode()).hexdigest(),
        "execution": "completed daily signal, next-open fills, A-share lots/limits/fees/slippage",
        "metrics": metrics,
        "phase_metrics": phases,
        "total_return_pct": round((equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3),
        "total_fees": round(total_fees, 2),
        "transactions": transactions,
        "signal_days": signal_days,
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
            "The source's same-open gap filter and trade are shifted to completed signal then next open",
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
        default=DATA_ROOT / "research" / "shortline_ma_macd_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
