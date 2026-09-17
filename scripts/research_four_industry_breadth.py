#!/usr/bin/env python3
"""Causal approximation of the public four-industry breadth strategy."""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from collections import Counter, defaultdict
from pathlib import Path

import pandas as pd

from performance_metrics import EXCELLENT_THRESHOLDS, calculate, excellent_failures
from point_in_time_universe import load_universe
from research_smallcap_breadth import _bar, _load_daily, _load_liquidity, _tradable
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
PARAMETERS = {
    "stock_num": 10,
    "roe_min": 0.15,
    "roa_ttm_min": 0.10,
    "minimum_listing_days": 375,
    "liquidity_max_age_days": 10,
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


def _risk_industry(label: str) -> str | None:
    value = str(label or "")
    if value.startswith("J66") or "银行业" in value:
        return "bank"
    if value.startswith("C31") or "黑色金属冶炼" in value:
        return "steel"
    if value.startswith("C32") or "有色金属冶炼" in value:
        return "nonferrous"
    if value.startswith("B06") or "煤炭开采" in value or "煤炭采选" in value:
        return "coal"
    return None


def _add_ttm_roa(frame: pd.DataFrame) -> pd.DataFrame:
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
            else:
                prior_annual = reports.get(pd.Timestamp(report_date.year - 1, 12, 31))
                if prior_annual is None:
                    continue
                required.append(prior_annual)
                ttm_profit = current.net_profit + prior_annual.net_profit - prior_same.net_profit
            values = [ttm_profit, current.total_assets, prior_same.total_assets]
            if any(pd.isna(value) for value in values):
                continue
            average_assets = (float(current.total_assets) + float(prior_same.total_assets)) / 2
            if average_assets <= 0:
                continue
            row = current._asdict()
            row["available_date"] = max(pd.Timestamp(item.available_date) for item in required)
            row["roa_ttm"] = float(ttm_profit) / average_assets
            output.append(row)
    return pd.DataFrame(output).sort_values(["available_date", "code"]).reset_index(drop=True)


def _latest_as_of(frame: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    available = frame[frame["available_date"] <= day]
    if available.empty:
        return available
    return (
        available.sort_values(["report_date", "available_date", "code"])
        .drop_duplicates("code", keep="last").set_index("code")
    )


def _industry_breadth(membership, bars, industries: dict[str, str], day: pd.Timestamp):
    counts = defaultdict(lambda: [0, 0])
    for code in membership:
        label = industries.get(code, "")
        row = _bar(bars.get(code), day) if code in bars else None
        if not label or row is None or pd.isna(row.get("ma20")):
            continue
        counts[label][1] += 1
        counts[label][0] += int(float(row["close"]) > float(row["ma20"]))
    if not counts:
        return None, {}, 0
    ratios = {label: above / total for label, (above, total) in counts.items()}
    top = max(sorted(ratios), key=ratios.get)
    return top, ratios, sum(total for _, total in counts.values())


def _select_targets(day: pd.Timestamp, membership, fundamentals: pd.DataFrame,
                    liquidity: dict, listed: dict[str, pd.Timestamp]) -> list[str]:
    rows = []
    for code in membership:
        if not code.startswith("sz002") or code not in fundamentals.index:
            continue
        ipo = listed.get(code)
        if ipo is None or (day - ipo).days < PARAMETERS["minimum_listing_days"]:
            continue
        row = fundamentals.loc[code]
        event = liquidity.get(code)
        if (
            event is None or (day - event[0]).days > PARAMETERS["liquidity_max_age_days"]
            or float(row["roe"]) <= PARAMETERS["roe_min"]
            or float(row["roa_ttm"]) <= PARAMETERS["roa_ttm_min"]
            or float(event[1]) <= 0
        ):
            continue
        rows.append((code, float(event[1])))
    return [code for code, _ in sorted(rows, key=lambda item: (item[1], item[0]))][
        :PARAMETERS["stock_num"]
    ]


def _closed_at_limit(row, previous_close: float) -> bool:
    return bool(row is not None and previous_close > 0 and float(row["close"]) / previous_close >= 1.048)


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
        frames.append(frame.dropna(subset=["roe", "net_profit", "total_assets", "available_date"]))
    if not frames:
        raise RuntimeError("fundamental data missing")
    return _add_ttm_roa(pd.concat(frames, ignore_index=True)), manifest


def _load_industries() -> tuple[dict[str, dict[str, str]], dict]:
    root = DATA_ROOT / "industries"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    snapshots = {}
    for snapshot_date, entry in sorted((manifest.get("snapshots") or {}).items()):
        path = Path(entry["path"])
        if not path.exists() or _sha256(path) != entry.get("sha256"):
            raise RuntimeError(f"industry checksum mismatch: {snapshot_date}")
        frame = pd.read_parquet(path, columns=["snapshot_date", "code", "industry"])
        actual = set(pd.to_datetime(frame["snapshot_date"]).dt.strftime("%Y-%m-%d"))
        if actual != {snapshot_date}:
            raise RuntimeError(f"industry snapshot date mismatch: {snapshot_date}")
        snapshots[snapshot_date] = dict(zip(frame["code"], frame["industry"]))
    if not snapshots:
        raise RuntimeError("industry snapshots missing")
    return snapshots, manifest


def _load_listing_dates() -> dict[str, pd.Timestamp]:
    path = DATA_ROOT / "universe" / "point_in_time.json.gz"
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        payload = json.load(fh)
    return {
        row["code"].replace(".", ""): pd.Timestamp(row["ipo_date"])
        for row in payload.get("listings", []) if row.get("ipo_date")
    }


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
    fundamentals, fundamental_manifest = _load_fundamentals()
    industry_snapshots, industry_manifest = _load_industries()
    listed = _load_listing_dates()
    liquidity_events, liquidity_manifest = _load_liquidity(codes, start, end)
    liquidity_days = sorted(liquidity_events)
    liquidity_number = 0
    current_liquidity = {}
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, int] = {}
    target_holdings: set[str] = set()
    pending_target: list[str] | None = None
    pending_protected: set[str] = set()
    deferred_sells: set[str] = set()
    last_close: dict[str, float] = {}
    equity_history = []
    closed_trade_dates = []
    total_fees = 0.0
    top_industries = Counter()
    missing_snapshots = []

    def sell(code: str, day: pd.Timestamp, row) -> bool:
        nonlocal cash, total_fees
        if code not in positions or not _tradable(row, last_close.get(code, 0.0), "sell"):
            return False
        cap = current_liquidity.get(code, (day, 0, 0))[1] / 1e8
        _, proceeds, commission, tax = calc_trade_cost(
            float(row["open"]), positions.pop(code), "sell", trade_cfg, market_cap=cap,
        )
        cash += proceeds
        total_fees += commission + tax
        closed_trade_dates.append(str(day)[:10])
        return True

    for day_number, day in enumerate(days):
        while liquidity_number < len(liquidity_days) and liquidity_days[liquidity_number] <= day:
            event_day = liquidity_days[liquidity_number]
            for code, cap, turn in liquidity_events[event_day]:
                current_liquidity[code] = (event_day, cap, turn)
            liquidity_number += 1

        if pending_target is not None:
            deferred_sells -= set(pending_target)
        for code in list(deferred_sells):
            row = _bar(bars.get(code), day) if code in bars else None
            if sell(code, day, row):
                deferred_sells.remove(code)

        if pending_target is not None:
            target_holdings = set(pending_target)
            for code in list(positions):
                if code in target_holdings:
                    continue
                if code in pending_protected:
                    continue
                row = _bar(bars.get(code), day) if code in bars else None
                if not sell(code, day, row):
                    deferred_sells.add(code)

            buy_codes = [code for code in pending_target if code not in positions]
            slots = min(len(buy_codes), PARAMETERS["stock_num"] - len(positions))
            slot_value = cash / slots if slots > 0 else 0.0
            for code in buy_codes[:slots]:
                row = _bar(bars.get(code), day) if code in bars else None
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
                    total_fees += commission + tax
                    positions[code] = shares
            pending_target = None
            pending_protected = set()

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
        for code in positions:
            if code in target_holdings:
                deferred_sells.discard(code)
                continue
            row = _bar(bars.get(code), day) if code in bars else None
            if not _closed_at_limit(row, previous_closes.get(code, 0.0)):
                deferred_sells.add(code)

        next_day = days[day_number + 1] if day_number + 1 < len(days) else None
        if next_day is not None and day.isocalendar()[:2] == next_day.isocalendar()[:2]:
            continue
        snapshot_date = str(day)[:10]
        industries = industry_snapshots.get(snapshot_date)
        if industries is None:
            missing_snapshots.append(snapshot_date)
            pending_target = []
            pending_protected = {
                code for code in positions
                if _closed_at_limit(_bar(bars.get(code), day), previous_closes.get(code, 0.0))
            }
            continue
        top, _, breadth_count = _industry_breadth(
            universe[snapshot_date], bars, industries, day,
        )
        if top is None or breadth_count == 0:
            pending_target = []
            pending_protected = {
                code for code in positions
                if _closed_at_limit(_bar(bars.get(code), day), previous_closes.get(code, 0.0))
            }
            continue
        top_industries[top] += 1
        if _risk_industry(top) is not None:
            pending_target = []
            pending_protected = {
                code for code in positions
                if _closed_at_limit(_bar(bars.get(code), day), previous_closes.get(code, 0.0))
            }
            continue
        latest = _latest_as_of(fundamentals, day)
        pending_target = _select_targets(
            day, universe[snapshot_date], latest, current_liquidity, listed,
        )
        pending_protected = {
            code for code in positions if code not in pending_target
            and _closed_at_limit(_bar(bars.get(code), day), previous_closes.get(code, 0.0))
        }

    metrics = calculate(equity_history, len(closed_trade_dates))
    failures = excellent_failures(metrics, require_deviation=False)
    phases = _phase_metrics(equity_history, closed_trade_dates)
    if any(row["annual_return_pct"] <= 0 or row["sharpe"] <= 0 for row in phases.values()):
        failures.append("at least one development phase has non-positive annual return or Sharpe")
    for name, manifest, label in (
        ("fundamental", fundamental_manifest, "基本面"),
        ("industry", industry_manifest, "行业"),
        ("liquidity", liquidity_manifest, "流通市值"),
    ):
        if not manifest.get("complete"):
            failures.append(
                f"{label}数据覆盖{float(manifest.get('coverage', 0)):.2%}，未达到100%"
            )
    if missing_snapshots:
        failures.append(f"缺少{len(missing_snapshots)}个周频行业快照")
    return {
        "strategy": "github-four-industry-breadth-causal-approximation-v1",
        "source": "https://github.com/roapi-cloud/jqdata-akshare-backtrader-utility/blob/main/strategies/05%20四大搅屎棍策略.txt",
        "source_blob": "e92e6f0b7d89a8be8c7c2fa25f96b4d8f0c943d4",
        "period": {"start": start, "end": end},
        "parameters": PARAMETERS,
        "execution": "week-end close signal, next tradable open fill, A-share T+1",
        "metrics": metrics,
        "phase_metrics": phases,
        "total_return_pct": round(
            (equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3,
        ),
        "total_fees": round(total_fees, 2),
        "top_industry_weeks": dict(top_industries.most_common()),
        "fundamental_coverage": fundamental_manifest.get("coverage"),
        "industry_coverage": industry_manifest.get("coverage"),
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
            "causal CSRC classifications approximate the source strategy's SW L1 industries",
            "weekly float market cap approximates the source strategy's total market cap ranking",
            "the sz002 code prefix approximates historical 399101 index membership",
            "a next-open sale approximates the source strategy's intraday limit-up-open sale",
            "historical ST identity is unavailable; conservative 5% fill limits are used",
            "any incomplete input manifest prevents shadow or release promotion",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "four_industry_breadth_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run(args.start, args.end)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
