#!/usr/bin/env python3
"""A/B-test a GitHub intraday-reversal gate on the unchanged cloud strategy."""
from __future__ import annotations

import argparse
import contextlib
import copy
import hashlib
import json
import math
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
STRATEGY_PATH = ROOT / "龟缠量化v6_optimized.py"

BUY_BLOCK = """        buy_candidates = sorted(
            [(code, bt_idx, row) for code, (bt_idx, row) in daily_rows.items()
             if code in stock_data and code in daily_params],
            key=lambda x: stock_data[x[0]]['score'],
            reverse=True
        )
"""

OVERLAY_BLOCK = BUY_BLOCK + """        _research_quantile = cfg.get("research_intraday_quantile")
        if _research_quantile is not None:
            _factor_rows = []
            for _code, _bt_idx, _row in buy_candidates:
                _signal = daily_state_sd[_code]['bt_df'].iloc[_bt_idx - 1]
                _open = float(_signal.get('open', 0) or 0)
                _close = float(_signal.get('close', 0) or 0)
                if _open > 0 and np.isfinite(_close / _open):
                    _factor_rows.append((_code, _close / _open - 1.0))
            _allowed = _research_lowest_quantile_codes(_factor_rows, _research_quantile)
            buy_candidates = [row for row in buy_candidates if row[0] in _allowed]
"""


def _lowest_quantile_codes(rows: list[tuple[str, float]], quantile: float) -> set[str]:
    if not 0 < quantile <= 1:
        raise ValueError("quantile must be in (0, 1]")
    finite = [(code, value) for code, value in rows if math.isfinite(value)]
    count = max(1, math.ceil(len(finite) * quantile)) if finite else 0
    return {code for code, _ in sorted(finite, key=lambda row: (row[1], row[0]))[:count]}


def _patched_source() -> str:
    source = STRATEGY_PATH.read_text(encoding="utf-8")
    if source.count(BUY_BLOCK) != 1:
        raise RuntimeError("cloud strategy buy block changed; overlay patch refused")
    return source.replace(BUY_BLOCK, OVERLAY_BLOCK)


def _load_strategy():
    name = "cloud_strategy_factor_overlay"
    module = types.ModuleType(name)
    module.__file__ = str(STRATEGY_PATH)
    module.__dict__["_research_lowest_quantile_codes"] = _lowest_quantile_codes
    sys.modules[name] = module
    exec(compile(_patched_source(), str(STRATEGY_PATH), "exec"), module.__dict__)
    return module


def _codes(data_root: Path, start: str, end: str, max_stocks: int) -> list[str]:
    from point_in_time_universe import load_universe

    universe, metadata = load_universe(
        data_root / "universe" / "point_in_time.json.gz",
        data_root / "universe" / "metadata.json",
    )
    days = [day for day in sorted(universe) if start <= day <= end]
    if not metadata.get("complete") or not days:
        raise RuntimeError("complete point-in-time universe is required")
    codes = sorted(set().union(*(universe[day] for day in days)))
    codes = [code for code in codes if (data_root / "stocks" / f"{code}.parquet").exists()]
    if max_stocks and len(codes) > max_stocks:
        codes = sorted(codes, key=lambda code: hashlib.sha256(code.encode("ascii")).digest())[:max_stocks]
    return codes


def _local_kline(data_root: Path):
    def load(code: str, count: int, cfg: dict) -> pd.DataFrame:
        del count, cfg
        normalized = code if code.startswith(("sh", "sz")) else ("sh" if code.startswith("6") else "sz") + code
        frame = pd.read_parquet(data_root / "stocks" / f"{normalized}.parquet")
        frame["date"] = pd.to_datetime(frame["date"])
        return frame.sort_values("date").reset_index(drop=True)

    return load


def _metrics(eq_df: pd.DataFrame, trades: list, initial_capital: float) -> dict:
    from performance_metrics import calculate, excellent_failures

    history = [
        {"date": str(row.date)[:10], "equity": float(row.equity)}
        for row in eq_df.itertuples(index=False)
    ]
    result = calculate(history, len(trades))
    result["total_return_pct"] = round(
        (history[-1]["equity"] / initial_capital - 1) * 100, 3,
    ) if history else 0.0
    result["failures"] = excellent_failures(result, require_deviation=False)
    return result


def run(data_root: Path, start: str, end: str, max_stocks: int, quantile: float) -> dict:
    os.environ["SUPER_AGENT_DATA_ROOT"] = str(data_root)
    strategy = _load_strategy()
    if not Path(strategy.STATE_FILE).exists():
        strategy.generate_timeline()
    strategy.get_kline = _local_kline(data_root)
    codes = _codes(data_root, start, end, max_stocks)
    initial_capital = float(strategy.DEFAULT_CONFIG["strategy"]["initial_capital"])

    results = {}
    with tempfile.TemporaryDirectory(prefix="cloud-overlay-") as temp:
        temp_root = Path(temp)
        shutil.copy2(ROOT / "data" / "optimal_params.json", temp_root / "optimal_params.json")
        strategy.DATA_DIR = str(temp_root)
        for name, overlay_quantile in (("cloud_baseline", None), ("cloud_intraday_overlay", quantile)):
            cfg = copy.deepcopy(strategy.DEFAULT_CONFIG)
            cfg["plot_enable"] = False
            if overlay_quantile is not None:
                cfg["research_intraday_quantile"] = overlay_quantile
            with open(os.devnull, "w", encoding="utf-8") as sink, contextlib.redirect_stdout(sink):
                trades, equity, *_ = strategy.run_multi_backtest(codes, cfg, start, end)
            results[name] = _metrics(equity, trades, initial_capital)

    baseline = results["cloud_baseline"]
    overlay = results["cloud_intraday_overlay"]
    return {
        "strategy": "unchanged-cloud-v6 plus GitHub v78 intraday-reversal entry gate",
        "period": {"start": start, "end": end},
        "stock_count": len(codes),
        "overlay": {"factor": "close/open-1", "keep_lowest_quantile": quantile},
        **results,
        "delta": {
            key: round(float(overlay[key]) - float(baseline[key]), 3)
            for key in ("annual_return_pct", "max_drawdown_pct", "calmar", "sortino", "sharpe", "closed_trades")
        },
        "decision": "research_pass" if not overlay["failures"] else "research_rejected",
        "limitations": [
            "the period is a consumed development domain and cannot be release evidence",
            "the cloud source and optimal_params are unchanged; only entry eligibility is filtered",
            "no deployment, shadow account, or main-strategy switch is performed",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--start", default="2011-01-04")
    parser.add_argument("--end", default="2017-12-29")
    parser.add_argument("--max-stocks", type=int, default=0)
    parser.add_argument("--quantile", type=float, default=0.5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.data_root.resolve(), args.start, args.end, args.max_stocks, args.quantile)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
