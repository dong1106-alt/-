#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
龟缠量化 v6_optimized 个股信号分析（只读分析，不写入交易/持仓数据）。

参数顺序：
1 result_mode: display_only/notify/no_reply/auto
2 codes: 逗号分隔股票代码，默认 sh603881,sz300895
3 analysis_date: YYYY-MM-DD，默认 2026-08-14
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===
import asyncio
import importlib.util
import json
import math
import os
import sys
import warnings
from datetime import datetime
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd
import requests

warnings.filterwarnings("ignore")

BASE_DIR = f"{_ROOT}"
STRATEGY_PATH = os.path.join(BASE_DIR, "龟缠量化v6_optimized.py")
CONFIG_PATH = os.path.join(BASE_DIR, "config/settings.yaml")
PARAMS_PATH = os.path.join(BASE_DIR, "data/optimal_params.json")
STATE_PATH = os.path.join(BASE_DIR, "data/market_state_timeline.json")
OUTPUT_DIR = "./codeact/output"
LOCAL_STOCK_DIR = os.path.join(BASE_DIR, "data/stocks")

DAY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{count},qfq"
WEEK_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},week,,,{count},qfq"
QUOTE_URL = "https://qt.gtimg.cn/q={codes}"


def safe_float(v: Any, default: float = float("nan")) -> float:
    try:
        if v is None or v == "":
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def fmt_num(v: Any, digits: int = 2, suffix: str = "") -> str:
    x = safe_float(v)
    if math.isnan(x):
        return "N/A"
    return f"{x:.{digits}f}{suffix}"


def fmt_pct(v: Any, digits: int = 2) -> str:
    x = safe_float(v)
    if math.isnan(x):
        return "N/A"
    return f"{x:.{digits}f}%"


def import_strategy():
    if not os.path.exists(STRATEGY_PATH):
        raise FileNotFoundError(f"策略文件不存在: {STRATEGY_PATH}")
    spec = importlib.util.spec_from_file_location("guichan_v6_opt", STRATEGY_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["guichan_v6_opt"] = mod
    spec.loader.exec_module(mod)
    return mod


def request_json(url: str, timeout: int = 20) -> Any:
    last_err = None
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    for _ in range(3):
        try:
            resp = requests.get(url, timeout=timeout, headers=headers)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            last_err = e
    raise RuntimeError(f"HTTP请求失败: {url} | {last_err}")


def _load_local_kline(code: str, count: int) -> pd.DataFrame | None:
    """从本地parquet读取K线（CodeAct环境可用）"""
    p = os.path.join(LOCAL_STOCK_DIR, f"{code}.parquet")
    if not os.path.exists(p):
        return None
    try:
        df = pd.read_parquet(p)
        if "date" not in df.columns or len(df) < 60:
            return None
        df["date"] = pd.to_datetime(df["date"])
        df = df.sort_values("date").tail(count).reset_index(drop=True)
        return df
    except Exception:
        return None


def _fetch_latest_bars_api(code: str, n: int = 5) -> list:
    """在线拉最近n根K线用于补齐parquet旧数据"""
    url = DAY_URL.format(code=code, count=n)
    try:
        raw = request_json(url)
        data = raw.get("data", {}).get(code, {})
        rows = data.get("qfqday") or data.get("day") or []
        return rows
    except Exception:
        return []


def fetch_kline(code: str, period: str, count: int) -> pd.DataFrame:
    # 1. 先尝试在线API（带UA）
    try:
        url = (DAY_URL if period == "day" else WEEK_URL).format(code=code, count=count)
        raw = request_json(url)
        data = raw.get("data", {}).get(code, {})
        if period == "day":
            key = "qfqday" if "qfqday" in data else "day"
        else:
            key = "qfqweek" if "qfqweek" in data else "week"
        rows = data.get(key) or []
        if rows:
            df = pd.DataFrame(rows)
            df = df.iloc[:, :6].copy()
            df.columns = ["date", "open", "close", "high", "low", "volume"]
            df["date"] = pd.to_datetime(df["date"])
            for c in ["open", "close", "high", "low", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
            if len(df) >= 60:
                return df
    except Exception:
        pass

    # 2. 回退：本地parquet + 在线补齐最近K线（仅日线）
    if period == "day":
        df = _load_local_kline(code, count)
        if df is not None and len(df) >= 60:
            latest = _fetch_latest_bars_api(code, 5)
            if latest:
                new_rows = []
                for r in latest:
                    d = pd.to_datetime(r[0])
                    if d > df["date"].max():
                        new_rows.append({"date": d, "open": float(r[1]), "close": float(r[2]),
                                         "high": float(r[3]), "low": float(r[4]), "volume": float(r[5])})
                if new_rows:
                    df = pd.concat([df, pd.DataFrame(new_rows)], ignore_index=True)
            return df.sort_values("date").tail(count).reset_index(drop=True)

    raise RuntimeError(f"{code} {period} K线获取失败（API和本地parquet均不可用）")


def fetch_quotes(codes: List[str]) -> Dict[str, Dict[str, Any]]:
    # 腾讯实时行情单次请求支持逗号分隔；返回 GBK 文本
    _headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    resp = requests.get(QUOTE_URL.format(codes=",".join(codes)), timeout=20, headers=_headers)
    resp.encoding = "gbk"
    text = resp.text.strip()
    result: Dict[str, Dict[str, Any]] = {}
    for line in text.split(";"):
        line = line.strip()
        if not line or "=" not in line:
            continue
        left, right = line.split("=", 1)
        code = left.replace("v_", "").strip()
        content = right.strip().strip('"')
        parts = content.split("~")
        if len(parts) < 50:
            result[code] = {"raw_parts": parts}
            continue
        result[code] = {
            "name": parts[1] or code,
            "code": parts[2] or code,
            "price": safe_float(parts[3]),
            "prev_close": safe_float(parts[4]),
            "open": safe_float(parts[5]),
            "volume_lot": safe_float(parts[6]),  # 手
            "amount_wan": safe_float(parts[37]),  # 万元
            "change": safe_float(parts[31]),
            "change_pct": safe_float(parts[32]),
            "high": safe_float(parts[33]),
            "low": safe_float(parts[34]),
            "turnover_rate": safe_float(parts[38]),
            "pe": safe_float(parts[39]),
            "amplitude": safe_float(parts[43]),
            "float_mcap_yi": safe_float(parts[44]),
            "total_mcap_yi": safe_float(parts[45]),
            "pb": safe_float(parts[46]),
            "time": parts[30] if len(parts) > 30 else "",
            "raw_parts": parts,
        }
    return result


def load_optimal_params(state: str) -> Dict[str, Any]:
    with open(PARAMS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    for r in data.get("results", []):
        if r.get("status") == "adopted" and r.get("state") == state:
            return r.get("params", {})
    return {}


def load_state_on(analysis_date: str) -> Dict[str, Any]:
    """读取/回推 analysis_date 当天市场状态；时间线不足时用上证指数回推。"""
    entry = None
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            timeline = json.load(f)
        for x in timeline:
            if x.get("date") == analysis_date:
                entry = x
                break
        if entry is None:
            prior = [x for x in timeline if x.get("date", "") <= analysis_date]
            if prior:
                entry = prior[-1]
    if entry:
        return entry
    # 回推：与 strategy generate_timeline 口径近似
    idx = fetch_kline("sh000001", "day", 250)
    idx = idx[idx["date"] <= pd.to_datetime(analysis_date)].tail(120).copy()
    if len(idx) < 65:
        return {"date": analysis_date, "state": "sideways", "close": np.nan, "ma60": np.nan}
    idx["ma60"] = idx["close"].rolling(60).mean()
    close = float(idx["close"].iloc[-1])
    ma60 = float(idx["ma60"].iloc[-1])
    deviation = (close - ma60) / ma60 if ma60 else 0
    ma60_5ago = float(idx["ma60"].iloc[-6])
    ma60_slope = (ma60 - ma60_5ago) / ma60_5ago if ma60_5ago else 0
    x = np.arange(60)
    y = idx["close"].tail(60).values
    r = np.corrcoef(x, y)[0, 1]
    r2_60 = float(r ** 2) if not np.isnan(r) else 0
    if ma60_slope < -0.005 and r2_60 > 0.4:
        state = "bear"
    elif ma60_slope > 0.003 and r2_60 > 0.4:
        state = "bull"
    elif abs(ma60_slope) < 0.001 and r2_60 < 0.2:
        state = "sideways"
    else:
        state = "transition"
    return {"date": analysis_date, "state": state, "close": close, "ma60": ma60,
            "ma60_slope": ma60_slope, "r2_60": r2_60, "deviation": deviation}


def apply_state_params(st_cfg: Dict[str, Any], params: Dict[str, Any]) -> Dict[str, Any]:
    cfg = dict(st_cfg)
    if not params:
        return cfg
    mapping = {
        "min_entry_score": "min_entry_score",
        "atr_multiplier": "base_atr_multiplier",
        "channel_base": "dc_period",
        "exit_base": "exit_period",
        "vol_ratio_high": "vol_ratio_high",
        "vol_ratio_low": "vol_ratio_low",
        "trend_str_high": "trend_str_high",
        "trend_str_low": "trend_str_low",
        "trail_stop_pct": "trail_stop_pct",
        "risk_value_ban": "risk_value_ban",
        "score_threshold": "score_threshold",
        "base_risk_pct": "base_risk_pct",
        "max_concurrent_positions": "max_concurrent_positions",
        "take_profit_base": "take_profit_base",
        "stop_multiplier_base": "stop_multiplier_base",
        "add_threshold_base": "add_threshold_base",
        "pre_filter_threshold": "pre_filter_threshold",
        "vol_pos_factor": "volatility_position_scale_factor",
        "vol_stack_threshold_large": "vol_stack_threshold_large",
        "vol_stack_threshold_mid": "vol_stack_threshold_mid",
        "vol_stack_threshold_small": "vol_stack_threshold_small",
    }
    for src, dst in mapping.items():
        if src in params:
            cfg[dst] = params[src]
    return cfg


def calc_extra_indicators(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ma5"] = out["close"].rolling(5).mean()
    out["ma10"] = out["close"].rolling(10).mean()
    # MA20/MA60 策略已算
    delta = out["close"].diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out["rsi14"] = 100 - 100 / (1 + rs)
    low9 = out["low"].rolling(9).min()
    high9 = out["high"].rolling(9).max()
    rsv = (out["close"] - low9) / (high9 - low9).replace(0, np.nan) * 100
    out["k"] = rsv.ewm(com=2, adjust=False).mean()
    out["d"] = out["k"].ewm(com=2, adjust=False).mean()
    out["j"] = 3 * out["k"] - 2 * out["d"]
    mid = out["close"].rolling(20).mean()
    std20 = out["close"].rolling(20).std(ddof=0)
    out["boll_mid"] = mid
    out["boll_up"] = mid + 2 * std20
    out["boll_low"] = mid - 2 * std20
    out["vol_ma5"] = out["volume"].rolling(5).mean()
    out["vol_ma20"] = out["volume"].rolling(20).mean()
    return out


def compute_signal_breakdown(last: pd.Series, prev: pd.Series, st_cfg: Dict[str, Any]) -> Dict[str, Any]:
    risk_val = safe_float(last.get("risk_value"), 50)
    mc_est = float(st_cfg.get("mc_est", 150))
    if mc_est >= 300:
        vs_threshold = st_cfg.get("vol_stack_threshold_large", 3.0)
    elif mc_est >= 100:
        vs_threshold = st_cfg.get("vol_stack_threshold_mid", 5.0)
    else:
        vs_threshold = st_cfg.get("vol_stack_threshold_small", 8.0)
    vs_penalty = vs_threshold * 0.3

    breakout = bool(pd.notna(last.get("dc_high")) and last["close"] > last["dc_high"])
    vol_ok = bool(last.get("vol_effective", False))
    chan_ok = bool(last.get("chan_buy", False) or last.get("chan_recent_type", "") != "")
    ma20_slope = safe_float(last.get("ma20_slope"), 0)
    price_above_ma60 = bool(pd.notna(last.get("ma60")) and last["close"] > last["ma60"])
    trend_ok = bool(st_cfg.get("use_trend_filter", True) and ma20_slope > 0.001 and price_above_ma60)
    money_strength = safe_float(last.get("money_strength"), 0)
    money_slope = safe_float(last.get("money_slope"), 0)
    money_ok = bool(st_cfg.get("use_money_filter", True) and money_strength > 0 and money_slope > 0)
    vol_stack = safe_float(last.get("vol_stack_ratio"), 1.0)
    weekly_trend = safe_float(last.get("weekly_trend"), 0)
    dc_agg = safe_float(last.get("dc_high_aggressive"), np.inf)
    aggressive_break = bool(last["close"] > dc_agg and last["volume"] > safe_float(last.get("vol_ma5"), 0) * 1.8)

    components = {
        "突破": 1.0 if breakout else 0.0,
        "有效放量": 1.0 if vol_ok else 0.0,
        "缠论买点": 2.0 if chan_ok else 0.0,
        "趋势过滤": 0.5 if trend_ok else 0.0,
        "资金过滤": 0.5 if money_ok else 0.0,
        "低风险值": 0.5 if risk_val < 33 else 0.0,
        "堆量比达标": 1.0 if vol_stack > vs_threshold else (-0.5 if vol_stack < vs_penalty else 0.0),
        "周线趋势": -1.0 if weekly_trend < 0 else (0.5 if weekly_trend > 0 else 0.0),
        "早鸟突破": 1.5 if aggressive_break else 0.0,
    }
    score = float(sum(components.values()))
    return {
        "components": components,
        "score": round(score, 2),
        "breakout": breakout,
        "vol_ok": vol_ok,
        "chan_ok": chan_ok,
        "trend_ok": trend_ok,
        "money_ok": money_ok,
        "risk_value": round(risk_val, 2),
        "vol_stack_ratio": round(vol_stack, 2),
        "weekly_trend": int(weekly_trend),
        "aggressive_break": aggressive_break,
        "dc_high": safe_float(last.get("dc_high")),
        "exit_low": safe_float(last.get("exit_low")),
        "dc_high_aggressive": dc_agg,
    }


def determine_conclusion(row: pd.Series, breakdown: Dict[str, Any]) -> Tuple[str, str]:
    buy = bool(row.get("buy_signal", False))
    sell = bool(row.get("sell_signal", False))
    signal_type = str(row.get("chan_buy_type") or row.get("chan_recent_type") or "")
    if buy and sell:
        return "观望（多空信号同日交织，等待确认）", signal_type or "综合冲突"
    if buy:
        return "买入", signal_type or "综合评分"
    if sell:
        return "卖出", "退出线/缠论卖点"
    if breakdown["aggressive_break"]:
        return "关注（早鸟突破但受风控约束）", "早鸟突破观察"
    return "持有/观望", "未达入场评分"


def build_stop_lines(last: pd.Series, adaptive: Dict[str, Any], st_cfg: Dict[str, Any], quote: Dict[str, Any]) -> Dict[str, float]:
    close = float(last["close"])
    atr = safe_float(last.get("atr"))
    # 与回测口径接近：自适应 stop_multiplier * 波动率分位乘数；单股无横截面时用波动率分位近似 1.0
    stop_mult = safe_float(adaptive.get("stop_multiplier"), st_cfg.get("base_atr_multiplier", 2.0))
    vol_perc = safe_float(last.get("volatility_percentile"), 0.5)
    vol_stop_min = st_cfg.get("volatility_stop_multiplier_min", 1.5)
    vol_stop_max = st_cfg.get("volatility_stop_multiplier_max", 3.0)
    vol_stop_mult = vol_stop_min + (vol_stop_max - vol_stop_min) * (0 if math.isnan(vol_perc) else vol_perc)
    atr_stop = close - atr * stop_mult * vol_stop_mult if not math.isnan(atr) else np.nan
    exit_low = safe_float(last.get("exit_low"))
    r2_20 = safe_float(adaptive.get("r2_20"), 0)
    if r2_20 > 0.15:
        tp_pct = 0.15
    elif r2_20 > 0.05:
        tp_pct = 0.25
    else:
        tp_pct = 0.40
    tp_threshold = safe_float(adaptive.get("take_profit_threshold"), st_cfg.get("take_profit_threshold", 0.40))
    return {
        "atr_stop_loss": round(atr_stop, 2) if not math.isnan(atr_stop) else np.nan,
        "exit_low": round(exit_low, 2) if not math.isnan(exit_low) else np.nan,
        "stop_multiplier": round(stop_mult * vol_stop_mult, 2),
        "take_profit_threshold_pct": round(tp_threshold * 100, 2),
        "dynamic_take_profit_pct": round(tp_pct * 100, 2),
        "take_profit_target": round(close * (1 + tp_threshold), 2),
    }


def compute_macro_factors(index_df: pd.DataFrame, analysis_ts: pd.Timestamp) -> Dict[str, Any]:
    idx = index_df[index_df["date"] <= analysis_ts].copy()
    if len(idx) < 65:
        return {"position_scale": 0.5, "market_slope": 0.0, "macro_r2_60": 0.0,
                "index_close": np.nan, "index_ma60": np.nan, "ma60_slope": 0.0,
                "trend_strength": 0.0}
    idx["ma20"] = idx["close"].rolling(20).mean()
    idx["ma60"] = idx["close"].rolling(60).mean()
    idx["trend_strength"] = (idx["close"] - idx["ma60"]) / idx["ma60"]
    idx["ma60_slope"] = (idx["ma60"] - idx["ma60"].shift(5)) / idx["ma60"].shift(5)
    idx["ma20_slope"] = (idx["ma20"] - idx["ma20"].shift(5)) / idx["ma20"].shift(5)
    idx["total_trend"] = idx["trend_strength"] + idx["ma60_slope"] * 10
    row = idx.iloc[-1]
    # R2
    x = np.arange(60)
    y = idx["close"].tail(60).values
    r = np.corrcoef(x, y)[0, 1]
    macro_r2 = float(r ** 2) if not np.isnan(r) else 0
    ma60_slope = safe_float(row["ma60_slope"], 0)
    total_trend = safe_float(row["total_trend"], 0)
    trend_strength = safe_float(row["trend_strength"], 0)
    # position scale approximate continuous scale
    sensitivity = 4.0
    if trend_strength < -0.03:
        pos_scale = 0.1
    else:
        if macro_r2 < 0.05 or ma60_slope < -0.005:
            cap = 0.3
        elif macro_r2 > 0.3 and ma60_slope > 0.005:
            cap = 1.0
        else:
            r2_eff = max(0.05, min(0.3, macro_r2))
            if r2_eff >= 0.2:
                cap = 0.8 + (r2_eff - 0.2) / 0.1 * 0.2
            elif r2_eff >= 0.1:
                cap = 0.6 + (r2_eff - 0.1) / 0.1 * 0.2
            else:
                cap = 0.4 + (r2_eff - 0.05) / 0.05 * 0.2
            if macro_r2 < 0.15:
                cap = min(cap, 0.5)
        raw = 0.5 + 0.5 * np.tanh(total_trend * sensitivity)
        pos_scale = raw * cap
    return {
        "position_scale": round(float(max(0.0, min(1.0, pos_scale))), 3),
        "market_slope": round(safe_float(row["ma20_slope"], 0), 5),
        "macro_r2_60": round(macro_r2, 4),
        "index_close": round(float(row["close"]), 2),
        "index_ma60": round(float(row["ma60"]), 2),
        "ma60_slope": round(ma60_slope, 5),
        "trend_strength": round(trend_strength, 4),
    }


def analyze_one(code: str, analysis_date: str, mod, base_cfg: Dict[str, Any],
                opt_params: Dict[str, Any], market_state: Dict[str, Any],
                index_df: pd.DataFrame, quotes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    analysis_ts = pd.to_datetime(analysis_date)
    day_df = fetch_kline(code, "day", 640)
    week_df = fetch_kline(code, "week", 100)
    day_df = day_df[day_df["date"] <= analysis_ts].copy()
    week_df = week_df[week_df["date"] <= analysis_ts].copy()
    if len(day_df) < 150:
        raise RuntimeError(f"日K线不足150条，实际{len(day_df)}条")
    if len(week_df) < 60:
        raise RuntimeError(f"周K线不足60条，实际{len(week_df)}条")

    st_cfg = apply_state_params(base_cfg["strategy"], opt_params)
    st_cfg["mc_est"] = mod.estimate_market_cap(float(day_df["close"].iloc[-1]))
    # 关闭外部数据依赖，本次只用腾讯K线
    base_cfg = dict(base_cfg)
    base_cfg["data_source"] = "coze"
    base_cfg["plot_enable"] = False

    df = mod.calc_indicators_vec(day_df.copy(), st_cfg)
    macro = compute_macro_factors(index_df, analysis_ts)
    df["position_scale"] = macro["position_scale"]
    df["market_slope"] = macro["market_slope"]

    # 自适应参数：完整按策略最后一根K线口径计算
    last_idx = len(df) - 1
    atr_series = df["atr"].iloc[max(0, last_idx - 20):last_idx + 1]
    atr_current = float(atr_series.iloc[-1])
    atr_ma20 = float(atr_series.mean()) if len(atr_series) else 1.0
    volatility_index = atr_current / atr_ma20 if atr_ma20 > 0 else 1.0
    adaptive = mod.calc_adaptive_params(df, last_idx, st_cfg, macro["market_slope"], volatility_index)

    # 缠论 + 增强信号
    df = mod.detect_chan_signals_optimized(df, pivot_win=5, chan_threshold=adaptive["chan_threshold"])
    df = mod.gen_signal_enhanced(df, st_cfg, adaptive)
    df = calc_extra_indicators(df)

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last
    quote = quotes.get(code, {})
    breakdown = compute_signal_breakdown(last, prev, st_cfg)
    conclusion, signal_type = determine_conclusion(last, breakdown)
    stops = build_stop_lines(last, adaptive, st_cfg, quote)

    # L2：个股120日R²；周线趋势已由策略内置
    closes_120 = df["close"].tail(120).values
    r2_120 = mod.calc_r2(closes_120.tolist(), lookback=120)
    # L3：波动率调整因子（近似单股自身波动率分位和ATR倍数）
    vol_perc = safe_float(last.get("volatility_percentile"), 0.5)
    l3_vol_pos_reduce = 1.0 - opt_params.get("vol_pos_factor", st_cfg.get("volatility_position_scale_factor", 0.15)) * vol_perc
    vol_stop_min = st_cfg.get("volatility_stop_multiplier_min", 1.5)
    vol_stop_max = st_cfg.get("volatility_stop_multiplier_max", 3.0)
    l3_stop_multiplier = vol_stop_min + (vol_stop_max - vol_stop_min) * vol_perc
    # 技术面评分：使用策略的整股质量评分
    tech_score = mod.calc_stock_quality_score(df.tail(250).copy(), st_cfg)

    vol5 = safe_float(last.get("vol_ma5"))
    vol20 = safe_float(last.get("vol_ma20"))
    vol_compare = vol5 / vol20 if vol20 and not math.isnan(vol20) else np.nan

    # 计算策略实际 required_score，便于展示为何未触发
    pos_scale_adj = max(0.0, min(1.0, float(last.get("position_scale", 0.5))))
    risk_val = breakdown["risk_value"]
    ts_vals = safe_float(last.get("trend_strength"), 0)
    risk_ban = min(92, max(72, 72 + ts_vals * 40))
    if risk_val > risk_ban:
        pos_scale_adj *= 0.6
    min_score = float(st_cfg.get("min_entry_score", 1.7))
    max_score = float(st_cfg.get("max_entry_score", 3.5))
    is_extreme = bool(st_cfg.get("extreme_value_exemption", True) and safe_float(last.get("valuation_percentile"), 0.5) < 0.10)
    adjusted_min = max(0.5, min_score - 0.5) if is_extreme else min_score
    required_score = adjusted_min + (max_score - adjusted_min) * (1.0 - pos_scale_adj) * 0.6
    required_score = max(adjusted_min, min(max_score, required_score))

    return {
        "code": code,
        "name": quote.get("name", code),
        "analysis_date": analysis_date,
        "quote": quote,
        "market_state": market_state,
        "macro": macro,
        "kline": {
            "day_count": int(len(day_df)),
            "week_count": int(len(week_df)),
            "last_day_date": last["date"].strftime("%Y-%m-%d"),
            "last_week_date": week_df.iloc[-1]["date"].strftime("%Y-%m-%d"),
        },
        "signal": {
            "conclusion": conclusion,
            "signal_type": signal_type,
            "entry_score": round(safe_float(last.get("entry_score")), 2),
            "required_score": round(required_score, 2),
            "score_threshold": st_cfg.get("score_threshold"),
            "min_entry_score": min_score,
            "max_entry_score": max_score,
            "buy_signal": bool(last.get("buy_signal", False)),
            "sell_signal": bool(last.get("sell_signal", False)),
            "breakdown": breakdown,
        },
        "dimension_scores": {
            "L1_macro_position_scale": macro["position_scale"],
            "L1_macro_r2_60": macro["macro_r2_60"],
            "L1_market_slope": macro["market_slope"],
            "L2_stock_r2_120": round(r2_120, 4),
            "L3_volatility_percentile": round(vol_perc, 4),
            "L3_vol_position_reduce": round(l3_vol_pos_reduce, 4),
            "L3_stop_multiplier_base": round(l3_stop_multiplier, 4),
            "L4_take_profit_threshold_pct": round(adaptive["take_profit_threshold"] * 100, 2),
            "L4_r2_20": round(adaptive["r2_20"], 4),
            "technical_quality_score": tech_score,
        },
        "adaptive_params": {k: (round(v, 5) if isinstance(v, (int, float)) and math.isfinite(v) else v)
                            for k, v in adaptive.items()},
        "indicators": {
            "close": round(float(last["close"]), 2),
            "open": round(float(last["open"]), 2),
            "high": round(float(last["high"]), 2),
            "low": round(float(last["low"]), 2),
            "prev_close": round(float(prev["close"]), 2),
            "change_pct": round((float(last["close"]) / float(prev["close"]) - 1) * 100, 2),
            "ma5": round(safe_float(last.get("ma5")), 2),
            "ma10": round(safe_float(last.get("ma10")), 2),
            "ma20": round(safe_float(last.get("ma20")), 2),
            "ma60": round(safe_float(last.get("ma60")), 2),
            "dif": round(safe_float(last.get("dif")), 4),
            "dea": round(safe_float(last.get("dea")), 4),
            "macd_hist": round(safe_float(last.get("macd_hist")), 4),
            "rsi14": round(safe_float(last.get("rsi14")), 2),
            "kdj_k": round(safe_float(last.get("k")), 2),
            "kdj_d": round(safe_float(last.get("d")), 2),
            "kdj_j": round(safe_float(last.get("j")), 2),
            "boll_up": round(safe_float(last.get("boll_up")), 2),
            "boll_mid": round(safe_float(last.get("boll_mid")), 2),
            "boll_low": round(safe_float(last.get("boll_low")), 2),
            "atr14": round(safe_float(last.get("atr")), 2),
            "volume": int(last["volume"]),
            "vol_ma5": round(vol5, 0),
            "vol_ma20": round(vol20, 0),
            "vol_5_vs_20": round(vol_compare, 2) if not math.isnan(vol_compare) else None,
            "risk_value": round(safe_float(last.get("risk_value")), 2),
            "vol_stack_ratio": round(safe_float(last.get("vol_stack_ratio")), 2),
            "weekly_trend": int(safe_float(last.get("weekly_trend"), 0)),
            "dc_high": round(safe_float(last.get("dc_high")), 2),
            "exit_low": round(safe_float(last.get("exit_low")), 2),
            "dc_high_aggressive": round(safe_float(last.get("dc_high_aggressive")), 2),
        },
        "stop_lines": stops,
        "config_snapshot": {
            "state": market_state.get("state"),
            "dc_period": st_cfg.get("dc_period"),
            "exit_period": st_cfg.get("exit_period"),
            "atr_period": st_cfg.get("atr_period"),
            "base_atr_multiplier": st_cfg.get("base_atr_multiplier"),
            "vol_ratio_high": st_cfg.get("vol_ratio_high"),
            "vol_ratio_low": st_cfg.get("vol_ratio_low"),
        },
    }


def operation_advice(r: Dict[str, Any]) -> str:
    sig = r["signal"]
    ind = r["indicators"]
    dim = r["dimension_scores"]
    if sig["buy_signal"]:
        if sig["signal_type"] in ("早鸟突破", "纯突破"):
            return f"策略给出买入信号（{sig['signal_type']}），但建议分批试仓，严格以ATR止损/退出线控制风险；当前L1宏观仓位系数{dim['L1_macro_position_scale']}，不宜满仓。"
        return f"策略给出买入信号（{sig['signal_type']}），可按组合风险规则分批参与，止损参考{fmt_num(r['stop_lines']['atr_stop_loss'])}元或退出线{fmt_num(r['stop_lines']['exit_low'])}元。"
    if sig["sell_signal"]:
        return "策略给出卖出信号，若已有持仓应优先降低仓位或离场观望，避免与退出信号对抗。"
    if ind["close"] < ind["ma60"]:
        return "价格仍在MA60下方，趋势过滤未通过，建议继续观望，等待重新站上均线并放量确认。"
    if sig["entry_score"] < sig["required_score"]:
        return f"未达到策略实际入场门槛（{sig['entry_score']} < {sig['required_score']}），建议持有观望或等待放量突破/缠论买点确认。"
    return "信号不明确，建议观望，等待更清晰的突破、回踩或缠论买点。"


def render_report(results: List[Dict[str, Any]], errors: List[Dict[str, str]], analysis_date: str) -> str:
    lines = []
    lines.append(f"# 龟缠量化v6_optimized 个股信号分析（{analysis_date}收盘）")
    lines.append("")
    lines.append("> 说明：本报告只做策略信号与技术指标分析，不修改任何持仓/交易文件；8/15-8/16为周末，无交易。")
    lines.append("")
    for r in results:
        q = r["quote"]
        sig = r["signal"]
        ind = r["indicators"]
        dim = r["dimension_scores"]
        stops = r["stop_lines"]
        lines.append(f"## {r['name']}（{r['code']}）")
        lines.append(f"- 收盘价：**{fmt_num(ind['close'])}元**，涨跌幅：**{fmt_pct(ind['change_pct'])}**（昨收{fmt_num(ind['prev_close'])}）")
        if q:
            lines.append(f"- 基本行情：总市值约{fmt_num(q.get('total_mcap_yi'))}亿元，流通市值约{fmt_num(q.get('float_mcap_yi'))}亿元，换手{fmt_pct(q.get('turnover_rate'))}，PE {fmt_num(q.get('pe'))}，PB {fmt_num(q.get('pb'))}")
        lines.append(f"- K线数据：日K {r['kline']['day_count']}条（截至{r['kline']['last_day_date']}），周K {r['kline']['week_count']}条（截至{r['kline']['last_week_date']}）")
        lines.append("")
        lines.append("### 策略信号结论")
        lines.append(f"- 结论：**{sig['conclusion']}**")
        lines.append(f"- 信号类型：{sig['signal_type']}")
        lines.append(f"- 入场评分：{sig['entry_score']}；实际通过门槛约：{sig['required_score']}（最低入场分{sig['min_entry_score']}，最大分{sig['max_entry_score']}）")
        lines.append(f"- Buy/Sell：{sig['buy_signal']} / {sig['sell_signal']}")
        lines.append("")
        lines.append("### 评分明细")
        for k, v in sig["breakdown"]["components"].items():
            lines.append(f"- {k}: {v:+.1f}")
        lines.append(f"- 合计：{sig['breakdown']['score']}")
        lines.append("")
        lines.append("### L1-L4与技术面")
        lines.append(f"- L1宏观：仓位系数 {dim['L1_macro_position_scale']}，上证60日R² {dim['L1_macro_r2_60']}，MA20斜率 {dim['L1_market_slope']}，市场状态 {r['market_state'].get('state')}")
        lines.append(f"- L2个股R²：120日R² {dim['L2_stock_r2_120']}")
        lines.append(f"- L3波动率：分位 {dim['L3_volatility_percentile']}，仓位折减 {dim['L3_vol_position_reduce']}，止损倍数基数 {dim['L3_stop_multiplier_base']}")
        lines.append(f"- L4止盈：20日R² {dim['L4_r2_20']}，动态止盈阈值 {dim['L4_take_profit_threshold_pct']}%")
        lines.append(f"- 技术面质量评分：{dim['technical_quality_score']}")
        lines.append("")
        lines.append("### 完整技术指标")
        lines.append("| 指标 | 数值 | 指标 | 数值 |")
        lines.append("|---|---:|---|---:|")
        rows = [
            ("MA5", fmt_num(ind["ma5"]), "MA10", fmt_num(ind["ma10"])),
            ("MA20", fmt_num(ind["ma20"]), "MA60", fmt_num(ind["ma60"])),
            ("DIF", fmt_num(ind["dif"], 4), "DEA", fmt_num(ind["dea"], 4)),
            ("MACD柱", fmt_num(ind["macd_hist"], 4), "RSI14", fmt_num(ind["rsi14"])),
            ("KDJ-K", fmt_num(ind["kdj_k"]), "KDJ-D", fmt_num(ind["kdj_d"])),
            ("KDJ-J", fmt_num(ind["kdj_j"]), "ATR14", fmt_num(ind["atr14"])),
            ("布林上轨", fmt_num(ind["boll_up"]), "布林中轨", fmt_num(ind["boll_mid"])),
            ("布林下轨", fmt_num(ind["boll_low"]), "风险值", fmt_num(ind["risk_value"])),
            ("成交量", f"{ind['volume']}", "5日均量", fmt_num(ind["vol_ma5"], 0)),
            ("20日均量", fmt_num(ind["vol_ma20"], 0), "5日/20日量比", fmt_num(ind["vol_5_vs_20"])),
            ("唐奇安上轨", fmt_num(ind["dc_high"]), "退出线下轨", fmt_num(ind["exit_low"])),
            ("10日早鸟上轨", fmt_num(ind["dc_high_aggressive"]), "周线趋势", {-1: "下降", 0: "走平", 1: "上升"}.get(ind["weekly_trend"], "N/A")),
        ]
        for a, b, c, d in rows:
            lines.append(f"| {a} | {b} | {c} | {d} |")
        lines.append("")
        lines.append("### 止损止盈建议")
        lines.append(f"- ATR止损线：{fmt_num(stops['atr_stop_loss'])}元（综合止损倍数约{stops['stop_multiplier']}）")
        lines.append(f"- 策略退出线：{fmt_num(stops['exit_low'])}元")
        lines.append(f"- 动态止盈阈值：{stops['dynamic_take_profit_pct']}%；止盈目标价约：{fmt_num(stops['take_profit_target'])}元（按阈值{stops['take_profit_threshold_pct']}%）")
        lines.append("")
        lines.append("### 操作建议")
        lines.append(f"- {operation_advice(r)}")
        lines.append("")
    if errors:
        lines.append("## 数据获取/分析失败")
        for e in errors:
            lines.append(f"- {e['code']}: {e['error']}")
        lines.append("")
    lines.append("### 口径说明")
    lines.append("- 日/周K线来自腾讯前复权接口；行情来自腾讯实时行情接口（2026-08-14收盘快照）。")
    lines.append("- 市场状态优先读取本地 market_state_timeline.json；L1宏观仓位、L2/L3/L4参数按策略源码口径计算。")
    lines.append("- 以上为策略信号和技术分析，不构成投资建议。")
    return "\n".join(lines)


async def main():
    result_mode = sys.argv[1] if len(sys.argv) > 1 else "display_only"
    codes_arg = sys.argv[2] if len(sys.argv) > 2 else "sh603881,sz300895"
    analysis_date = sys.argv[3] if len(sys.argv) > 3 else "2026-08-14"
    codes = [x.strip() for x in codes_arg.split(",") if x.strip()]
    print(f"[参数] result_mode={result_mode}, codes={codes}, analysis_date={analysis_date}")

    # 不导入 sdk 也可生成报告；但 CodeAct 要求脚本必须提交
    from codeact_sdk import CodeActSDK
    sdk = CodeActSDK()
    actual_mode = result_mode if result_mode != "auto" else "display_only"

    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        mod = import_strategy()
        cfg = mod.DEFAULT_CONFIG.copy()
        cfg["strategy"] = dict(cfg["strategy"])
        cfg["plot_enable"] = False

        market_state = load_state_on(analysis_date)
        state_name = market_state.get("state", "sideways")
        opt_params = load_optimal_params(state_name)
        quotes = fetch_quotes(codes)
        index_df = fetch_kline("sh000001", "day", 640)
        index_df = index_df[index_df["date"] <= pd.to_datetime(analysis_date)].copy()

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []
        for code in codes:
            try:
                print(f"[分析] {code} ...")
                r = analyze_one(code, analysis_date, mod, cfg, opt_params, market_state, index_df, quotes)
                results.append(r)
                print(f"[完成] {code} {r['signal']['conclusion']} score={r['signal']['entry_score']}")
            except Exception as e:
                errors.append({"code": code, "error": str(e)})
                print(f"[失败] {code}: {e}")

        if not results and errors:
            raise RuntimeError("; ".join([f"{x['code']}:{x['error']}" for x in errors]))

        report = render_report(results, errors, analysis_date)
        ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(OUTPUT_DIR, f"guichan_signal_analysis_{ts_tag}.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)

        summary_parts = []
        for r in results:
            summary_parts.append(
                f"{r['name']}({r['code']})：{r['signal']['conclusion']}，评分{r['signal']['entry_score']}，"
                f"收盘{fmt_num(r['indicators']['close'])}元（{fmt_pct(r['indicators']['change_pct'])}）"
            )
        message = "\n".join([
            f"龟缠v6信号分析完成（{analysis_date}收盘）：",
            *[f"- {x}" for x in summary_parts],
        ])
        if errors:
            message += "\n失败：" + "；".join([f"{x['code']} {x['error']}" for x in errors])
        message += f"\n完整报告：[guichan_signal_analysis_{ts_tag}.md](computer://{os.path.abspath(report_path)})"

        await sdk.submit_result(
            result_mode=actual_mode,
            status="success",
            message=message,
            data={
                "report_path": report_path,
                "codes": codes,
                "analysis_date": analysis_date,
                "market_state": state_name,
                "success_count": len(results),
                "failed_items": errors,
                "results_brief": [
                    {
                        "code": r["code"],
                        "name": r["name"],
                        "conclusion": r["signal"]["conclusion"],
                        "signal_type": r["signal"]["signal_type"],
                        "entry_score": r["signal"]["entry_score"],
                        "close": r["indicators"]["close"],
                        "change_pct": r["indicators"]["change_pct"],
                    } for r in results
                ],
            },
        )
    except Exception as e:
        await sdk.submit_result(
            result_mode="notify",
            status="error",
            message=f"龟缠信号分析失败：{e}",
            data={"codes": codes, "analysis_date": analysis_date},
        )
        raise

if __name__ == "__main__":
    asyncio.run(main())