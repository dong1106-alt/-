#!/usr/bin/env python3
"""Causal replay of the public value/low-volatility strategy."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS, calculate, excellent_failures
from point_in_time_universe import load_universe
from research_quality_value import _latest_as_of
from research_smallcap_breadth import _bar, _tradable
from research_trend_v5 import _load_memberships
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
INDEX_PATH = DATA_ROOT / "hs300" / "sh000300.parquet"
SOURCE_COMMIT = "f33c78ef6f5d203059785c5d7e51db2bf01a54ba"
SOURCE_BLOB_SHA = "2d112d1485fa77373531918b1427fa67bd3231f3"
SOURCE_SHA256 = "0eb848e4a857a394041ac7f90eafe13dfc2ef119da261c692aff0f240fc89951"
SOURCE_URL = (
    "https://github.com/ShenzhenLime/factor_mining/blob/"
    f"{SOURCE_COMMIT}/ref/ref_code/124/0208/2021%E5%B9%B4%E5%BA%A6%E7%B2%BE%E9%80%89"
    "%E7%AD%96%E7%95%A5/5.%E4%BB%B7%E5%80%BC%E4%BD%8E%E6%B3%A2%EF%BC%88%E4%B8%8B"
    "%EF%BC%89--%E5%8D%81%E5%B9%B4%E5%8D%81%E5%80%8D%EF%BC%882020%E6%8B%9C%E5%B9%B4"
    "%EF%BC%89.py"
)
PARAMETERS = {
    "universe": "historical HS300",
    "stock_num": 10,
    "maximum_pe": 20.0,
    "minimum_pb_to_pe": 0.1,
    "volatility_days": 241,
    "index_ma_days": 61,
    "drawdown_trigger": 0.10,
    "drawdown_stock_ratio": 0.50,
    "treasury": "000012.XSHG",
    "treasury_fallback": "cash when historical treasury index is unavailable",
    "rebalance": "daily open",
    "signal_cutoff": "previous trading day close and point-in-time fundamentals",
    "fill": "next open, T+1",
    "source_fixed_slippage": 0.02,
}
PHASES = (
    ("2011-2013", "2011-01-04", "2013-12-31"),
    ("2014-2015", "2014-01-02", "2015-12-31"),
    ("2016-2017", "2016-01-04", "2017-12-29"),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _add_ttm_eps(frame: pd.DataFrame) -> pd.DataFrame:
    output = []
    for _, group in frame.sort_values(["code", "report_date"]).groupby("code"):
        reports = {pd.Timestamp(row.report_date): row for row in group.itertuples(index=False)}
        for report_date, current in reports.items():
            required = [current]
            if report_date.month == 12:
                ttm_eps = current.eps
            else:
                prior_same = reports.get(report_date - pd.DateOffset(years=1))
                prior_annual = reports.get(pd.Timestamp(report_date.year - 1, 12, 31))
                if prior_same is None or prior_annual is None:
                    continue
                required.extend((prior_same, prior_annual))
                ttm_eps = current.eps + prior_annual.eps - prior_same.eps
            if pd.isna(ttm_eps) or pd.isna(current.bps):
                continue
            row = current._asdict()
            row["available_date"] = max(pd.Timestamp(item.available_date) for item in required)
            row["ttm_eps"] = float(ttm_eps)
            output.append(row)
    return pd.DataFrame(output).sort_values(["available_date", "code"]).reset_index(drop=True)


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
        frames.append(frame.dropna(subset=["eps", "bps", "available_date"]))
    if not frames:
        raise RuntimeError("fundamental data missing")
    return _add_ttm_eps(pd.concat(frames, ignore_index=True)), manifest


def _load_daily(codes: list[str], end: str) -> dict[str, pd.DataFrame]:
    bars = {}
    for number, code in enumerate(codes, 1):
        path = DATA_ROOT / "stocks" / f"{code}.parquet"
        if not path.exists():
            continue
        frame = pd.read_parquet(
            path, columns=["date", "open", "high", "low", "close", "volume", "trade_status"],
        )
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame[
            (frame["date"] >= pd.Timestamp("2010-01-01"))
            & (frame["date"] <= pd.Timestamp(end))
        ].sort_values("date")
        if len(frame) >= PARAMETERS["volatility_days"]:
            bars[code] = frame.set_index("date")
        if number % 300 == 0:
            print(f"[value-low-vol] loaded daily {number}/{len(codes)}", flush=True)
    return bars


def _select_weights(day: pd.Timestamp, membership: set[str], bars: dict,
                    fundamentals: pd.DataFrame) -> dict[str, float]:
    latest = _latest_as_of(fundamentals, day)
    ranked = []
    volatilities = {}
    for code in membership:
        if code not in latest.index:
            continue
        frame = bars.get(code)
        closes = frame.loc[:day, "close"].tail(PARAMETERS["volatility_days"]) if frame is not None else []
        if len(closes) < PARAMETERS["volatility_days"]:
            continue
        closes = pd.to_numeric(closes, errors="coerce")
        if closes.isna().any() or (closes <= 0).any():
            continue
        price = float(closes.iloc[-1])
        eps = float(latest.loc[code, "ttm_eps"])
        bps = float(latest.loc[code, "bps"])
        if price <= 0 or eps <= 0 or bps <= 0:
            continue
        pe = price / eps
        pb_to_pe = eps / bps
        volatility = float(closes.pct_change().dropna().std())
        if (
            pe >= PARAMETERS["maximum_pe"]
            or pb_to_pe <= PARAMETERS["minimum_pb_to_pe"]
            or pd.isna(volatility) or volatility <= 0
        ):
            continue
        ranked.append((code, pe))
        volatilities[code] = volatility
    selected = [code for code, _ in sorted(ranked, key=lambda item: (item[1], item[0]))][
        :PARAMETERS["stock_num"]
    ]
    inverse = {code: 1.0 / volatilities[code] for code in selected}
    total = sum(inverse.values())
    return {code: value / total for code, value in inverse.items()} if total else {}


def _target_weights(selected: dict[str, float], held: set[str], market_on: bool,
                    drawdown: float) -> dict[str, float]:
    weights = selected if market_on else {code: weight for code, weight in selected.items() if code in held}
    ratio = PARAMETERS["drawdown_stock_ratio"] if drawdown > PARAMETERS["drawdown_trigger"] else 1.0
    return {code: weight * ratio for code, weight in weights.items()}


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
    if not universe_meta.get("complete") or len(days) < 180:
        raise RuntimeError("complete historical trading calendar is required")
    memberships, membership_manifest = _load_memberships(days)
    codes = sorted(set().union(*memberships.values()))
    bars = _load_daily(codes, end)
    fundamentals, fundamental_manifest = _load_fundamentals()
    index = pd.read_parquet(INDEX_PATH)
    index["date"] = pd.to_datetime(index["date"])
    index = index.sort_values("date").set_index("date")
    if any(day not in index.index for day in days):
        raise RuntimeError("HS300 index coverage mismatch")
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, int] = {}
    last_close: dict[str, float] = {}
    pending: dict[str, float] | None = None
    equity_history = []
    close_dates = []
    total_fees = 0.0
    transactions = 0
    peak = cash
    minimum_cash = cash
    maximum_positions = 0
    risk_off_days = 0
    reduced_days = 0

    def marked_price(row, field: str, fallback: float) -> float:
        value = float(row[field]) if row is not None and pd.notna(row.get(field)) else 0.0
        return value if value > 0 else fallback

    for number, day in enumerate(days):
        if pending is not None:
            open_equity = cash
            for code, shares in positions.items():
                row = _bar(bars.get(code), day) if code in bars else None
                open_equity += shares * marked_price(row, "open", last_close.get(code, 0.0))
            desired = {}
            for code, weight in pending.items():
                row = _bar(bars.get(code), day) if code in bars else None
                if row is not None and float(row["open"]) > 0:
                    desired[code] = int(open_equity * weight / float(row["open"]) // 100) * 100

            for code, current in list(positions.items()):
                target = desired.get(code, 0)
                shares = current - target
                row = _bar(bars.get(code), day) if code in bars else None
                if shares < 100 or not _tradable(row, last_close.get(code, 0.0), "sell"):
                    continue
                cap = None
                _, proceeds, commission, tax = calc_trade_cost(
                    float(row["open"]), shares, "sell", trade_cfg, market_cap=cap,
                )
                cash += proceeds
                total_fees += commission + tax
                transactions += 1
                if target:
                    positions[code] = target
                else:
                    positions.pop(code)
                    close_dates.append(str(day)[:10])

            for code, target in desired.items():
                shares = target - positions.get(code, 0)
                row = _bar(bars.get(code), day) if code in bars else None
                if shares < 100 or not _tradable(row, last_close.get(code, 0.0), "buy"):
                    continue
                while shares >= 100:
                    _, cost, commission, tax = calc_trade_cost(
                        float(row["open"]), shares, "buy", trade_cfg,
                    )
                    if cost <= cash:
                        break
                    shares -= 100
                if shares >= 100:
                    cash -= cost
                    total_fees += commission + tax
                    transactions += 1
                    positions[code] = positions.get(code, 0) + shares
            pending = None

        if cash < -0.01 or any(shares <= 0 or shares % 100 for shares in positions.values()):
            invalid = {code: shares for code, shares in positions.items()
                       if shares <= 0 or shares % 100}
            raise RuntimeError(
                f"portfolio accounting invariant failed: day={day.date()} cash={cash} invalid={invalid}"
            )
        value = cash
        for code, shares in positions.items():
            row = _bar(bars.get(code), day) if code in bars else None
            value += shares * marked_price(row, "close", last_close.get(code, 0.0))
        peak = max(peak, value)
        drawdown = 1.0 - value / peak
        equity_history.append({"date": str(day)[:10], "equity": round(value, 2)})
        minimum_cash = min(minimum_cash, cash)
        maximum_positions = max(maximum_positions, len(positions))

        for code, frame in bars.items():
            row = _bar(frame, day)
            if row is not None and pd.notna(row.get("close")) and float(row["close"]) > 0:
                last_close[code] = float(row["close"])
        if number + 1 >= len(days):
            continue
        selected = _select_weights(day, memberships[str(day)[:10]], bars, fundamentals)
        index_closes = index.loc[:day, "close"].tail(PARAMETERS["index_ma_days"])
        market_on = len(index_closes) == PARAMETERS["index_ma_days"] and (
            float(index_closes.iloc[-1]) >= float(index_closes.mean())
        )
        risk_off_days += int(not market_on)
        reduced_days += int(drawdown > PARAMETERS["drawdown_trigger"])
        pending = _target_weights(selected, set(positions), market_on, drawdown)

    metrics = calculate(equity_history, len(close_dates))
    phases = _phase_metrics(equity_history, close_dates)
    failures = excellent_failures(metrics, require_deviation=False)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    if not fundamental_manifest.get("complete"):
        failures.append(
            f"基本面数据覆盖{float(fundamental_manifest.get('coverage', 0)):.4%}，未达到100%"
        )
    failures.append("缺少000012.XSHG历史国债指数，剩余仓位按现金保守替代")
    return {
        "strategy": "value-low-volatility-fixed-rule-causal-replay-v1",
        "source": {
            "url": SOURCE_URL, "commit": SOURCE_COMMIT,
            "blob_sha": SOURCE_BLOB_SHA, "sha256": SOURCE_SHA256,
        },
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "rule_sha256": hashlib.sha256(json.dumps(PARAMETERS, sort_keys=True).encode()).hexdigest(),
        "execution": "prior-close signal, next-open fills, A-share lots/limits/fees/slippage",
        "metrics": metrics,
        "phase_metrics": phases,
        "total_return_pct": round((equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3),
        "total_fees": round(total_fees, 2),
        "transactions": transactions,
        "minimum_cash": round(minimum_cash, 2),
        "maximum_positions": maximum_positions,
        "risk_off_days": risk_off_days,
        "reduced_days": reduced_days,
        "input_manifest": {
            "memberships": membership_manifest,
            "fundamental_coverage": fundamental_manifest.get("coverage"),
        },
        "hard_gate": {"thresholds": EXCELLENT_THRESHOLDS, "development_failures": failures},
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "Quarterly point-in-time EPS is converted to TTM before PE ranking",
            "Missing treasury-index returns are conservatively represented by cash",
            "Historical ST identity is unavailable; conservative 5% fill limits are used",
            "Incomplete fundamentals prevent shadow or release promotion",
            "2011-2017 is consumed development data; 2002-2008 unseen data is not read",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "value_low_volatility_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
