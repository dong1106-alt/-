#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
龟缠量化 v6_optimized 研报A股名单批量信号扫描。

复用 guichan_v6_stock_signal_analysis 模块的完整策略信号逻辑，
对西部证券8月5日AI算力上游材料研报建议关注的全部A股重新跑最新信号，
输出总表 + 分类点评。

参数顺序：
1 result_mode: display_only / notify / no_reply / auto
2 analysis_date: YYYY-MM-DD（默认 2026-08-14）
3 stock_list_path: 股票名单JSON路径，默认使用内置22只研报名单

股票名单JSON格式: [{"code":"sz300285","name":"国瓷材料","sector":"PCB材料"}, ...]
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
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")

# ---- 路径 ----
BASE_DIR = f"{_ROOT}"
SCRIPTS_DIR = os.path.join(BASE_DIR, "codeact/scripts")
OUTPUT_DIR = "./codeact/output"

# 将 scripts 目录加入 sys.path 以便 import 已有分析模块
if SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, SCRIPTS_DIR)

import guichan_v6_stock_signal_analysis as sig_mod  # noqa: E402

# ---- 内置研报名单（22只） ----
DEFAULT_STOCKS: List[Dict[str, str]] = [
    # PCB材料
    {"code": "sz300285", "name": "国瓷材料", "sector": "PCB材料"},
    {"code": "sz301028", "name": "国际复材", "sector": "PCB材料"},
    {"code": "sh603002", "name": "宏昌电子", "sector": "PCB材料"},
    {"code": "sh601208", "name": "东材科技", "sector": "PCB材料"},
    {"code": "sh600500", "name": "中化国际", "sector": "PCB材料"},
    # 半导体材料
    {"code": "sz300054", "name": "鼎龙股份", "sector": "半导体材料"},
    {"code": "sh600378", "name": "昊华科技", "sector": "半导体材料"},
    {"code": "sz002409", "name": "雅克科技", "sector": "半导体材料"},
    {"code": "sh603650", "name": "彤程新材", "sector": "半导体材料"},
    # 封测材料
    {"code": "sz300398", "name": "飞凯材料", "sector": "封测材料"},
    # 光通信/其他
    {"code": "sz002428", "name": "云南锗业", "sector": "光通信/其他"},
    {"code": "sh600141", "name": "兴发集团", "sector": "光通信/其他"},
    {"code": "sh603938", "name": "三孚股份", "sector": "光通信/其他"},
    {"code": "sz002254", "name": "泰和新材", "sector": "光通信/其他"},
    # 氟化工/冷却液
    {"code": "sh603379", "name": "三美股份", "sector": "氟化工/冷却液"},
    {"code": "sz300037", "name": "新宙邦", "sector": "氟化工/冷却液"},
    {"code": "sh600160", "name": "巨化股份", "sector": "氟化工/冷却液"},
    {"code": "sz002585", "name": "双星新材", "sector": "氟化工/冷却液"},
    # 之前无数据的4只（科创板，688为上交所）
    {"code": "sz688300", "name": "联瑞新材", "sector": "PCB材料(科创)"},
    {"code": "sh688535", "name": "华海诚科", "sector": "封测材料(科创)"},
    {"code": "sh688268", "name": "华特气体", "sector": "半导体材料(科创)"},
    {"code": "sh688106", "name": "金宏气体", "sector": "半导体材料(科创)"},
]


def safe_float(v: Any, default: float = float("nan")) -> float:
    try:
        if v is None or v == "":
            return default
        x = float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default


def load_stock_list(path: Optional[str]) -> List[Dict[str, str]]:
    if not path:
        return DEFAULT_STOCKS
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    out = []
    for item in data:
        out.append({
            "code": str(item["code"]).strip(),
            "name": str(item.get("name", item["code"])).strip(),
            "sector": str(item.get("sector", "未分类")).strip(),
        })
    return out


def fetch_kline_with_fallback(code: str, period: str, count: int) -> pd.DataFrame:
    """获取K线；对 sz688xxx 自动回退 sh688xxx。"""
    try:
        return sig_mod.fetch_kline(code, period, count)
    except Exception as e:
        if code.startswith("sz688"):
            alt = "sh" + code[2:]
            print(f"[回退] {code} -> {alt} ({e})")
            return sig_mod.fetch_kline(alt, period, count)
        raise


def calc_period_change(day_df: pd.DataFrame, analysis_ts: pd.Timestamp, n: int) -> Optional[float]:
    """计算最近n个交易日涨跌幅(%)，基于analysis_date当日收盘与n个交易日前收盘。"""
    df = day_df[day_df["date"] <= analysis_ts].copy()
    if len(df) < n + 1:
        return None
    close_now = float(df["close"].iloc[-1])
    close_then = float(df["close"].iloc[-(n + 1)])
    if close_then == 0:
        return None
    return round((close_now / close_then - 1) * 100, 2)


def weekly_trend_label(v: int) -> str:
    return {1: "上升", 0: "中性", -1: "下降"}.get(v, "N/A")


def classify(result: Dict[str, Any]) -> str:
    """按筛选标准分类。"""
    sig = result["signal"]
    if sig["buy_signal"]:
        return "可考虑买入"
    score = safe_float(sig.get("entry_score"), 0)
    wt = int(sig["breakdown"].get("weekly_trend", 0))
    if score >= 2.0 and wt >= 0:
        return "接近信号，可关注"
    return "暂不考虑"


def analyze_one_stock(stock: Dict[str, str], analysis_date: str,
                      mod, base_cfg: Dict[str, Any], opt_params: Dict[str, Any],
                      market_state: Dict[str, Any], index_df: pd.DataFrame,
                      quotes: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """线程内分析单只股票，返回精简结果。"""
    code = stock["code"]
    name = stock["name"]
    sector = stock["sector"]
    analysis_ts = pd.to_datetime(analysis_date)

    # 使用带fallback的K线获取
    day_df = fetch_kline_with_fallback(code, "day", 640)
    week_df = fetch_kline_with_fallback(code, "week", 100)

    # 确定实际使用的代码（sz688可能回退为sh688）
    actual_code = code
    if code.startswith("sz688") and len(day_df) > 0:
        # 判断是否回退过：通过比较返回数据是否成功
        # fetch_kline_with_fallback 内部已处理，这里用代码标记
        actual_code = code  # quote仍用原code尝试

    day_df = day_df[day_df["date"] <= analysis_ts].copy()
    week_df = week_df[week_df["date"] <= analysis_ts].copy()

    if len(day_df) < 150:
        raise RuntimeError(f"日K线不足150条，实际{len(day_df)}条")
    if len(week_df) < 60:
        raise RuntimeError(f"周K线不足60条，实际{len(week_df)}条")

    st_cfg = sig_mod.apply_state_params(base_cfg["strategy"], opt_params)
    st_cfg["mc_est"] = mod.estimate_market_cap(float(day_df["close"].iloc[-1]))
    local_cfg = dict(base_cfg)
    local_cfg["data_source"] = "coze"
    local_cfg["plot_enable"] = False

    df = mod.calc_indicators_vec(day_df.copy(), st_cfg)
    macro = sig_mod.compute_macro_factors(index_df, analysis_ts)
    df["position_scale"] = macro["position_scale"]
    df["market_slope"] = macro["market_slope"]

    last_idx = len(df) - 1
    atr_series = df["atr"].iloc[max(0, last_idx - 20):last_idx + 1]
    atr_current = float(atr_series.iloc[-1])
    atr_ma20 = float(atr_series.mean()) if len(atr_series) else 1.0
    volatility_index = atr_current / atr_ma20 if atr_ma20 > 0 else 1.0
    adaptive = mod.calc_adaptive_params(df, last_idx, st_cfg, macro["market_slope"], volatility_index)

    df = mod.detect_chan_signals_optimized(df, pivot_win=5, chan_threshold=adaptive["chan_threshold"])
    df = mod.gen_signal_enhanced(df, st_cfg, adaptive)
    df = sig_mod.calc_extra_indicators(df)

    last = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else last

    # quote可能用回退后的代码
    quote = quotes.get(code, {})
    if not quote and code.startswith("sz688"):
        quote = quotes.get("sh" + code[2:], {})

    breakdown = sig_mod.compute_signal_breakdown(last, prev, st_cfg)
    conclusion, signal_type = sig_mod.determine_conclusion(last, breakdown)

    close = float(last["close"])
    atr14 = safe_float(last.get("atr"))
    atr_pct = round(atr14 / close * 100, 2) if close and not math.isnan(atr14) else None

    change_5d = calc_period_change(day_df, analysis_ts, 5)
    change_20d = calc_period_change(day_df, analysis_ts, 20)

    vol5 = safe_float(last.get("vol_ma5"))
    vol20 = safe_float(last.get("vol_ma20"))
    vol_ratio = round(vol5 / vol20, 2) if vol20 and not math.isnan(vol20) and vol20 > 0 else None

    # 当日涨跌幅
    change_pct = round((close / float(prev["close"]) - 1) * 100, 2)

    # 换手率从quote取
    turnover = quote.get("turnover_rate") if quote else None

    result = {
        "code": code,
        "actual_code": actual_code,
        "name": name,
        "sector": sector,
        "close": round(close, 2),
        "change_pct": change_pct,
        "turnover_rate": round(turnover, 2) if turnover and not math.isnan(turnover) else None,
        "score": round(safe_float(last.get("entry_score")), 2),
        "buy_signal": bool(last.get("buy_signal", False)),
        "sell_signal": bool(last.get("sell_signal", False)),
        "conclusion": conclusion,
        "signal_type": signal_type,
        "weekly_trend": int(safe_float(last.get("weekly_trend"), 0)),
        "atr_pct": atr_pct,
        "change_5d": change_5d,
        "change_20d": change_20d,
        "vol_ratio": vol_ratio,
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
        "boll_pos": None,  # 稍后计算
        "risk_value": round(safe_float(last.get("risk_value")), 2),
        "vol_stack_ratio": round(safe_float(last.get("vol_stack_ratio")), 2),
        "breakdown_components": breakdown["components"],
        "breakdown_score": breakdown["score"],
        "dc_high": round(safe_float(last.get("dc_high")), 2),
        "exit_low": round(safe_float(last.get("exit_low")), 2),
        "aggressive_break": breakdown["aggressive_break"],
        "kline_day_count": int(len(day_df)),
        "kline_week_count": int(len(week_df)),
        "last_day_date": last["date"].strftime("%Y-%m-%d"),
    }

    # 布林位置: 0=下轨 1=上轨
    boll_up = safe_float(last.get("boll_up"))
    boll_low = safe_float(last.get("boll_low"))
    if not math.isnan(boll_up) and not math.isnan(boll_low) and boll_up != boll_low:
        result["boll_pos"] = round((close - boll_low) / (boll_up - boll_low), 2)

    # 分类
    result["verdict"] = classify({"signal": {
        "buy_signal": result["buy_signal"],
        "entry_score": result["score"],
        "breakdown": {"weekly_trend": result["weekly_trend"]},
    }})

    return result


def render_table(results: List[Dict[str, Any]]) -> str:
    """渲染总表Markdown。"""
    lines = []
    lines.append("| 股票 | 板块 | 收盘 | 涨跌幅 | 策略评分 | Buy | 周线趋势 | ATR% | 5日涨跌 | 20日涨跌 | 量比 | 判定 |")
    lines.append("|---|---|---:|---:|---:|:---:|:---:|---:|---:|---:|---:|---|")
    for r in sorted(results, key=lambda x: (
        {"可考虑买入": 0, "接近信号，可关注": 1, "暂不考虑": 2}[x["verdict"]],
        -x["score"]
    )):
        chg = f"{r['change_pct']:+.2f}%" if r['change_pct'] is not None else "N/A"
        atr = f"{r['atr_pct']:.2f}%" if r['atr_pct'] is not None else "N/A"
        ch5 = f"{r['change_5d']:+.2f}%" if r['change_5d'] is not None else "N/A"
        ch20 = f"{r['change_20d']:+.2f}%" if r['change_20d'] is not None else "N/A"
        vr = f"{r['vol_ratio']:.2f}" if r['vol_ratio'] is not None else "N/A"
        buy_mark = "✅" if r["buy_signal"] else "—"
        wt = weekly_trend_label(r["weekly_trend"])
        verdict = r["verdict"]
        if verdict == "可考虑买入":
            verdict = "**🟢 可考虑买入**"
        elif verdict == "接近信号，可关注":
            verdict = "**🟡 接近信号**"
        else:
            verdict = "⚪ 暂不考虑"
        lines.append(
            f"| {r['name']}({r['code']}) | {r['sector']} | {r['close']:.2f} | {chg} | "
            f"{r['score']:.2f} | {buy_mark} | {wt} | {atr} | {ch5} | {ch20} | {vr} | {verdict} |"
        )
    return "\n".join(lines)


def render_commentary(results: List[Dict[str, Any]]) -> str:
    """对可考虑买入和接近信号的股票给出简要点评。"""
    buy_list = [r for r in results if r["verdict"] == "可考虑买入"]
    watch_list = [r for r in results if r["verdict"] == "接近信号，可关注"]

    lines = []
    if buy_list:
        lines.append("## 🟢 可考虑买入")
        lines.append("")
        for r in buy_list:
            comp = r["breakdown_components"]
            pos_items = [k for k, v in comp.items() if v > 0]
            neg_items = [k for k, v in comp.items() if v < 0]
            lines.append(f"### {r['name']}（{r['code']}）— {r['sector']}")
            lines.append(f"- 收盘 {r['close']:.2f}元，当日{r['change_pct']:+.2f}%，5日{r['change_5d']:+.2f}%，20日{r['change_20d']:+.2f}%")
            lines.append(f"- 策略评分 {r['score']:.2f}，信号类型：{r['signal_type']}")
            lines.append(f"- MA5/10/20/60：{r['ma5']}/{r['ma10']}/{r['ma20']}/{r['ma60']}")
            lines.append(f"- MACD(DIF/DEA/柱)：{r['dif']}/{r['dea']}/{r['macd_hist']}，RSI14 {r['rsi14']}，KDJ({r['kdj_k']}/{r['kdj_d']}/{r['kdj_j']})")
            lines.append(f"- ATR% {r['atr_pct']:.2f}%，量比(5/20) {r['vol_ratio']:.2f}，换手率 {r['turnover_rate']}%")
            lines.append(f"- 布林位置 {r['boll_pos']}（0=下轨,1=上轨），风险值 {r['risk_value']}")
            lines.append(f"- 加分项：{', '.join(pos_items) if pos_items else '无'}")
            if neg_items:
                lines.append(f"- 扣分项：{', '.join(neg_items)}")
            lines.append(f"- 唐奇安上轨 {r['dc_high']}，退出线 {r['exit_low']}")
            lines.append("")
    else:
        lines.append("## 🟢 可考虑买入")
        lines.append("")
        lines.append("**本次扫描无任何股票触发策略Buy信号。**")
        lines.append("")

    if watch_list:
        lines.append("## 🟡 接近信号，可关注")
        lines.append("")
        for r in watch_list:
            comp = r["breakdown_components"]
            pos_items = [k for k, v in comp.items() if v > 0]
            neg_items = [k for k, v in comp.items() if v < 0]
            lines.append(f"### {r['name']}（{r['code']}）— {r['sector']}")
            lines.append(f"- 收盘 {r['close']:.2f}元，当日{r['change_pct']:+.2f}%，5日{r['change_5d']:+.2f}%，20日{r['change_20d']:+.2f}%")
            lines.append(f"- 策略评分 {r['score']:.2f}（未达Buy触发线），周线趋势{weekly_trend_label(r['weekly_trend'])}")
            lines.append(f"- MA5/10/20/60：{r['ma5']}/{r['ma10']}/{r['ma20']}/{r['ma60']}")
            lines.append(f"- MACD(DIF/DEA/柱)：{r['dif']}/{r['dea']}/{r['macd_hist']}，RSI14 {r['rsi14']}，KDJ({r['kdj_k']}/{r['kdj_d']}/{r['kdj_j']})")
            lines.append(f"- ATR% {r['atr_pct']:.2f}%，量比(5/20) {r['vol_ratio']:.2f}，换手率 {r['turnover_rate']}%")
            lines.append(f"- 布林位置 {r['boll_pos']}，风险值 {r['risk_value']}")
            lines.append(f"- 加分项：{', '.join(pos_items) if pos_items else '无'}")
            if neg_items:
                lines.append(f"- 扣分项：{', '.join(neg_items)}")
            lines.append(f"- 唐奇安上轨 {r['dc_high']}（距上轨{((r['dc_high']/r['close'])-1)*100:.2f}%），退出线 {r['exit_low']}")
            lines.append("")

    # 如果全部无Buy，指出最接近的2-3只
    if not buy_list and not watch_list:
        # 按评分排序取前3
        sorted_r = sorted(results, key=lambda x: -x["score"])[:3]
        lines.append("## 📌 相对最接近信号的标的")
        lines.append("")
        for r in sorted_r:
            comp = r["breakdown_components"]
            pos_items = [k for k, v in comp.items() if v > 0]
            lines.append(f"- **{r['name']}（{r['code']}）**：评分{r['score']:.2f}，周线{weekly_trend_label(r['weekly_trend'])}，"
                         f"收盘{r['close']:.2f}元，5日{r['change_5d']:+.2f}%，量比{r['vol_ratio']:.2f}，"
                         f"加分项[{', '.join(pos_items)}]，距唐奇安上轨{((r['dc_high']/r['close'])-1)*100:.2f}%")
        lines.append("")

    return "\n".join(lines)


def render_full_report(results: List[Dict[str, Any]], errors: List[Dict[str, str]],
                       analysis_date: str, market_state: str) -> str:
    lines = []
    lines.append(f"# 龟缠量化v6_optimized 研报A股名单批量信号扫描（{analysis_date}收盘）")
    lines.append("")
    lines.append("> 西部证券8月5日AI算力上游材料研报建议关注名单（22只），使用龟缠v6_optimized完整信号逻辑重跑。")
    lines.append("> 数据来源：腾讯前复权日K/周K + 实时行情；不修改任何持仓/交易文件。")
    lines.append(f"> 市场状态：**{market_state}**")
    lines.append("")

    buy_count = len([r for r in results if r["verdict"] == "可考虑买入"])
    watch_count = len([r for r in results if r["verdict"] == "接近信号，可关注"])
    skip_count = len([r for r in results if r["verdict"] == "暂不考虑"])
    lines.append(f"**扫描结果：成功{len(results)}只，失败{len(errors)}只；"
                 f"🟢可考虑买入{buy_count}只，🟡接近信号{watch_count}只，⚪暂不考虑{skip_count}只。**")
    lines.append("")

    lines.append("## 总表")
    lines.append("")
    lines.append(render_table(results))
    lines.append("")

    lines.append(render_commentary(results))

    if errors:
        lines.append("## ❌ 数据获取失败")
        lines.append("")
        for e in errors:
            lines.append(f"- {e['code']} {e['name']}: {e['error']}")
        lines.append("")

    lines.append("---")
    lines.append("### 口径说明")
    lines.append("- 策略评分 = 突破(+1) + 有效放量(+1) + 缠论买点(+2) + 趋势过滤(+0.5) + 资金过滤(+0.5) + 低风险值(+0.5) + 堆量比达标(+1/-0.5) + 周线趋势(+0.5/0/-1) + 早鸟突破(+1.5)")
    lines.append("- 筛选标准：Buy=True → 可考虑买入；Buy=False但评分≥2.0且周线趋势≥0 → 接近信号；其余 → 暂不考虑")
    lines.append("- ATR% = ATR14/收盘价×100；量比 = 5日均量/20日均量")
    lines.append("- 5日/20日涨跌幅基于前复权收盘价计算")
    lines.append("- 以上为策略信号和技术分析，不构成投资建议。")
    return "\n".join(lines)


async def main():
    result_mode = sys.argv[1] if len(sys.argv) > 1 else "display_only"
    analysis_date = sys.argv[2] if len(sys.argv) > 2 else "2026-08-14"
    stock_list_path = sys.argv[3] if len(sys.argv) > 3 else ""

    from codeact_sdk import CodeActSDK
    sdk = CodeActSDK()
    actual_mode = result_mode if result_mode != "auto" else "display_only"

    try:
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        stocks = load_stock_list(stock_list_path or None)
        print(f"[参数] result_mode={result_mode}, analysis_date={analysis_date}, stocks={len(stocks)}只")

        # 加载策略和配置
        mod = sig_mod.import_strategy()
        cfg = mod.DEFAULT_CONFIG.copy()
        cfg["strategy"] = dict(cfg["strategy"])
        cfg["plot_enable"] = False

        market_state = sig_mod.load_state_on(analysis_date)
        state_name = market_state.get("state", "sideways")
        opt_params = sig_mod.load_optimal_params(state_name)
        print(f"[市场] state={state_name}, opt_params loaded={bool(opt_params)}")

        # 行情和指数
        codes_for_quote = [s["code"] for s in stocks]
        # 对 sz688 同时尝试 sh688
        extra_quotes = []
        for c in codes_for_quote:
            if c.startswith("sz688"):
                extra_quotes.append("sh" + c[2:])
        all_quote_codes = codes_for_quote + extra_quotes
        quotes = sig_mod.fetch_quotes(all_quote_codes)

        index_df = sig_mod.fetch_kline("sh000001", "day", 640)
        index_df = index_df[index_df["date"] <= pd.to_datetime(analysis_date)].copy()

        results: List[Dict[str, Any]] = []
        errors: List[Dict[str, str]] = []

        # 并发分析（ThreadPoolExecutor，因为策略计算是CPU密集+IO混合）
        max_workers = 4  # 控制并发避免内存和超时
        print(f"[并发] 使用 {max_workers} 个线程分析 {len(stocks)} 只股票...")

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            future_map = {}
            for stock in stocks:
                fut = executor.submit(
                    analyze_one_stock, stock, analysis_date,
                    mod, cfg, opt_params, market_state, index_df, quotes
                )
                future_map[fut] = stock

            for fut in as_completed(future_map):
                stock = future_map[fut]
                try:
                    r = fut.result()
                    results.append(r)
                    print(f"[完成] {r['code']} {r['name']} score={r['score']} buy={r['buy_signal']} verdict={r['verdict']}")
                except Exception as e:
                    errors.append({"code": stock["code"], "name": stock["name"], "error": str(e)})
                    print(f"[失败] {stock['code']} {stock['name']}: {e}")

        if not results:
            raise RuntimeError("全部股票分析失败：" + "; ".join(
                [f"{e['code']}:{e['error']}" for e in errors]
            ))

        # 按板块排序
        results.sort(key=lambda x: (x["sector"], x["code"]))

        # 生成报告
        report = render_full_report(results, errors, analysis_date, state_name)
        ts_tag = datetime.now().strftime("%Y%m%d_%H%M%S")
        report_path = os.path.join(OUTPUT_DIR, f"guichan_research_scan_{ts_tag}.md")
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(report)

        # 保存JSON结果
        json_path = os.path.join(OUTPUT_DIR, f"guichan_research_scan_{ts_tag}.json")
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "analysis_date": analysis_date,
                "market_state": state_name,
                "results": results,
                "errors": errors,
            }, f, ensure_ascii=False, indent=2, default=str)

        # 构建message
        buy_list = [r for r in results if r["verdict"] == "可考虑买入"]
        watch_list = [r for r in results if r["verdict"] == "接近信号，可关注"]
        skip_list = [r for r in results if r["verdict"] == "暂不考虑"]

        msg_lines = [
            f"龟缠v6研报名单扫描完成（{analysis_date}收盘）",
            f"市场状态：{state_name}；成功{len(results)}/{len(stocks)}只",
            f"🟢可考虑买入 {len(buy_list)}只，🟡接近信号 {len(watch_list)}只，⚪暂不考虑 {len(skip_list)}只",
            "",
        ]

        if buy_list:
            msg_lines.append("🟢 可考虑买入：")
            for r in buy_list:
                msg_lines.append(
                    f"  {r['name']}({r['code']}) 收盘{r['close']:.2f} 评分{r['score']:.2f} "
                    f"信号:{r['signal_type']} 5日{r['change_5d']:+.2f}%"
                )
        else:
            msg_lines.append("🟢 可考虑买入：无（本次无Buy信号触发）")

        if watch_list:
            msg_lines.append("🟡 接近信号：")
            for r in watch_list:
                msg_lines.append(
                    f"  {r['name']}({r['code']}) 收盘{r['close']:.2f} 评分{r['score']:.2f} "
                    f"周线{weekly_trend_label(r['weekly_trend'])} 5日{r['change_5d']:+.2f}%"
                )

        if not buy_list and not watch_list:
            top3 = sorted(results, key=lambda x: -x["score"])[:3]
            msg_lines.append("📌 相对最接近：")
            for r in top3:
                msg_lines.append(
                    f"  {r['name']}({r['code']}) 评分{r['score']:.2f} 周线{weekly_trend_label(r['weekly_trend'])} "
                    f"量比{r['vol_ratio']:.2f} 距上轨{((r['dc_high']/r['close'])-1)*100:.1f}%"
                )

        if errors:
            msg_lines.append(f"\n❌ 失败{len(errors)}只：" + "、".join([f"{e['name']}" for e in errors]))

        msg_lines.append(f"\n完整报告：[guichan_research_scan_{ts_tag}.md](computer://{os.path.abspath(report_path)})")

        message = "\n".join(msg_lines)

        await sdk.submit_result(
            result_mode=actual_mode,
            status="success",
            message=message,
            data={
                "report_path": report_path,
                "json_path": json_path,
                "analysis_date": analysis_date,
                "market_state": state_name,
                "total": len(stocks),
                "success_count": len(results),
                "failed_count": len(errors),
                "buy_count": len(buy_list),
                "watch_count": len(watch_list),
                "skip_count": len(skip_list),
                "buy_list": [{"code": r["code"], "name": r["name"], "score": r["score"]} for r in buy_list],
                "watch_list": [{"code": r["code"], "name": r["name"], "score": r["score"]} for r in watch_list],
                "errors": errors,
            },
        )
    except Exception as e:
        import traceback
        traceback.print_exc()
        await sdk.submit_result(
            result_mode="notify",
            status="error",
            message=f"研报名单批量扫描失败：{e}",
            data={"analysis_date": analysis_date},
        )
        raise


if __name__ == "__main__":
    asyncio.run(main())