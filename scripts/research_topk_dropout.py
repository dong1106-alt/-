#!/usr/bin/env python3
"""Causal, fee-adjusted research replay of a Qlib-style Top-K dropout portfolio."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path

import pandas as pd

from performance_metrics import calculate, excellent_failures
from point_in_time_universe import load_universe
from trading_rules import calc_trade_cost, load_trade_cost

ROOT = Path(__file__).resolve().parent.parent
DATA_ROOT = Path(os.environ.get("SUPER_AGENT_DATA_ROOT", str(ROOT / "data"))).resolve()
ML_FEATURES = (
    "kmid", "klen", "ret1", "ret5", "ret10", "ret20", "ret60",
    "ma5_bias", "ma10_bias", "ma20_bias", "ma60_bias",
    "vol5", "vol10", "vol20", "vol60", "rsv5", "rsv20", "rsv60",
    "volume_ratio5", "volume_ratio20", "price_volume_corr20", "price_volume_corr60",
)


def _features(frame: pd.DataFrame, forward_horizon: int = 5) -> pd.DataFrame:
    close = frame["close"]
    daily = close.pct_change()
    output = pd.DataFrame(index=frame.index)
    output["kmid"] = (close - frame["open"]) / frame["open"]
    output["klen"] = (frame["high"] - frame["low"]) / frame["open"]
    output["ret1"] = daily
    output["ret5"] = close.pct_change(5)
    output["ret10"] = close.pct_change(10)
    output["ret20"] = close.pct_change(20)
    output["ret60"] = close.pct_change(60)
    for window in (5, 10, 20, 60):
        output[f"ma{window}_bias"] = close / close.rolling(window).mean() - 1
        output[f"vol{window}"] = daily.rolling(window).std() * math.sqrt(250)
    for window in (5, 20, 60):
        low = frame["low"].rolling(window).min()
        high = frame["high"].rolling(window).max()
        output[f"rsv{window}"] = (close - low) / (high - low)
    output["volume_ratio5"] = frame["volume"] / frame["volume"].rolling(5).mean()
    output["volume_ratio20"] = frame["volume"] / frame["volume"].rolling(20).mean()
    output["price_volume_corr20"] = close.rolling(20).corr(frame["volume"].clip(lower=1).apply(math.log))
    output["price_volume_corr60"] = close.rolling(60).corr(frame["volume"].clip(lower=1).apply(math.log))
    # Supervised research label: next open through the sixth open; never a signal feature.
    if forward_horizon < 1:
        raise ValueError("forward_horizon must be positive")
    future5 = pd.Series(float("nan"), index=frame.index)
    opens = frame["open"].to_numpy()
    entry = pd.Series(opens[1:-forward_horizon]).where(lambda values: values > 0)
    exit_ = pd.Series(opens[forward_horizon + 1:]).where(lambda values: values > 0)
    future5.iloc[:-(forward_horizon + 1)] = (exit_ / entry - 1).to_numpy()
    output["future5"] = future5
    return output


def _load_bars(stock_dir: Path, codes: list[str], start: str, end: str,
               history_start: str | None = None, forward_horizon: int = 5,
               volume_multipliers: dict[str, int] | None = None) -> dict[str, pd.DataFrame]:
    bars = {}
    start_ts = pd.Timestamp(history_start) if history_start else pd.Timestamp(start) - pd.Timedelta(days=240)
    end_ts = pd.Timestamp(end)
    for number, code in enumerate(codes, 1):
        path = stock_dir / f"{code}.parquet"
        if not path.exists():
            continue
        try:
            frame = pd.read_parquet(
                path, columns=["date", "open", "high", "low", "close", "volume", "trade_status"],
            )
        except Exception:
            continue
        frame["date"] = pd.to_datetime(frame["date"])
        frame = frame[(frame["date"] >= start_ts) & (frame["date"] <= end_ts)].sort_values("date")
        if len(frame) < 130:
            continue
        for column, values in _features(frame, forward_horizon).items():
            frame[column] = values
        multiplier = (volume_multipliers or {}).get(code, 100)
        frame["avg_value20"] = (frame["close"] * frame["volume"] * multiplier).rolling(20).mean()
        bars[code] = frame.set_index("date")
        if number % 500 == 0:
            print(f"[topk] loaded {number}/{len(codes)} files")
    return bars


def _fit_lightgbm(bars: dict[str, pd.DataFrame], train_end: str, valid_end: str,
                  forward_horizon: int) -> dict:
    try:
        import lightgbm as lgb
    except ImportError as exc:
        raise RuntimeError("lightgbm is required in the Codex research environment") from exc

    parts = []
    for code, frame in bars.items():
        part = frame.loc[:, [*ML_FEATURES, "future5"]].copy()
        part["code"] = code
        part["date"] = part.index
        parts.append(part.reset_index(drop=True))
    panel = pd.concat(parts, ignore_index=True).replace([math.inf, -math.inf], float("nan"))
    panel = panel.dropna(subset=list(ML_FEATURES))
    panel.loc[:, list(ML_FEATURES)] = panel.groupby("date")[list(ML_FEATURES)].rank(pct=True)
    labeled = panel.dropna(subset=["future5"]).copy()
    labeled["label"] = labeled.groupby("date")["future5"].rank(pct=True) - 0.5
    train = labeled[labeled["date"] <= pd.Timestamp(train_end)]
    valid = labeled[(labeled["date"] > pd.Timestamp(train_end)) & (labeled["date"] <= pd.Timestamp(valid_end))]
    if len(train) < 10_000 or len(valid) < 5_000:
        raise RuntimeError("insufficient date-separated LightGBM samples")

    model = lgb.LGBMRegressor(
        objective="regression", n_estimators=1000, learning_rate=0.05,
        num_leaves=63, max_depth=8, colsample_bytree=0.8879,
        subsample=0.8789, reg_alpha=20.0, reg_lambda=50.0,
        n_jobs=-1, verbosity=-1, random_state=42,
    )
    model.fit(
        train[list(ML_FEATURES)], train["label"],
        eval_set=[(valid[list(ML_FEATURES)], valid["label"])],
        callbacks=[lgb.early_stopping(50, verbose=False)],
    )
    panel["ml_score"] = model.predict(panel[list(ML_FEATURES)], num_iteration=model.best_iteration_)
    for code, scored in panel.groupby("code"):
        values = scored.set_index("date")["ml_score"]
        bars[code].loc[values.index, "ml_score"] = values
    valid_ic = valid.assign(
        prediction=model.predict(valid[list(ML_FEATURES)], num_iteration=model.best_iteration_),
    ).groupby("date").apply(
        lambda rows: rows["prediction"].corr(rows["label"]), include_groups=False,
    )
    return {
        "train_samples": len(train), "validation_samples": len(valid),
        "best_iteration": int(model.best_iteration_),
        "validation_mean_rank_ic": round(float(valid_ic.mean()), 4),
        "validation_ic_positive_rate": round(float((valid_ic > 0).mean()), 3),
    }


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
    return ratio < 1.095 if side == "buy" else ratio > 0.905


def _rank_scores(cross: pd.DataFrame, model_name: str) -> pd.Series:
    ranks = cross.rank(pct=True)
    if model_name == "lightgbm":
        return ranks["ml_score"]
    if model_name == "reversal60":
        return -ranks["ret60"]
    weights = {
        "ret5": 0.05, "ret20": 0.15, "ret60": 0.15, "ma20_bias": 0.10,
        "vol20": 0.25, "rsv20": 0.05, "price_volume_corr20": 0.25,
    }
    return -sum(ranks[name] * weight for name, weight in weights.items())


def run(*, start: str, end: str, max_stocks: int, topk: int, n_drop: int,
        rebalance_days: int, market_ma: int, circuit_drawdown_pct: float,
        model_name: str = "linear", train_end: str = "2023-12-20",
        valid_end: str = "2024-12-20", require_stock_uptrend: bool = False,
        forward_horizon: int = 5, data_start: str = "2022-01-01") -> dict:
    universe, universe_meta = load_universe()
    days = [pd.Timestamp(day) for day in sorted(universe) if start <= day <= end]
    if not universe_meta.get("complete") or len(days) < 120:
        raise RuntimeError("complete point-in-time universe with at least 120 days is required")
    code_days = days
    if model_name == "lightgbm":
        # Freeze the research sample before test data begins.
        code_days = [pd.Timestamp(day) for day in sorted(universe) if data_start <= day <= valid_end]
    codes = sorted(set().union(*(universe[str(day)[:10]] for day in code_days)))
    if max_stocks and len(codes) > max_stocks:
        codes = sorted(codes, key=lambda code: hashlib.sha256(code.encode("ascii")).digest())[:max_stocks]
    history_manifest = json.loads(
        (DATA_ROOT / "universe" / "stock_history_manifest.json").read_text(encoding="utf-8")
    ).get("stocks", {})
    volume_multipliers = {
        code: 1 if str(history_manifest.get(code, {}).get("source", "")).startswith("BaoStock") else 100
        for code in codes
    }
    history_start = data_start if model_name == "lightgbm" else None
    bars = _load_bars(
        DATA_ROOT / "stocks", codes, start, end,
        history_start=history_start, forward_horizon=forward_horizon,
        volume_multipliers=volume_multipliers,
    )
    if len(bars) < topk * 3:
        raise RuntimeError("too few complete stock histories")

    model_meta = (
        _fit_lightgbm(bars, train_end, valid_end, forward_horizon)
        if model_name == "lightgbm" else None
    )
    index = pd.read_parquet(DATA_ROOT / "index" / "sh000001.parquet")
    index["date"] = pd.to_datetime(index["date"])
    index = index.sort_values("date").set_index("date")
    if market_ma > 0:
        index["market_ma"] = index["close"].rolling(market_ma).mean()
    days = [day for day in days if day in index.index]
    trade_cfg = load_trade_cost(ROOT / "config" / "settings.yaml")

    cash = 1_000_000.0
    positions: dict[str, int] = {}
    last_close: dict[str, float] = {}
    equity_history = []
    pending = None
    closed_trades = 0
    total_fees = 0.0
    peak = cash
    risk_off_until = -1

    for day_number, day in enumerate(days):
        day_key = str(day)[:10]
        membership = universe[day_key]

        if pending is not None:
            target, sell_all = pending
            sell_codes = list(positions) if sell_all else [code for code in positions if code not in target]
            for code in sell_codes:
                row = _bar(bars[code], day)
                previous = last_close.get(code, 0.0)
                if not _tradable(row, previous, "sell"):
                    continue
                shares = positions.pop(code)
                _, proceeds, commission, tax = calc_trade_cost(float(row["open"]), shares, "sell", trade_cfg)
                cash += proceeds
                total_fees += commission + tax
                closed_trades += 1

            if not sell_all:
                open_values = []
                for code, shares in positions.items():
                    row = _bar(bars[code], day)
                    open_values.append(shares * float(row["open"] if row is not None else last_close.get(code, 0)))
                equity_open = cash + sum(open_values)
                slot_value = equity_open * 0.95 / topk
                for code in target:
                    if code in positions or len(positions) >= topk:
                        continue
                    row = _bar(bars[code], day)
                    previous = last_close.get(code, 0.0)
                    if not _tradable(row, previous, "buy"):
                        continue
                    shares = int(slot_value / float(row["open"]) // 100) * 100
                    while shares >= 100:
                        _, cost, commission, tax = calc_trade_cost(float(row["open"]), shares, "buy", trade_cfg)
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
            price = float(row["close"]) if row is not None else last_close.get(code, 0.0)
            value += shares * price
        if not positions and day_number == risk_off_until:
            peak = value
        peak = max(peak, value)
        drawdown_pct = (1.0 - value / peak) * 100 if peak else 0.0
        equity_history.append({"date": day_key, "equity": round(value, 2)})

        for code, frame in bars.items():
            row = _bar(frame, day)
            if row is not None:
                last_close[code] = float(row["close"])

        if drawdown_pct >= circuit_drawdown_pct and positions:
            risk_off_until = day_number + 20
            pending = ([], True)
            continue
        if day_number % rebalance_days or pending is not None:
            continue

        market_on = market_ma <= 0 or float(index.loc[day, "close"]) > float(index.loc[day, "market_ma"])
        if not market_on or day_number < risk_off_until:
            if positions:
                pending = ([], True)
            continue

        candidate_rows = []
        for code, frame in bars.items():
            if code not in membership:
                continue
            row = _bar(frame, day)
            if model_name == "lightgbm":
                factor_names = ("ml_score",)
            elif model_name == "reversal60":
                factor_names = ("ret60",)
            else:
                factor_names = (
                    "ret5", "ret20", "ret60", "ma20_bias", "vol20", "rsv20",
                    "price_volume_corr20",
                )
            if row is None or any(pd.isna(row[name]) for name in factor_names):
                continue
            if float(row["avg_value20"]) < 20_000_000:
                continue
            if require_stock_uptrend and float(row["ma20_bias"]) <= 0:
                continue
            candidate_rows.append({"code": code, **{name: float(row[name]) for name in factor_names}})
        cross = pd.DataFrame(candidate_rows).set_index("code") if candidate_rows else pd.DataFrame()
        if cross.empty:
            continue
        scores = _rank_scores(cross, model_name)
        order = list(scores.sort_values(ascending=False).index)
        rank = {code: number for number, code in enumerate(order)}
        forced = [code for code in positions if code not in membership or code not in rank]
        drop_pool = sorted(
            (code for code in positions if code in rank), key=lambda code: rank[code], reverse=True,
        )
        sells = forced + [code for code in drop_pool if rank[code] >= topk][:n_drop]
        survivors = [code for code in positions if code not in sells]
        buys = [code for code in order if code not in survivors][:max(0, topk - len(survivors))]
        pending = (survivors + buys, False)

    metrics = calculate(equity_history, closed_trades)
    failures = excellent_failures(metrics, require_deviation=False)
    return {
        "strategy": "qlib-inspired-topk-dropout-v1",
        "source": "https://github.com/microsoft/qlib/blob/main/qlib/contrib/strategy/signal_strategy.py",
        "period": {"start": start, "end": end},
        "parameters": {
            "topk": topk, "n_drop": n_drop, "rebalance_days": rebalance_days,
            "market_ma": market_ma, "circuit_drawdown_pct": circuit_drawdown_pct,
            "sampled_stocks": len(bars), "model": model_name,
            "require_stock_uptrend": require_stock_uptrend,
            "forward_horizon": forward_horizon,
        },
        "model_validation": model_meta,
        "metrics": metrics,
        "total_return_pct": round((equity_history[-1]["equity"] / equity_history[0]["equity"] - 1) * 100, 3),
        "total_fees": round(total_fees, 2),
        "decision": "research_pass" if not failures else "research_rejected",
        "failures": failures,
        "limitations": [
            "研究回放区间已被既有验证使用，不能作为新的发布样本外证据",
            "必须在未见区间复验并完成实时影子500笔后才可申请切换",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2023-01-03")
    parser.add_argument("--end", default="2026-09-04")
    parser.add_argument("--max-stocks", type=int, default=800)
    parser.add_argument("--topk", type=int, default=30)
    parser.add_argument("--n-drop", type=int, default=10)
    parser.add_argument("--rebalance-days", type=int, default=5)
    parser.add_argument("--market-ma", type=int, default=120)
    parser.add_argument("--circuit-drawdown-pct", type=float, default=8.0)
    parser.add_argument("--model", choices=("linear", "lightgbm", "reversal60"), default="linear")
    parser.add_argument("--train-end", default="2023-12-20")
    parser.add_argument("--valid-end", default="2024-12-20")
    parser.add_argument("--require-stock-uptrend", action="store_true")
    parser.add_argument("--forward-horizon", type=int, default=5)
    parser.add_argument("--data-start", default="2022-01-01")
    parser.add_argument("--output", type=Path, default=ROOT / "data" / "research_results" / "topk_dropout.json")
    args = parser.parse_args()
    result = run(
        start=args.start, end=args.end, max_stocks=args.max_stocks, topk=args.topk,
        n_drop=args.n_drop, rebalance_days=args.rebalance_days, market_ma=args.market_ma,
        circuit_drawdown_pct=args.circuit_drawdown_pct, model_name=args.model,
        train_end=args.train_end, valid_end=args.valid_end,
        require_stock_uptrend=args.require_stock_uptrend,
        forward_horizon=args.forward_horizon,
        data_start=args.data_start,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
