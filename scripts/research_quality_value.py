#!/usr/bin/env python3
"""Causal quality-value IC and gross-return screen on consumed data."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path

import pandas as pd

from point_in_time_universe import load_universe

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
PHASES = (
    ("2011-2013", "2011-01-04", "2013-12-31"),
    ("2014-2015", "2014-01-02", "2015-12-31"),
    ("2016-2017", "2016-01-04", "2017-12-29"),
)
FINANCIAL_MARKERS = ("银行", "保险", "证券", "多元金融", "信托")
RULE = {
    "rebalance": "every 20 trading days",
    "label": "next stock open to 21st stock open",
    "minimum_average_value20": 20_000_000,
    "eligibility": "positive EPS/BPS, non-financial industry, all six fields present",
    "factors_equal_weight": (
        "ROE", "net-profit YoY", "low debt ratio", "OCF-per-share/EPS",
        "EPS/unadjusted price", "BPS/unadjusted price",
    ),
    "selection": "highest score decile",
}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _latest_as_of(fundamentals: pd.DataFrame, day: pd.Timestamp) -> pd.DataFrame:
    available = fundamentals[fundamentals["available_date"] <= day]
    if available.empty:
        return available
    return (
        available.sort_values(["report_date", "available_date", "code"])
        .drop_duplicates("code", keep="last").set_index("code")
    )


def _score(cross: pd.DataFrame) -> pd.DataFrame:
    frame = cross.copy()
    numeric = (
        "roe", "profit_yoy", "debt_ratio", "ocf_per_share", "eps", "bps", "price",
    )
    for column in numeric:
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    financial = pd.Series(
        [
            any(marker in str(name) for marker in FINANCIAL_MARKERS)
            for name in frame["industry"].fillna("").tolist()
        ],
        index=frame.index,
        dtype=bool,
    )
    frame = frame[
        ~financial & (frame["eps"] > 0) & (frame["bps"] > 0) & (frame["price"] > 0)
    ].dropna(subset=list(numeric))
    if frame.empty:
        return frame
    frame["cash_quality"] = (frame["ocf_per_share"] / frame["eps"]).clip(-3, 3)
    frame["earnings_yield"] = frame["eps"] / frame["price"]
    frame["book_to_price"] = frame["bps"] / frame["price"]
    factors = {
        "roe": True, "profit_yoy": True, "debt_ratio": False,
        "cash_quality": True, "earnings_yield": True, "book_to_price": True,
    }
    ranks = []
    for column, higher_is_better in factors.items():
        values = frame[column] if higher_is_better else -frame[column]
        ranks.append(values.rank(pct=True))
    frame["quality_value_score"] = pd.concat(ranks, axis=1).mean(axis=1)
    return frame


def _sample_days(universe: dict[str, set[str]]) -> tuple[list[pd.Timestamp], dict[str, str]]:
    sample_days = []
    phases = {}
    all_days = [pd.Timestamp(day) for day in sorted(universe)]
    for name, start, end in PHASES:
        days = [day for day in all_days if start <= str(day)[:10] <= end]
        chosen = days[:-21:20]
        sample_days.extend(chosen)
        phases.update({str(day)[:10]: name for day in chosen})
    return sample_days, phases


def _future_open_return(frame: pd.DataFrame, horizon: int = 20) -> pd.Series:
    """Research label: next open to the open after ``horizon`` held bars."""
    output = pd.Series(float("nan"), index=frame.index)
    opens = frame["open"].to_numpy()
    if len(opens) <= horizon + 1:
        return output
    entry = pd.Series(opens[1:-horizon]).where(lambda values: values > 0)
    exit_ = pd.Series(opens[horizon + 1:]).where(lambda values: values > 0)
    output.iloc[:-(horizon + 1)] = (exit_ / entry - 1).to_numpy()
    return output


def _load_fundamentals() -> tuple[pd.DataFrame, dict]:
    root = DATA_ROOT / "fundamentals"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    frames = []
    for report_date, entry in sorted((manifest.get("periods") or {}).items()):
        path = Path(entry["path"])
        if not path.exists() or _sha256(path) != entry.get("sha256"):
            raise RuntimeError(f"fundamental checksum mismatch: {report_date}")
        frame = pd.read_parquet(path)
        if set(frame["report_date"].dt.strftime("%Y-%m-%d")) != {report_date}:
            raise RuntimeError(f"fundamental report date mismatch: {report_date}")
        frames.append(frame[frame["complete_factors"]])
    if not frames:
        raise RuntimeError("fundamental data missing")
    return pd.concat(frames, ignore_index=True), manifest


def _load_market_rows(codes: list[str], sample_days: list[pd.Timestamp]) -> dict[str, list[dict]]:
    rows = {str(day)[:10]: [] for day in sample_days}
    sample_set = set(sample_days)
    history_manifest = json.loads(
        (DATA_ROOT / "universe" / "stock_history_manifest.json").read_text(encoding="utf-8")
    ).get("stocks", {})
    liquidity_manifest = json.loads(
        (DATA_ROOT / "liquidity" / "manifest.json").read_text(encoding="utf-8")
    )
    liquidity_entries = liquidity_manifest.get("stocks") or {}
    for number, code in enumerate(codes, 1):
        stock_path = DATA_ROOT / "stocks" / f"{code}.parquet"
        liquidity_path = DATA_ROOT / "liquidity" / f"{code}.parquet"
        liquidity_entry = liquidity_entries.get(code) or {}
        if (
            not stock_path.exists() or liquidity_entry.get("status") != "complete"
            or not liquidity_path.exists() or _sha256(liquidity_path) != liquidity_entry.get("sha256")
        ):
            continue
        frame = pd.read_parquet(stock_path, columns=["date", "open", "close", "volume"])
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.sort_values("date")
        multiplier = (
            1 if str(history_manifest.get(code, {}).get("source", "")).startswith("BaoStock") else 100
        )
        frame["average_value20"] = (
            frame["close"] * frame["volume"] * multiplier
        ).rolling(20).mean()
        frame["future20"] = _future_open_return(frame)
        sample = frame[frame["date"].isin(sample_set)][
            ["date", "average_value20", "future20"]
        ].sort_values("date")
        if sample.empty:
            continue
        liquidity = pd.read_parquet(liquidity_path, columns=["date", "close"])
        liquidity["date"] = pd.to_datetime(liquidity["date"])
        liquidity = liquidity.sort_values("date").rename(columns={"close": "price"})
        sample = pd.merge_asof(
            sample, liquidity, on="date", direction="backward", tolerance=pd.Timedelta(days=10),
        )
        for row in sample.itertuples(index=False):
            rows[str(row.date)[:10]].append({
                "code": code, "average_value20": row.average_value20,
                "future20": row.future20, "price": row.price,
            })
        if number % 500 == 0:
            print(f"[quality-value] loaded {number}/{len(codes)} stocks", flush=True)
    rows["_liquidity_manifest"] = liquidity_manifest
    return rows


def _summarize(observations: list[dict]) -> dict:
    if not observations:
        return {"observations": 0}
    frame = pd.DataFrame(observations)
    top = frame["top_return"]
    periods_per_year = 250 / 20
    annualized = (float((1 + top).prod()) ** (periods_per_year / len(top)) - 1) * 100
    return {
        "observations": len(frame),
        "mean_rank_ic": round(float(frame["rank_ic"].mean()), 4),
        "positive_ic_rate": round(float((frame["rank_ic"] > 0).mean()), 3),
        "mean_top_20d_pct": round(float(top.mean()) * 100, 3),
        "mean_market_20d_pct": round(float(frame["market_return"].mean()) * 100, 3),
        "mean_top_excess_20d_pct": round(
            float((top - frame["market_return"]).mean()) * 100, 3,
        ),
        "mean_top_minus_bottom_20d_pct": round(
            float((top - frame["bottom_return"]).mean()) * 100, 3,
        ),
        "gross_annualized_pct": round(annualized, 3),
        "average_one_way_turnover_pct": round(float(frame["turnover"].mean()) * 100, 2),
        "average_cross_section": round(float(frame["cross_section"].mean()), 1),
        "average_eligible_coverage_pct": round(float(frame["eligible_coverage"].mean()) * 100, 2),
    }


def run() -> dict:
    universe, universe_meta = load_universe()
    if not universe_meta.get("complete"):
        raise RuntimeError("complete point-in-time universe is required")
    sample_days, day_phase = _sample_days(universe)
    codes = sorted(set().union(*(universe[str(day)[:10]] for day in sample_days)))
    fundamentals, fundamental_manifest = _load_fundamentals()
    market_rows = _load_market_rows(codes, sample_days)
    liquidity_manifest = market_rows.pop("_liquidity_manifest")
    observations = {name: [] for name, _, _ in PHASES}
    previous = {name: set() for name, _, _ in PHASES}
    for day in sample_days:
        day_key = str(day)[:10]
        phase = day_phase[day_key]
        market = pd.DataFrame(market_rows.get(day_key) or [])
        if market.empty:
            continue
        market = market[
            market["code"].isin(universe[day_key])
            & (market["average_value20"] >= RULE["minimum_average_value20"])
        ].dropna(subset=["future20", "price"])
        latest = _latest_as_of(fundamentals, day)
        if market.empty or latest.empty:
            continue
        cross = market.set_index("code").join(latest, how="inner")
        eligible = _score(cross)
        if len(eligible) < 100:
            continue
        score = eligible["quality_value_score"]
        future = eligible["future20"]
        rank_ic = float(score.rank(pct=True).corr(future.rank(pct=True)))
        if not math.isfinite(rank_ic):
            continue
        count = max(10, len(eligible) // 10)
        selected = set(score.nlargest(count).index)
        bottom = set(score.nsmallest(count).index)
        turnover = 1.0 if not previous[phase] else 1.0 - len(selected & previous[phase]) / len(selected)
        observations[phase].append({
            "rank_ic": rank_ic,
            "top_return": float(future.loc[list(selected)].mean()),
            "bottom_return": float(future.loc[list(bottom)].mean()),
            "market_return": float(future.mean()),
            "turnover": turnover,
            "cross_section": len(eligible),
            "eligible_coverage": len(eligible) / len(universe[day_key]),
        })
        previous[phase] = selected

    phases = {name: _summarize(observations[name]) for name, _, _ in PHASES}
    stable = all(
        row.get("mean_rank_ic", 0) > 0
        and row.get("mean_top_excess_20d_pct", 0) > 0
        and row.get("mean_top_minus_bottom_20d_pct", 0) > 0
        for row in phases.values()
    )
    limitations = ["2011-2017 is consumed development data and cannot be release evidence"]
    if not fundamental_manifest.get("complete"):
        limitations.append(
            f"fundamental coverage {float(fundamental_manifest.get('coverage', 0)):.2%} is below 100%"
        )
    if not liquidity_manifest.get("complete"):
        limitations.append(
            f"unadjusted-price coverage {float(liquidity_manifest.get('coverage', 0)):.2%} is below 100%"
        )
    return {
        "strategy": "fixed-equal-weight-quality-value-v1",
        "rule": RULE,
        "rule_sha256": hashlib.sha256(
            json.dumps(RULE, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest(),
        "revision_policy": fundamental_manifest.get("revision_policy"),
        "phases": phases,
        "stable_across_phases": stable,
        "decision": "gross_screen_pass" if stable else "research_rejected",
        "promotion_allowed": bool(
            stable and fundamental_manifest.get("complete") and liquidity_manifest.get("complete")
        ),
        "limitations": limitations,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output", type=Path,
        default=DATA_ROOT / "research" / "quality_value_ic_dev_2011_2017.json",
    )
    args = parser.parse_args()
    result = run()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
