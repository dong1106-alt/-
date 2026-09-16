#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
全市场沪深主板股票综合质量评分（quality_score）筛选脚本

用途：bear市场新门槛 pre_filter_threshold=68 下，扫描全市场沪深主板股票，
      筛选 quality_score >= 68 的可买标的，并统计 60-68 分区间数量。

评分逻辑：复用 data/causal_quality.py，与每日信号扫描使用同一评分函数。

数据来源：本地 parquet（data/stocks/），不联网。
输出：终端打印 Top30 + 区间统计，结果落盘 codeact/output/high_score_screen_YYYYMMDD.txt
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===

import asyncio
import sys
import os
import re
import json
import time
import warnings
from datetime import datetime
from concurrent.futures import ProcessPoolExecutor, as_completed

# ============================================================
# 0. 自动安装 pyarrow（沙箱环境可能缺失）
# ============================================================
def _ensure_pyarrow():
    try:
        import pyarrow  # noqa: F401
        return
    except ImportError:
        pass
    print("[依赖] pyarrow 未安装，正在从阿里云镜像安装...")
    import subprocess
    r = subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyarrow",
         "-i", "https://mirrors.aliyun.com/pypi/simple/", "--timeout", "300"],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        print("[依赖] pip install 失败：", r.stderr[-500:])
        raise
    print("[依赖] pyarrow 安装完成。")

_ensure_pyarrow()

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ============================================================
# 1. 路径与常量（可通过命令行参数覆盖）
# ============================================================
BASE_DIR = f"{_ROOT}"
STOCK_DIR = os.path.join(BASE_DIR, "data", "stocks")
NAME_FILE = os.path.join(BASE_DIR, "data", "all_main_board_codes.txt")
OUTPUT_DIR = os.path.join(BASE_DIR, "codeact", "output")

# 沪深主板代码正则（排除创业板 300/301、科创板 688、北交所 8xx/4xx）
MAIN_BOARD_PATTERNS = [
    re.compile(r"^sh60\d{4}\.parquet$"),
    re.compile(r"^sz000\d{3}\.parquet$"),
    re.compile(r"^sz001\d{3}\.parquet$"),
    re.compile(r"^sz002\d{3}\.parquet$"),
]

# 评分使用的 K 线条数（与 daily_signal_scan.py KLINE_COUNT 一致）
KLINE_COUNT = 150
MIN_DATA_DAYS = 120

# 与 daily_signal_scan.py STRATEGY 完全一致的配置
STRATEGY = {
    "dc_period": 20,
    "exit_period": 10,
    "atr_period": 20,
    "vol_window": 60,
    "base_risk_pct": 0.10,
    "min_atr_pct": 0.020,
    "base_atr_multiplier": 2.0,
    "base_chan_threshold": 0.70,
    "chan_lookback": 1,
    "min_entry_score": 1.7,
    "max_entry_score": 3.5,
    "pre_filter_threshold": 60,
    "market_hard_cutoff": -0.03,
    "volatility_stop_multiplier_min": 1.5,
    "volatility_stop_multiplier_max": 3.0,
    "volatility_position_scale_factor": 0.15,
    "volatility_adaptive": True,
    "stock_trend_filter": True,
    "stock_trend_lookback": 20,
    "min_position_scale": 0.0,
    "max_position_scale": 1.00,
    "tanh_sensitivity": 4.0,
    "vol_ratio_high": 1.3,
    "vol_ratio_low": 0.7,
    "trend_str_high": 0.15,
    "trend_str_low": -0.05,
    "use_trend_filter": True,
    "use_money_filter": True,
    "use_test_pullback": True,
    "test_pullback_threshold": 0.02,
    "test_pullback_vol_ratio": 0.7,
    "test_period": 30,
    "test_gain_threshold": 0.06,
    "extreme_value_exemption": True,
    "pe_historical_percentile_threshold": 0.10,
    "pb_historical_percentile_threshold": 0.10,
    "extreme_value_position_scale": 0.3,
    "extreme_value_max_hold_days": 20,
    "vol_stack_threshold_large": 3.0,
    "vol_stack_threshold_mid": 5.0,
    "vol_stack_threshold_small": 8.0,
}


# ============================================================
# 2. 股票名称映射（从 all_main_board_codes.txt 加载，同时用于 ST 过滤）
# ============================================================
def load_name_map(path: str) -> dict:
    """加载 code -> name 映射；同时返回 ST 代码集合。"""
    name_map = {}
    if not os.path.exists(path):
        return name_map
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or "," not in line:
                continue
            parts = line.split(",", 1)
            code = parts[0].strip()
            name = parts[1].strip() if len(parts) > 1 else ""
            name_map[code] = name
    return name_map


def is_st_name(name: str) -> bool:
    """判断是否 ST / *ST / PT 股。"""
    if not name:
        return False
    uname = name.upper().replace(" ", "")
    return "ST" in uname or "PT" in uname


# ============================================================
# 3. 指标计算（严格复刻 daily_signal_scan.py calc_indicators）
# ============================================================
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算全部技术指标（移植自 daily_signal_scan.py，含动态通道/退出线）。"""
    st = STRATEGY
    dc_p = st["dc_period"]
    exit_p = st["exit_period"]
    atr_p = st["atr_period"]
    vol_win = st["vol_window"]

    df = df.copy()

    # 动态海龟通道
    atr_pct = (df["close"].diff().abs() / df["close"].shift(1)).rolling(60).mean()
    atr_pct_ma = atr_pct.rolling(120).mean()
    vol_ratio = atr_pct / atr_pct_ma

    ch_base = st["dc_period"]
    ch_low = max(5, ch_base - 5)
    ch_high = ch_base + 5

    df[f"dc_high_{ch_low}"] = df["high"].rolling(ch_low).max().shift(1)
    df[f"dc_high_{ch_base}"] = df["high"].rolling(ch_base).max().shift(1)
    df[f"dc_high_{ch_high}"] = df["high"].rolling(ch_high).max().shift(1)
    df[f"dc_low_{ch_low}"] = df["low"].rolling(ch_low).min().shift(1)
    df[f"dc_low_{ch_base}"] = df["low"].rolling(ch_base).min().shift(1)
    df[f"dc_low_{ch_high}"] = df["low"].rolling(ch_high).min().shift(1)

    vr_high = st["vol_ratio_high"]
    vr_low = st["vol_ratio_low"]
    dc_high = pd.Series(np.nan, index=df.index)
    dc_low = pd.Series(np.nan, index=df.index)
    high_vol = vol_ratio > vr_high
    low_vol = vol_ratio < vr_low
    dc_high[high_vol] = df.loc[high_vol, f"dc_high_{ch_high}"]
    dc_low[high_vol] = df.loc[high_vol, f"dc_low_{ch_high}"]
    dc_high[low_vol] = df.loc[low_vol, f"dc_high_{ch_low}"]
    dc_low[low_vol] = df.loc[low_vol, f"dc_low_{ch_low}"]
    dc_high[~high_vol & ~low_vol] = df.loc[~high_vol & ~low_vol, f"dc_high_{ch_base}"]
    dc_low[~high_vol & ~low_vol] = df.loc[~high_vol & ~low_vol, f"dc_low_{ch_base}"]
    df["dc_high"] = dc_high
    df["dc_low"] = dc_low

    # 动态退出线
    ex_base = st["exit_period"]
    ex_low = max(3, ex_base - 2)
    ex_high = ex_base + 2
    df[f"exit_low_{ex_low}"] = df["low"].rolling(ex_low).min().shift(1)
    df[f"exit_low_{ex_base}"] = df["low"].rolling(ex_base).min().shift(1)
    df[f"exit_low_{ex_high}"] = df["low"].rolling(ex_high).min().shift(1)
    trend_str = (df["close"] - df["close"].rolling(60).mean()) / df["close"].rolling(60).mean()
    ts_high = st["trend_str_high"]
    ts_low = st["trend_str_low"]
    strong_trend = trend_str > ts_high
    weak_trend = trend_str < ts_low
    exit_low = pd.Series(np.nan, index=df.index)
    exit_low[strong_trend] = df.loc[strong_trend, f"exit_low_{ex_high}"]
    exit_low[weak_trend] = df.loc[weak_trend, f"exit_low_{ex_low}"]
    exit_low[~strong_trend & ~weak_trend] = df.loc[~strong_trend & ~weak_trend, f"exit_low_{ex_base}"]
    df["exit_low"] = exit_low

    df["prev_close"] = df["close"].shift(1)

    # ATR
    tr1 = df["high"] - df["low"]
    tr2 = abs(df["high"] - df["prev_close"])
    tr3 = abs(df["low"] - df["prev_close"])
    df["tr"] = np.maximum(np.maximum(tr1, tr2), tr3)
    df["atr"] = df["tr"].rolling(atr_p).mean()

    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = 2 * (df["dif"] - df["dea"])

    # 均线
    df["ma5"] = df["close"].rolling(5).mean()
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["trend_strength"] = (df["close"] - df["ma60"]) / df["ma60"]
    df["ma20_slope"] = (df["ma20"] - df["ma20"].shift(5)) / df["ma20"].shift(5)

    # 资金面
    df["money_strength"] = df["macd_hist"]
    df["money_slope"] = df["money_strength"] - df["money_strength"].shift(3)

    # 试盘回踩
    df["test_day"] = (df["close"] / df["close"].shift(1) - 1) >= st["test_gain_threshold"]
    df["test_occurred"] = df["test_day"].rolling(st["test_period"]).sum() > 0

    # 成交量
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_q75"] = df["volume"].rolling(vol_win).quantile(0.75)
    df["vol_effective"] = df["volume"] > df["vol_q75"]

    # 波动率
    df["volatility_ratio"] = df["atr"] / df["close"]
    df["volatility_ma"] = df["volatility_ratio"].rolling(120).mean()
    df["volatility_percentile"] = df["volatility_ratio"].rolling(120).rank(pct=True)
    df["volatility_percentile"] = df["volatility_percentile"].fillna(0.5)
    df["ma60_direction"] = df["ma60"] > df["ma60"].shift(st["stock_trend_lookback"])

    # 风险值
    llv_34 = df["low"].rolling(34).min()
    hhv_34 = df["high"].rolling(34).max()
    range_34 = hhv_34 - llv_34
    range_34 = range_34.replace(0, 1)
    raw_risk = 100 * (df["close"] - llv_34) / range_34
    raw_risk = raw_risk.clip(0, 100)
    df["risk_value"] = raw_risk.ewm(span=3, adjust=False).mean()

    # 堆量比
    ma_vol_10 = df["volume"].rolling(10).mean()
    ma_vol_20 = df["volume"].rolling(20).mean()
    ma_vol_15 = df["volume"].rolling(15).mean()
    ma_vol_40 = df["volume"].rolling(40).mean()
    llv_mavol20_120 = ma_vol_20.rolling(120).min()
    llv_mavol40_300 = ma_vol_40.rolling(300).min()
    llv_mavol20_120 = llv_mavol20_120.replace(0, 1)
    llv_mavol40_300 = llv_mavol40_300.replace(0, 1)
    vol_ratio_1 = ma_vol_10 / llv_mavol20_120
    vol_ratio_2 = ma_vol_15 / llv_mavol40_300
    df["vol_stack_ratio"] = np.maximum(vol_ratio_1.fillna(0), vol_ratio_2.fillna(0))

    # 周线趋势
    weekly_close = df["close"].rolling(5).mean()
    weekly_ma20 = weekly_close.rolling(100).mean()
    df["weekly_trend"] = 0
    df.loc[weekly_close > weekly_ma20 * 1.005, "weekly_trend"] = 1
    df.loc[weekly_close < weekly_ma20 * 0.995, "weekly_trend"] = -1

    # 早鸟突破
    df["dc_high_aggressive"] = df["high"].rolling(10).max().shift(1)

    return df


# ============================================================
# 4. 估值分位（严格复刻 daily_signal_scan.py）
# ============================================================
def calc_valuation_percentile(df: pd.DataFrame, lookback: int = 250) -> dict:
    if len(df) < lookback:
        return {"price_percentile": 0.5, "is_extreme": False,
                "min_price": 0, "max_price": 0}
    recent = df.tail(lookback)
    current_price = df["close"].iloc[-1]
    min_price = recent["close"].min()
    max_price = recent["close"].max()
    if max_price == min_price:
        percentile = 0.5
    else:
        percentile = (current_price - min_price) / (max_price - min_price)
    return {
        "price_percentile": float(percentile),
        "is_extreme": bool(percentile < 0.10),
        "min_price": float(min_price),
        "max_price": float(max_price),
    }


# ============================================================
# 5. 综合质量评分（严格复刻 daily_signal_scan.py calc_stock_quality_score）
# ============================================================
def calc_stock_quality_score(df: pd.DataFrame):
    """截至当日的因果预评分，见 data.causal_quality。"""
    from data.causal_quality import score_stock
    return score_stock(df, STRATEGY)


# ============================================================
# 6. 单只股票评分（worker 进程入口）
# ============================================================
def score_one_stock(args):
    """
    参数: (filename, stock_dir, target_date_str, name_map_serializable)
    返回: dict 或 None
    """
    filename, stock_dir, target_date_str, name_map = args
    code = filename.replace(".parquet", "")
    path = os.path.join(stock_dir, filename)

    try:
        df = pd.read_parquet(path)
        if "date" not in df.columns:
            df = df.reset_index()
        df["date"] = pd.to_datetime(df["date"])
        for c in ["open", "close", "high", "low", "volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        df = df.dropna(subset=["open", "close", "high", "low", "volume"])

        if len(df) < MIN_DATA_DAYS:
            return None

        last_date = df["date"].iloc[-1]
        last_date_str = last_date.strftime("%Y-%m-%d")
        # 只保留最新K线日期 == 目标交易日（或最近交易日）
        if last_date_str != target_date_str:
            return None

        # 取最近 KLINE_COUNT 根（与 daily scan 一致）
        df = df.tail(KLINE_COUNT).reset_index(drop=True)
        if len(df) < MIN_DATA_DAYS:
            return None

        df = calc_indicators(df)
        score, detail = calc_stock_quality_score(df)

        last_row = df.iloc[-1]
        close = float(last_row["close"])
        ma5 = float(last_row["ma5"]) if not pd.isna(last_row["ma5"]) else 0.0
        ma20 = float(last_row["ma20"]) if not pd.isna(last_row["ma20"]) else 0.0
        atr = float(last_row["atr"]) if not pd.isna(last_row["atr"]) else 0.0
        atr_pct = (atr / close * 100) if close > 0 else 0.0

        name = name_map.get(code, "")

        return {
            "code": code,
            "name": name,
            "score": float(score),
            "close": close,
            "ma5": ma5,
            "ma20": ma20,
            "atr_pct": atr_pct,
            "last_date": last_date_str,
            "data_days": int(len(df)),
            "detail": detail,
        }
    except Exception as e:
        # 单只失败不影响整体
        return {"code": code, "error": str(e)[:120]}


# ============================================================
# 7. 主流程
# ============================================================
async def main():
    # 位置参数: result_mode [target_date] [threshold] [max_workers]
    result_mode = sys.argv[1] if len(sys.argv) > 1 else "display_only"
    target_date = sys.argv[2] if len(sys.argv) > 2 else "2026-08-25"
    threshold = float(sys.argv[3]) if len(sys.argv) > 3 else 68.0
    max_workers = int(sys.argv[4]) if len(sys.argv) > 4 else max(2, (os.cpu_count() or 4))

    print(f"[参数] result_mode={result_mode}, target_date={target_date}, "
          f"threshold={threshold}, max_workers={max_workers}")

    # 归一化 result_mode
    actual_mode = result_mode if result_mode != "auto" else "display_only"

    sdk = None
    try:
        from codeact_sdk import CodeActSDK
        sdk = CodeActSDK()
    except Exception:
        sdk = None  # 允许普通 python 运行

    try:
        t0 = time.time()

        # 7.1 加载名称映射 & ST 过滤
        name_map = load_name_map(NAME_FILE)
        print(f"[名称] 已加载 {len(name_map)} 条代码-名称映射")

        # 7.2 扫描主板 parquet 文件
        all_files = os.listdir(STOCK_DIR)
        main_board_files = []
        skipped_st = 0
        skipped_non_main = 0
        for f in all_files:
            if not f.endswith(".parquet"):
                continue
            matched = False
            for pat in MAIN_BOARD_PATTERNS:
                if pat.match(f):
                    matched = True
                    break
            if not matched:
                skipped_non_main += 1
                continue
            code = f.replace(".parquet", "")
            name = name_map.get(code, "")
            if is_st_name(name):
                skipped_st += 1
                continue
            main_board_files.append(f)

        print(f"[筛选] 沪深主板（排除ST/PT）候选: {len(main_board_files)} 只 "
              f"(排除非主板 {skipped_non_main}, ST/PT {skipped_st})")

        # 7.3 多进程评分
        # name_map 作为可序列化 dict 传入子进程
        tasks = [(f, STOCK_DIR, target_date, name_map) for f in main_board_files]
        results = []
        done = 0
        total = len(tasks)
        errors = 0

        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(score_one_stock, t): t[0] for t in tasks}
            for fut in as_completed(futures):
                done += 1
                try:
                    r = fut.result()
                except Exception:
                    errors += 1
                    continue
                if r is None:
                    continue
                if "error" in r:
                    errors += 1
                    continue
                results.append(r)
                if done % 300 == 0 or done == total:
                    print(f"[进度] {done}/{total} 完成，已获取有效评分 {len(results)} 只")

        elapsed = time.time() - t0
        print(f"[完成] 共处理 {total} 只，有效评分 {len(results)} 只，"
              f"错误/跳过 {errors}，耗时 {elapsed:.1f}s")

        if not results:
            msg = f"今日（{target_date}）全市场无有效评分数据。"
            print(f"\n{msg}")
            if sdk:
                await sdk.submit_result(
                    result_mode=actual_mode, status="success",
                    message=msg,
                    data={"target_date": target_date, "scored": 0,
                          "high_score_count": 0, "mid_range_count": 0},
                )
            return

        # 7.4 分区间统计
        high = [r for r in results if r["score"] >= threshold]
        mid = [r for r in results if threshold - 8 <= r["score"] < threshold]
        high.sort(key=lambda x: x["score"], reverse=True)
        mid.sort(key=lambda x: x["score"], reverse=True)

        print(f"\n{'='*70}")
        print(f"  全市场沪深主板 quality_score 筛选结果（{target_date}）")
        print(f"  bear市场新门槛 pre_filter_threshold = {threshold:.0f}")
        print(f"{'='*70}")
        print(f"  有效评分总数: {len(results)}")
        print(f"  ≥{threshold:.0f} 分: {len(high)} 只")
        print(f"  {threshold-8:.0f}-{threshold:.0f} 分区间: {len(mid)} 只（供参考）")
        print(f"{'='*70}")

        # 7.5 打印 Top30
        if high:
            print(f"\n【≥{threshold:.0f}分股票列表】（按评分降序，最多Top30）\n")
            header = f"{'排名':>4}  {'代码':<10} {'名称':<10} {'评分':>6}  {'收盘价':>8}  {'MA5':>8}  {'MA20':>8}  {'ATR%':>6}"
            print(header)
            print("-" * len(header))
            for i, r in enumerate(high[:30], 1):
                name_disp = r["name"][:8] if r["name"] else "-"
                print(f"{i:>4}  {r['code']:<10} {name_disp:<10} {r['score']:>6.1f}  "
                      f"{r['close']:>8.2f}  {r['ma5']:>8.2f}  {r['ma20']:>8.2f}  "
                      f"{r['atr_pct']:>5.2f}%")
        else:
            print(f"\n*** 今日全市场无≥{threshold:.0f}分股票 ***")

        # 打印 60-68 区间前10
        if mid:
            print(f"\n【{threshold-8:.0f}-{threshold:.0f}分区间 Top10（供参考）】\n")
            header2 = f"{'排名':>4}  {'代码':<10} {'名称':<10} {'评分':>6}  {'收盘价':>8}  {'MA20':>8}  {'ATR%':>6}"
            print(header2)
            print("-" * len(header2))
            for i, r in enumerate(mid[:10], 1):
                name_disp = r["name"][:8] if r["name"] else "-"
                print(f"{i:>4}  {r['code']:<10} {name_disp:<10} {r['score']:>6.1f}  "
                      f"{r['close']:>8.2f}  {r['ma20']:>8.2f}  {r['atr_pct']:>5.2f}%")

        # 7.6 落盘
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        out_path = os.path.join(OUTPUT_DIR, f"high_score_screen_{target_date.replace('-', '')}.txt")
        with open(out_path, "w", encoding="utf-8") as f:
            f.write(f"全市场沪深主板 quality_score 筛选结果\n")
            f.write(f"扫描日期: {target_date}\n")
            f.write(f"bear市场新门槛 pre_filter_threshold = {threshold:.0f}\n")
            f.write(f"评分逻辑: daily_signal_scan.py calc_indicators + calc_stock_quality_score (KLINE_COUNT={KLINE_COUNT})\n")
            f.write(f"有效评分总数: {len(results)}\n")
            f.write(f"≥{threshold:.0f}分: {len(high)} 只\n")
            f.write(f"{threshold-8:.0f}-{threshold:.0f}分区间: {len(mid)} 只\n")
            f.write(f"处理耗时: {elapsed:.1f}s\n")
            f.write("\n" + "=" * 90 + "\n")
            if high:
                f.write(f"【≥{threshold:.0f}分股票列表】（按评分降序）\n\n")
                f.write(f"{'排名':>4}  {'代码':<10} {'名称':<12} {'评分':>6}  "
                        f"{'收盘价':>8}  {'MA5':>8}  {'MA20':>8}  {'ATR%':>7}  "
                        f"{'R²':>6} {'模拟笔数':>6} {'胜率':>6} {'估值分位':>8}\n")
                f.write("-" * 110 + "\n")
                for i, r in enumerate(high, 1):
                    d = r.get("detail", {})
                    name_disp = r["name"][:10] if r["name"] else "-"
                    f.write(f"{i:>4}  {r['code']:<10} {name_disp:<12} {r['score']:>6.1f}  "
                            f"{r['close']:>8.2f}  {r['ma5']:>8.2f}  {r['ma20']:>8.2f}  "
                            f"{r['atr_pct']:>6.2f}%  "
                            f"{d.get('r_squared', 0):>6.3f} "
                            f"{d.get('sim_trades', 0):>6} "
                            f"{d.get('sim_win_rate', 0)*100:>5.0f}% "
                            f"{d.get('valuation_percentile', 0)*100:>7.1f}%\n")
            else:
                f.write(f"\n*** 今日全市场无≥{threshold:.0f}分股票 ***\n")
            f.write("\n" + "=" * 90 + "\n")
            f.write(f"【{threshold-8:.0f}-{threshold:.0f}分区间（供参考，共{len(mid)}只）】\n\n")
            f.write(f"{'排名':>4}  {'代码':<10} {'名称':<12} {'评分':>6}  "
                    f"{'收盘价':>8}  {'MA20':>8}  {'ATR%':>7}\n")
            f.write("-" * 80 + "\n")
            for i, r in enumerate(mid[:30], 1):
                name_disp = r["name"][:10] if r["name"] else "-"
                f.write(f"{i:>4}  {r['code']:<10} {name_disp:<12} {r['score']:>6.1f}  "
                        f"{r['close']:>8.2f}  {r['ma20']:>8.2f}  {r['atr_pct']:>6.2f}%\n")
            if len(mid) > 30:
                f.write(f"... 其余 {len(mid)-30} 只略\n")

        print(f"\n[落盘] 结果已保存: {out_path}")

        # 7.7 构造 message
        if high:
            top_lines = []
            for i, r in enumerate(high[:10], 1):
                name_disp = r["name"] if r["name"] else "-"
                top_lines.append(
                    f"{i}. {r['code']} {name_disp} | 评分{r['score']:.1f} | "
                    f"收盘{r['close']:.2f} | MA20 {r['ma20']:.2f} | ATR% {r['atr_pct']:.2f}%"
                )
            top_str = "\n".join(top_lines)
            msg = (
                f"📊 bear市场门槛{threshold:.0f}分筛选完成（{target_date}）\n"
                f"有效评分 {len(results)} 只，≥{threshold:.0f}分 **{len(high)}只**，"
                f"{threshold-8:.0f}-{threshold:.0f}分区间 {len(mid)} 只。\n\n"
                f"Top{min(10,len(high))}：\n{top_str}\n\n"
                f"完整列表：[high_score_screen_{target_date.replace('-','')}.txt]"
                f"(computer://{os.path.abspath(out_path)})"
            )
        else:
            msg = (
                f"📊 bear市场门槛{threshold:.0f}分筛选完成（{target_date}）\n"
                f"有效评分 {len(results)} 只，**今日全市场无≥{threshold:.0f}分股票**。\n"
                f"{threshold-8:.0f}-{threshold:.0f}分区间有 {len(mid)} 只（供参考）。\n\n"
                f"完整结果：[high_score_screen_{target_date.replace('-','')}.txt]"
                f"(computer://{os.path.abspath(out_path)})"
            )

        if sdk:
            await sdk.submit_result(
                result_mode=actual_mode, status="success",
                message=msg,
                data={
                    "target_date": target_date,
                    "threshold": threshold,
                    "scored_total": len(results),
                    "high_score_count": len(high),
                    "mid_range_count": len(mid),
                    "output_path": os.path.abspath(out_path),
                    "top10": [
                        {"code": r["code"], "name": r["name"],
                         "score": r["score"], "close": r["close"]}
                        for r in high[:10]
                    ],
                },
            )
        else:
            print("\n[提交] CodeActSDK 不可用，结果仅打印+落盘。")

    except Exception as e:
        import traceback
        traceback.print_exc()
        if sdk:
            await sdk.submit_result(
                result_mode="notify", status="error",
                message=f"全市场评分筛选失败: {type(e).__name__}: {str(e)[:200]}",
            )
        raise


if __name__ == "__main__":
    asyncio.run(main())
