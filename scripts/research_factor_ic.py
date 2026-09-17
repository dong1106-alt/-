#!/usr/bin/env python3
"""Measure development-period rank IC for a small causal subset of Qlib Alpha158."""
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
FACTORS = (
    "ret5", "ret20", "ret60", "ma20_bias", "vol20", "rsv20", "price_volume_corr20",
    "overnight5", "intraday1", "ivol20",
)


def _features(frame: pd.DataFrame, volume_multiplier: int = 100,
              market_returns: pd.Series | None = None) -> pd.DataFrame:
    close = frame["close"]
    low20 = frame["low"].rolling(20).min()
    high20 = frame["high"].rolling(20).max()
    returns = close.pct_change()
    output = pd.DataFrame(index=frame.index)
    output["ret5"] = close.pct_change(5)
    output["ret20"] = close.pct_change(20)
    output["ret60"] = close.pct_change(60)
    output["ma20_bias"] = close / close.rolling(20).mean() - 1
    output["vol20"] = returns.rolling(20).std() * math.sqrt(250)
    output["rsv20"] = (close - low20) / (high20 - low20)
    output["price_volume_corr20"] = close.rolling(20).corr(frame["volume"].clip(lower=1).apply(math.log))
    output["overnight5"] = (frame["open"] / close.shift(1) - 1).rolling(5).mean()
    output["intraday1"] = close / frame["open"] - 1
    if market_returns is None:
        output["ivol20"] = float("nan")
    else:
        market = market_returns.reindex(frame.index)
        mean_stock = returns.rolling(20, min_periods=15).mean()
        mean_market = market.rolling(20, min_periods=15).mean()
        covariance = (returns * market).rolling(20, min_periods=15).mean() - mean_stock * mean_market
        stock_variance = (returns * returns).rolling(20, min_periods=15).mean() - mean_stock.pow(2)
        market_variance = (market * market).rolling(20, min_periods=15).mean() - mean_market.pow(2)
        residual_variance = stock_variance - covariance.pow(2) / market_variance.where(market_variance > 0)
        output["ivol20"] = residual_variance.clip(lower=0).pow(0.5)
    # Supervised research label only; it is never included in the signal features.
    future5 = pd.Series(float("nan"), index=frame.index)
    opens = frame["open"].to_numpy()
    entry = pd.Series(opens[1:-5]).where(lambda values: values > 0)
    exit_ = pd.Series(opens[6:]).where(lambda values: values > 0)
    future5.iloc[:-6] = (exit_ / entry - 1).to_numpy()
    output["future5"] = future5
    output["liquidity20"] = (close * frame["volume"] * volume_multiplier).rolling(20).mean()
    return output


def _rank_ic(cross: pd.DataFrame, factor: str) -> float | None:
    pair = cross[[factor, "future5"]].apply(pd.to_numeric, errors="coerce").dropna()
    pair = pair[
        pair[factor].map(math.isfinite) & pair["future5"].map(math.isfinite)
    ]
    if len(pair) < 50:
        return None
    return float(pair[factor].rank(pct=True).corr(pair["future5"].rank(pct=True)))


def _portfolio_row(cross: pd.DataFrame, factor: str, previous: set[str]) -> tuple[dict | None, set[str]]:
    pair = cross[[factor, "future5"]].apply(pd.to_numeric, errors="coerce").dropna()
    pair = pair[pair[factor].map(math.isfinite) & pair["future5"].map(math.isfinite)]
    if len(pair) < 100:
        return None, previous
    count = max(10, len(pair) // 10)
    selected = set(pair.nsmallest(count, factor).index)
    high = pair.nlargest(count, factor)
    turnover = 1.0 if not previous else 1.0 - len(selected & previous) / len(selected)
    return {
        "low_return": float(pair.loc[list(selected), "future5"].mean()),
        "market_return": float(pair["future5"].mean()),
        "high_return": float(high["future5"].mean()),
        "turnover": turnover,
    }, selected


def _portfolio_summary(rows: list[dict]) -> dict:
    if not rows:
        return {}
    gross = pd.Series([row["low_return"] for row in rows])
    market = pd.Series([row["market_return"] for row in rows])
    high = pd.Series([row["high_return"] for row in rows])
    periods_per_year = 50
    annualized = (float((1 + gross).prod()) ** (periods_per_year / len(gross)) - 1) * 100
    return {
        "mean_low_decile_5d_pct": round(float(gross.mean()) * 100, 3),
        "mean_market_5d_pct": round(float(market.mean()) * 100, 3),
        "mean_low_excess_5d_pct": round(float((gross - market).mean()) * 100, 3),
        "mean_low_minus_high_5d_pct": round(float((gross - high).mean()) * 100, 3),
        "gross_annualized_pct": round(annualized, 3),
        "average_one_way_turnover_pct": round(
            sum(row["turnover"] for row in rows) / len(rows) * 100, 2,
        ),
        "periods": len(rows),
    }


def run(start: str, end: str, max_stocks: int) -> dict:
    universe, meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end]
    if not meta.get("complete") or not days:
        raise RuntimeError("complete point-in-time universe is required")
    codes = sorted(set().union(*(universe[str(day)[:10]] for day in days)))
    if max_stocks and len(codes) > max_stocks:
        codes = sorted(codes, key=lambda code: hashlib.sha256(code.encode("ascii")).digest())[:max_stocks]
    history_manifest = json.loads(
        (DATA_ROOT / "universe" / "stock_history_manifest.json").read_text(encoding="utf-8")
    ).get("stocks", {})
    index = pd.read_parquet(DATA_ROOT / "index" / "sh000001.parquet", columns=["date", "close"])
    index["date"] = pd.to_datetime(index["date"])
    index = index.sort_values("date").set_index("date")
    market_returns = index["close"].pct_change()
    feature_by_code = {}
    for number, code in enumerate(codes, 1):
        path = DATA_ROOT / "stocks" / f"{code}.parquet"
        if not path.exists():
            continue
        try:
            frame = pd.read_parquet(
                path, columns=["date", "open", "high", "low", "close", "volume"],
            )
        except Exception:
            continue
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.sort_values("date").set_index("date")
        if len(frame) >= 80:
            multiplier = 1 if str(history_manifest.get(code, {}).get("source", "")).startswith("BaoStock") else 100
            feature_by_code[code] = _features(frame, multiplier, market_returns)
        if number % 500 == 0:
            print(f"[factor-ic] loaded {number}/{len(codes)} files")

    daily_ic = {factor: [] for factor in FACTORS}
    portfolio_rows = {factor: [] for factor in FACTORS}
    previous_selections = {factor: set() for factor in FACTORS}
    for day in days[::5]:
        rows = []
        members = universe[str(day)[:10]]
        for code, features in feature_by_code.items():
            if code not in members or day not in features.index:
                continue
            row = features.loc[day]
            if isinstance(row, pd.DataFrame):
                row = row.iloc[-1]
            if float(row.get("liquidity20", 0) or 0) >= 20_000_000:
                item = row.to_dict()
                item["code"] = code
                rows.append(item)
        cross = pd.DataFrame(rows)
        if len(cross) < 50:
            continue
        cross = cross.set_index("code")
        for factor in FACTORS:
            value = _rank_ic(cross, factor)
            if value is not None:
                daily_ic[factor].append(value)
            portfolio, selected = _portfolio_row(cross, factor, previous_selections[factor])
            if portfolio is not None:
                portfolio_rows[factor].append(portfolio)
                previous_selections[factor] = selected

    summary = {}
    for factor, values in daily_ic.items():
        series = pd.Series(values).dropna()
        mean = float(series.mean()) if len(series) else 0.0
        std = float(series.std()) if len(series) > 1 else 0.0
        summary[factor] = {
            "mean_rank_ic": round(mean, 4),
            "ic_ir": round(mean / std * math.sqrt(len(series)), 3) if std > 0 else 0.0,
            "positive_rate": round(float((series > 0).mean()), 3) if len(series) else 0.0,
            "observations": len(series),
        }
    return {
        "source": "Qlib Alpha158 causal OHLCV subset; next-open executable label",
        "development_period": {"start": start, "end": end},
        "sampled_stocks": len(feature_by_code),
        "forward_horizon_days": 5,
        "factor_ic": summary,
        "gross_factor_portfolios": {
            factor: _portfolio_summary(portfolio_rows[factor]) for factor in FACTORS
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-03")
    parser.add_argument("--end", default="2024-12-31")
    parser.add_argument("--max-stocks", type=int, default=800)
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "research_results" / "factor_ic.json")
    args = parser.parse_args()
    result = run(args.start, args.end, args.max_stocks)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
