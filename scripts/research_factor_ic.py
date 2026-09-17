#!/usr/bin/env python3
"""Measure development-period rank IC for a small causal subset of Qlib Alpha158."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

import pandas as pd

from point_in_time_universe import load_universe

ROOT = Path(__file__).resolve().parent.parent


def _features(frame: pd.DataFrame) -> pd.DataFrame:
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
    # Supervised research label only; it is never included in the signal features.
    future5 = pd.Series(float("nan"), index=frame.index)
    future5.iloc[:-5] = close.to_numpy()[5:] / close.to_numpy()[:-5] - 1
    output["future5"] = future5
    # Tencent/BaoStock daily volume is in board lots (100 shares).
    output["liquidity20"] = (close * frame["volume"] * 100).rolling(20).mean()
    return output


def run(start: str, end: str, max_stocks: int) -> dict:
    universe, meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end]
    if not meta.get("complete") or not days:
        raise RuntimeError("complete point-in-time universe is required")
    codes = sorted(set().union(*(universe[str(day)[:10]] for day in days)))
    if max_stocks and len(codes) > max_stocks:
        codes = sorted(codes, key=lambda code: hashlib.sha256(code.encode("ascii")).digest())[:max_stocks]
    feature_by_code = {}
    for number, code in enumerate(codes, 1):
        path = ROOT / "data" / "stocks" / f"{code}.parquet"
        if not path.exists():
            continue
        try:
            frame = pd.read_parquet(path, columns=["date", "high", "low", "close", "volume"])
        except Exception:
            continue
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame.sort_values("date").set_index("date")
        if len(frame) >= 80:
            feature_by_code[code] = _features(frame)
        if number % 500 == 0:
            print(f"[factor-ic] loaded {number}/{len(codes)} files")

    factors = ["ret5", "ret20", "ret60", "ma20_bias", "vol20", "rsv20", "price_volume_corr20"]
    daily_ic = {factor: [] for factor in factors}
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
                rows.append(row)
        cross = pd.DataFrame(rows).replace([math.inf, -math.inf], pd.NA).dropna()
        if len(cross) < 50:
            continue
        target_rank = cross["future5"].rank(pct=True)
        for factor in factors:
            daily_ic[factor].append(float(cross[factor].rank(pct=True).corr(target_rank)))

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
        "source": "Qlib Alpha158 causal OHLCV subset",
        "development_period": {"start": start, "end": end},
        "sampled_stocks": len(feature_by_code),
        "forward_horizon_days": 5,
        "factor_ic": summary,
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
