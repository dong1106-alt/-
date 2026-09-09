#!/usr/bin/env python3
"""
每日信号扫描脚本 - 沪深300成分股 (v6_optimized)
基于龟缠量化v6_optimized信号逻辑，每日收盘后扫描买卖信号。

v6_optimized核心升级（L1-L4四层自适应引擎 + 动态核心参数）：
1. 大盘过滤(L1)：上证指数偏离MA60超过-3%硬截断 + 宏观R²/MA60斜率渐变仓位上限
2. 【v6动态海龟通道】：计算15/20/25三组通道，按波动率vol_ratio离散选择
   - vol_ratio > 1.3 → 25日通道（高波动放宽容口）
   - vol_ratio < 0.7 → 15日通道（低波动收紧容口）
   - 其他 → 20日通道（默认）
3. 【v6动态退出线】：计算8/10/12三组退出线，按趋势强度trend_str离散选择
   - trend_str > 0.15 → 12日退出线（强趋势让利润跑）
   - trend_str < -0.05 → 8日退出线（弱趋势锁利润）
   - 其他 → 10日退出线（默认）
4. 缠论买卖信号：底背离(一买)、回踩不破低(二买)、中枢上沿(三买)、顶背离(卖出)
5. 【v6动态预评分门槛】：根据上证指数R²和偏离度动态调整门槛
   - idx_r2 > 0.5 and idx_dev > 0 → threshold = base - 5（牛市降门槛扩池子）
   - idx_r2 < 0.15 or idx_dev < -0.03 → threshold = base + 5（熊市升门槛精选）
   - 其他 → threshold = base（默认40）
   - 抄底先锋软评分(X_7主力吸货强度+dip_count突破计数)
   - 估值极端低豁免(价格历史分位<10%额外+15分)
   - 筹码集中度辅助加分
6. L2行业过滤：120日R² < 0.15时买入仓位减半（滚动计算，无未来函数）
7. L3跨股波动率百分位：两阶段扫描，根据ATR%百分位调整止损倍数
8. L4 R²止盈：20日R²>0.15止盈15%，0.05-0.15止盈25%，<0.05止盈40%
9. 多空趋势/资金面/试盘回踩等软评分因子
10. min_entry_score = 1.7
11. ATR止损：基于波动率自适应的止损位计算
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===
import tempfile

import asyncio
import json
import sys
import os
import time
import numpy as np
import pandas as pd
import requests
from typing import List, Tuple, Dict, Optional
from codeact_sdk import CodeActSDK

# ===================== 常量配置 =====================
HS300_FILE = f"{_ROOT}/codeact/scripts/hs300_codes.txt"
MAIN_BOARD_FILE = f"{_ROOT}/data/all_main_board_codes.txt"
INDEX_CODE = "sh000001"
SZ_INDEX_CODE = "sz399001"
OUTPUT_DIR = os.environ.get("SIGNAL_OUTPUT_DIR", "./codeact/output")
KLINE_COUNT = 150  # 150条K线，覆盖所有指标需求（vol_ratio会NaN但代码已有fallback）
MAX_CONCURRENCY = 30  # proxy在50并发持续压测下会限流(P50从0.15s→2.2s)，降到30保持低延迟
# 端点顺序：proxy.finance.qq.com 当前最稳定（2026-08），放第一位优先使用；
# web.ifzq.gtimg.cn 主端点已被WAF拦截(501/403)，作为最后兜底，避免每只股票都先在死端点上重试超时。
KLINE_API = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
KLINE_API_FALLBACKS = [
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
]
# 网络抖动重试次数（仅对超时/连接错误重试；4xx/5xx/WAF等确定性错误不重试）
MAX_RETRIES = 1

# 本地数据目录（优先读取，大幅减少网络请求）
LOCAL_STOCK_DIR = f"{_ROOT}/data/stocks"
LOCAL_INDEX_DIR = f"{_ROOT}/data/index"

# CLI 数据通道（bash / CodeAct 均可用，作为腾讯 API 的兜底）
CLI_WRAPPER = f"{_ROOT}/.skills/skill_stock-data-skill/bin/stock-cli"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _check_cli_available() -> bool:
    """启动时探测 stock-cli 是否可用（API Key 是否配置）。
    不可用则后续所有请求直接跳过 CLI，避免每只股票等待 20s 超时。"""
    try:
        import subprocess
        result = subprocess.run(
            [CLI_WRAPPER, "call", "kline",
             "--param", "code=sh600000",
             "--param", "period=day",
             "--param", "count=5",
             "--param", "fq=qfq"],
            capture_output=True, text=True, timeout=8,
        )
        return result.returncode == 0
    except Exception:
        return False


_CLI_AVAILABLE = _check_cli_available()


def _fetch_kline_via_cli(code: str, count: int) -> pd.DataFrame | None:
    """通过 stock-cli 直接获取日 K 线（前复权），失败返回 None。
    启动时已探测 CLI 可用性，不可用直接返回 None，避免超时等待。"""
    if not _CLI_AVAILABLE:
        return None
    try:
        import subprocess
        result = subprocess.run(
            [CLI_WRAPPER, "call", "kline",
             "--param", f"code={code}",
             "--param", "period=day",
             "--param", f"count={count}",
             "--param", "fq=qfq"],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode != 0:
            return None
        payload = json.loads(result.stdout)
        rows = payload.get("data") or []
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["date"] = pd.to_datetime(df["date"])
        for c in ["open", "close", "high", "low", "volume"]:
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
        return df if len(df) >= 30 else None
    except Exception:
        return None

# 优化参数文件
# 影子候选可通过环境变量读取独立参数文件；主盘始终使用正式参数文件。
OPTIMAL_PARAMS_FILE = os.environ.get(
    "OPTIMAL_PARAMS_FILE", f"{_ROOT}/data/optimal_params.json"
)

# 策略参数（v6_optimized基准值，运行时会被自适应参数覆盖）
STRATEGY = {
    "dc_period": 20,
    "exit_period": 10,
    "atr_period": 20,
    "vol_window": 60,
    "base_risk_pct": 0.10,
    "min_atr_pct": 0.020,          # 【新增】波动率过滤：ATR占股价比<2.0%跳过买入
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
    # 【v6动态通道阈值】运行时被自适应参数覆盖
    "vol_ratio_high": 1.3,
    "vol_ratio_low": 0.7,
    "trend_str_high": 0.15,
    "trend_str_low": -0.05,
    # 软评分因子开关
    "use_trend_filter": True,
    "use_money_filter": True,
    "use_test_pullback": True,
    "test_pullback_threshold": 0.02,
    "test_pullback_vol_ratio": 0.7,
    "test_period": 30,
    "test_gain_threshold": 0.06,
    # 估值极端低豁免
    "extreme_value_exemption": True,
    "pe_historical_percentile_threshold": 0.10,
    "pb_historical_percentile_threshold": 0.10,
    "extreme_value_position_scale": 0.3,
    "extreme_value_max_hold_days": 20,
    # 【v6同步】堆量比评分市值分档阈值（大/中/小市值）
    "vol_stack_threshold_large": 3.0,
    "vol_stack_threshold_mid": 5.0,
    "vol_stack_threshold_small": 8.0,
}


# ===================== 自适应参数系统 =====================
def classify_market_state(macro_r2: float, macro_slope: float, trend_strength: float) -> str:
    """
    分类当前市场状态（与market_state.py classify_state逻辑一致）
    返回: bull/bear/sideways/transition
    """
    # 转折期：R²在0.15-0.5之间且斜率与偏离度方向不一致
    if 0.15 < macro_r2 < 0.5:
        if (macro_slope > 0 and trend_strength < -0.01) or (macro_slope < 0 and trend_strength > 0.01):
            return 'transition'
    # 牛市
    if macro_slope > 0.003 and macro_r2 > 0.5 and trend_strength > 0:
        return 'bull'
    # 熊市
    if macro_slope < -0.003 and macro_r2 > 0.3 and trend_strength < -0.02:
        return 'bear'
    # 震荡
    if macro_r2 < 0.15 or abs(macro_slope) < 0.002:
        return 'sideways'
    # 介于牛熊之间
    if macro_slope > 0 and trend_strength > -0.01:
        return 'bull' if macro_r2 > 0.3 else 'sideways'
    if macro_slope < 0 and trend_strength < 0.01:
        return 'bear' if macro_r2 > 0.2 else 'sideways'
    return 'sideways'

def load_adaptive_params(market_state: str) -> Optional[Dict]:
    """
    从optimal_params.json读取当前市场状态对应的优化参数
    返回优化参数dict或None（文件不存在/状态未采纳时）
    """
    if not os.path.exists(OPTIMAL_PARAMS_FILE):
        return None
    try:
        with open(OPTIMAL_PARAMS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        for r in data.get('results', []):
            if r['state'] == market_state and r.get('status') == 'adopted':
                return r['params']
        return None
    except Exception:
        return None

def apply_adaptive_params(market_state: str) -> Dict:
    """
    根据市场状态加载自适应参数并覆盖STRATEGY
    返回: {"applied": bool, "state": str, "params": dict, "source": str}
    """
    params = load_adaptive_params(market_state)
    if params is None:
        return {"applied": False, "state": market_state, "params": {}, "source": "v6默认参数"}
    
    # 覆盖STRATEGY中的对应参数
    STRATEGY["min_entry_score"] = params["min_entry_score"]
    STRATEGY["base_atr_multiplier"] = params["atr_multiplier"]
    STRATEGY["dc_period"] = params["channel_base"]
    STRATEGY["exit_period"] = params["exit_base"]
    STRATEGY["vol_ratio_high"] = params["vol_ratio_high"]
    STRATEGY["vol_ratio_low"] = params["vol_ratio_low"]
    STRATEGY["trend_str_high"] = params["trend_str_high"]
    STRATEGY["trend_str_low"] = params["trend_str_low"]
    # 【自适应】新增3个参数
    STRATEGY["trail_stop_pct"] = params.get("trail_stop_pct", 0.05)
    STRATEGY["risk_value_ban"] = params.get("risk_value_ban", 80)
    STRATEGY["score_threshold"] = params.get("score_threshold", 2.0)
    # 【扩展】7个新增自适应参数（18参数全适配）
    STRATEGY["base_risk_pct"] = params.get("base_risk_pct", 0.10)
    STRATEGY["max_concurrent_positions"] = params.get("max_concurrent_positions", 5)
    STRATEGY["take_profit_base"] = params.get("take_profit_base", 0.4)
    STRATEGY["stop_multiplier_base"] = params.get("stop_multiplier_base", 2.0)
    STRATEGY["add_threshold_base"] = params.get("add_threshold_base", 0.06)
    STRATEGY["pre_filter_threshold"] = params.get("pre_filter_threshold", 60)
    STRATEGY["vol_pos_factor"] = params.get("vol_pos_factor", 0.15)
    # 【v6同步】堆量比评分市值分档阈值（21参数全适配）
    STRATEGY["vol_stack_threshold_large"] = params.get("vol_stack_threshold_large", 3.0)
    STRATEGY["vol_stack_threshold_mid"] = params.get("vol_stack_threshold_mid", 5.0)
    STRATEGY["vol_stack_threshold_small"] = params.get("vol_stack_threshold_small", 8.0)

    return {
        "applied": True,
        "state": market_state,
        "params": {
            "min_entry_score": params["min_entry_score"],
            "atr_multiplier": params["atr_multiplier"],
            "channel_base": params["channel_base"],
            "exit_base": params["exit_base"],
            "vol_ratio_high": params["vol_ratio_high"],
            "vol_ratio_low": params["vol_ratio_low"],
            "trend_str_high": params["trend_str_high"],
            "trend_str_low": params["trend_str_low"],
            "trail_stop_pct": STRATEGY["trail_stop_pct"],
            "risk_value_ban": STRATEGY["risk_value_ban"],
            "score_threshold": STRATEGY["score_threshold"],
            "base_risk_pct": STRATEGY["base_risk_pct"],
            "max_concurrent_positions": STRATEGY["max_concurrent_positions"],
            "take_profit_base": STRATEGY["take_profit_base"],
            "stop_multiplier_base": STRATEGY["stop_multiplier_base"],
            "add_threshold_base": STRATEGY["add_threshold_base"],
            "pre_filter_threshold": STRATEGY["pre_filter_threshold"],
            "vol_pos_factor": STRATEGY["vol_pos_factor"],
            "vol_stack_threshold_large": STRATEGY["vol_stack_threshold_large"],
            "vol_stack_threshold_mid": STRATEGY["vol_stack_threshold_mid"],
            "vol_stack_threshold_small": STRATEGY["vol_stack_threshold_small"],
        },
        "source": f"自适应优化参数({market_state})"
    }


# ===================== 通用工具函数 =====================
def calc_r2(closes, lookback=60):
    """计算R²（线性回归拟合度）"""
    n = min(lookback, len(closes))
    if n < 3:
        return 0.0
    x = np.arange(n)
    y = np.array(closes[-n:], dtype=float)
    r = np.corrcoef(x, y)[0, 1]
    return float(r ** 2) if not np.isnan(r) else 0.0


def calc_valuation_percentile(df: pd.DataFrame, lookback: int = 250) -> Dict[str, float]:
    """
    计算当前价格在历史区间内的估值分位（用价格替代PE/PB）
    """
    if len(df) < lookback:
        return {'price_percentile': 0.5, 'is_extreme': False, 'min_price': 0, 'max_price': 0}

    recent = df.tail(lookback)
    current_price = df['close'].iloc[-1]
    min_price = recent['close'].min()
    max_price = recent['close'].max()

    if max_price == min_price:
        percentile = 0.5
    else:
        percentile = (current_price - min_price) / (max_price - min_price)

    return {
        'price_percentile': float(percentile),
        'is_extreme': bool(percentile < 0.10),
        'min_price': float(min_price),
        'max_price': float(max_price),
    }


def estimate_market_cap(price: float) -> float:
    """按股价粗估市值档位（移植自主策略estimate_market_cap），用于堆量比分档阈值"""
    if price < 5:
        return 30
    elif price < 10:
        return 80
    elif price < 20:
        return 150
    elif price < 50:
        return 300
    elif price < 100:
        return 500
    else:
        return 800


# ===================== 数据获取（腾讯行情API） =====================
def _parse_tencent_bars(code: str, raw: dict) -> pd.DataFrame:
    """解析腾讯API返回的K线数据（兼容7列主端点和10列备用端点）。
    备用端点 proxy.finance.qq.com 的每行格式：
      [date, open, close, high, low, volume, {dividend_dict}, change_pct, amount, suffix]
    我们只取前6个标量列，忽略字典/额外列。"""
    stock_data = raw.get("data", {}).get(code, {})
    bars = stock_data.get("qfqday") or stock_data.get("day")
    if not bars:
        # 空K线=停牌/退市（API返回code=0但day=[]），返回空DataFrame而非抛异常，
        # 让调用方区分"无数据"和"请求失败"，避免无谓地尝试其他端点
        return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])
    # 只提取前6个标量字段，跳过字典等非标量元素（分红信息等）
    rows = []
    for b in bars:
        scalar_vals = [v for v in b if not isinstance(v, dict)]
        if len(scalar_vals) < 6:
            continue
        rows.append(scalar_vals[:6])
    if not rows:
        return pd.DataFrame(columns=["date", "open", "close", "high", "low", "volume"])
    col_names = ["date", "open", "close", "high", "low", "volume"]
    df = pd.DataFrame(rows, columns=col_names)
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = pd.to_numeric(df[c])
    df = df.sort_values("date").reset_index(drop=True)
    return df


def _fetch_latest_bars_api(code: str, count: int = 5) -> pd.DataFrame:
    """从腾讯API只拉最新几根K线（用于补齐本地数据）。
    主端点被WAF拦截时自动切换到备用端点。"""
    endpoints = [KLINE_API] + KLINE_API_FALLBACKS
    last_err = None
    for ep in endpoints:
        url = f"{ep}?param={code},day,,,{count},qfq"
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = requests.get(url, timeout=6, headers=HTTP_HEADERS)
                if resp.status_code != 200:
                    # 4xx/5xx/WAF 是确定性失败，重试无意义，直接换下一个端点
                    last_err = RuntimeError(f"HTTP {resp.status_code}")
                    break
                if resp.text.lstrip().startswith("<"):
                    last_err = RuntimeError("WAF block (HTML response)")
                    break
                raw = resp.json()
                if raw.get("code") != 0:
                    last_err = RuntimeError(f"API error: {raw.get('msg', 'unknown')}")
                    break
                parsed = _parse_tencent_bars(code, raw)
                if parsed is not None and len(parsed) == 0:
                    # proxy返回code=0但day=[]→停牌/退市，其他端点也不会有数据，立即返回空DF
                    return parsed
                return parsed
            except (requests.Timeout, requests.ConnectionError) as e:
                # 仅网络抖动才重试
                last_err = e
                if attempt < MAX_RETRIES:
                    time.sleep(0.2 * (attempt + 1))
            except Exception as e:
                last_err = e
                break
    raise RuntimeError(f"fetch_latest_bars failed for {code}: {str(last_err)[:100]}")


# 启动时探测 parquet 引擎；缺失则自动从阿里云镜像安装（沙箱环境会被重置）
def _ensure_parquet_engine():
    try:
        pd.DataFrame({"a": [1]}).to_parquet(os.path.join(tempfile.gettempdir(), "_probe.parquet"))
        pd.read_parquet(os.path.join(tempfile.gettempdir(), "_probe.parquet"))
        return True
    except Exception:
        pass
    # 尝试自动安装 pyarrow（阿里云镜像最稳定）
    import subprocess, sys
    for mirror in ["https://mirrors.aliyun.com/pypi/simple/",
                   "https://pypi.tuna.tsinghua.edu.cn/simple/"]:
        try:
            print(f"[环境] pyarrow 缺失，尝试从镜像安装: {mirror}")
            r = subprocess.run(
                [sys.executable, "-m", "pip", "install", "pyarrow",
                 "-i", mirror, "--timeout", "120", "--quiet"],
                capture_output=True, text=True, timeout=240,
            )
            if r.returncode == 0:
                # 重新加载 pandas 引擎
                import importlib
                try:
                    import pyarrow  # noqa
                    pd.DataFrame({"a": [1]}).to_parquet(os.path.join(tempfile.gettempdir(), "_probe.parquet"))
                    pd.read_parquet(os.path.join(tempfile.gettempdir(), "_probe.parquet"))
                    print("[环境] pyarrow 自动安装成功")
                    return True
                except Exception:
                    pass
        except Exception:
            continue
    print("[环境] pyarrow 自动安装失败，切换为 CLI 数据通道")
    return False


_HAS_PARQUET = _ensure_parquet_engine()


def _load_local_kline(code: str) -> Optional[pd.DataFrame]:
    """读取本地parquet K线数据（pyarrow 不可用时直接返回 None，不抛异常）"""
    if not _HAS_PARQUET:
        return None
    if code == INDEX_CODE:
        path = f"{LOCAL_INDEX_DIR}/{code}.parquet"
    else:
        path = f"{LOCAL_STOCK_DIR}/{code}.parquet"
    if not os.path.exists(path):
        return None
    try:
        df = pd.read_parquet(path)
        # 确保列名一致
        if "date" not in df.columns:
            df = df.reset_index()
        df["date"] = pd.to_datetime(df["date"])
        for c in ["open", "close", "high", "low", "volume"]:
            if c in df.columns:
                df[c] = pd.to_numeric(df[c], errors="coerce")
        df = df.sort_values("date").reset_index(drop=True)
        return df
    except Exception:
        return None


def _fetch_kline_via_http(code: str, count: int) -> pd.DataFrame | None:
    """通过腾讯 HTTP API 拉取K线（多端点轮询，主端点被WAF时切备用）。
    成功返回 DataFrame，失败返回 None。"""
    for ep in [KLINE_API] + KLINE_API_FALLBACKS:
        url = f"{ep}?param={code},day,,,{count},qfq"
        try:
            resp = requests.get(url, timeout=6, headers=HTTP_HEADERS)
            if resp.status_code != 200 or resp.text.lstrip().startswith("<"):
                continue
            raw = resp.json()
            if raw.get("code") != 0:
                continue
            df = _parse_tencent_bars(code, raw)
            if df is not None and len(df) == 0:
                # proxy确认停牌/退市（空K线），其他端点也不会有，直接返回空DF
                return df
            if df is not None and len(df) >= 30:
                return df
        except Exception:
            continue
    return None


def _merge_local_and_api(local_df: pd.DataFrame, api_df: pd.DataFrame,
                         count: int) -> pd.DataFrame:
    """将本地 parquet 历史与 API 近期数据拼接去重，截断为最近 count 条。"""
    common_cols = [c for c in ["date", "open", "close", "high", "low", "volume"]
                   if c in local_df.columns and c in api_df.columns]
    cutoff = api_df["date"].min()
    hist = local_df[local_df["date"] < cutoff]
    combined = pd.concat(
        [hist[common_cols], api_df[common_cols]], ignore_index=True
    )
    combined = combined.drop_duplicates(subset=["date"], keep="last")
    combined = combined.sort_values("date").reset_index(drop=True)
    if len(combined) > count:
        combined = combined.tail(count).reset_index(drop=True)
    return combined


def _save_kline_to_parquet(code: str, df: pd.DataFrame) -> None:
    """把成功获取的K线写回本地parquet，保证下次扫描数据是最新的、减少对网络的依赖。
    关键：与现有parquet合并（保留长历史），而非整体覆盖。
    （2026-09-01修复：原来整体覆盖会把指数/股票parquet截断成150根，导致月度重建
    市场状态时间线时只剩91天历史，回测窗口<60根K线→永远0交易。）
    失败静默（不影响主流程）。"""
    if not _HAS_PARQUET or df is None or len(df) < 30:
        return
    try:
        path = f"{LOCAL_INDEX_DIR if code == INDEX_CODE or code == SZ_INDEX_CODE else LOCAL_STOCK_DIR}/{code}.parquet"
        out = df[["date", "open", "close", "high", "low", "volume"]].copy()
        out["date"] = pd.to_datetime(out["date"])
        # 与已有历史合并：新数据覆盖同日旧数据，历史保留（cap 2000根≈8年防止无限增长）
        if os.path.exists(path):
            try:
                old = pd.read_parquet(path)
                if "date" in old.columns and len(old) > 0:
                    old["date"] = pd.to_datetime(old["date"])
                    merged = pd.concat([old, out], ignore_index=True)
                    merged = merged.drop_duplicates(subset=["date"], keep="last")
                    merged = merged.sort_values("date").reset_index(drop=True)
                    if len(merged) > 2000:
                        merged = merged.tail(2000).reset_index(drop=True)
                    out = merged
            except Exception:
                pass
        out.to_parquet(path, index=False)
    except Exception:
        pass


def fetch_kline(code: str, count: int = KLINE_COUNT, persist: bool = True) -> pd.DataFrame:
    """
    混合数据获取（优先级）：
    1. 本地 parquet（pyarrow 可用时）：历史数据毫秒级读取
    2. stock-cli（API Key 可用时）：拉取近期数据与本地拼接
    3. 腾讯 HTTP API（多端点轮询，含 proxy.finance.qq.com 备用）：始终可用的兜底
    persist=True 时把最新数据写回 parquet，保证下次扫描数据是最新的，网络全挂时也不影响止损判断。
    返回数据截断为最近 count 条。
    """
    local_df = _load_local_kline(code) if _HAS_PARQUET else None

    # ---- 路径 A：本地有充足 parquet 数据 ----
    if local_df is not None and len(local_df) >= 120:
        local_last_date = pd.Timestamp(local_df["date"].iloc[-1]).normalize()
        days_stale = (pd.Timestamp.now().normalize() - local_last_date).days

        # A0. 本地数据 >30 天：大概率已退市/长期停牌，腾讯API也返回空day，
        #     不再浪费CLI+3端点HTTP请求，直接返回本地数据（后续7天新鲜度检查会跳过）
        if days_stale > 30:
            if len(local_df) > count:
                return local_df.tail(count).reset_index(drop=True)
            return local_df

        # A1. 本地数据 ≤5 天：只拉几根补齐（毫秒级）
        if days_stale <= 5:
            new_bars_count = 0
            try:
                latest_df = _fetch_latest_bars_api(code, count=5)
                common_cols = [c for c in ["date", "open", "close", "high", "low", "volume"]
                               if c in local_df.columns and c in latest_df.columns]
                for _, api_row in latest_df.iterrows():
                    api_date = api_row["date"]
                    mask = local_df["date"] == api_date
                    if mask.any():
                        for col in ["open", "close", "high", "low", "volume"]:
                            if col in local_df.columns and col in latest_df.columns:
                                local_df.loc[mask, col] = api_row[col]
                new_bars = latest_df[latest_df["date"] > local_last_date]
                new_bars_count = len(new_bars)
                if new_bars_count > 0:
                    combined = pd.concat([local_df[common_cols], new_bars[common_cols]], ignore_index=True)
                    combined = combined.drop_duplicates(subset=["date"], keep="last")
                    combined = combined.sort_values("date").reset_index(drop=True)
                else:
                    combined = local_df
                if len(combined) > count:
                    combined = combined.tail(count).reset_index(drop=True)
                if persist and new_bars_count > 0:
                    _save_kline_to_parquet(code, combined)
                return combined
            except Exception:
                if len(local_df) > count:
                    return local_df.tail(count).reset_index(drop=True)
                return local_df

        # A2. 本地数据 >5 天：优先 CLI 拉近期数据拼接；CLI 不可用则 HTTP API
        fetch_count = max(60, days_stale + 30)
        cli_recent = _fetch_kline_via_cli(code, fetch_count)
        if cli_recent is not None and len(cli_recent) >= 30:
            result = _merge_local_and_api(local_df, cli_recent, count)
            if persist:
                _save_kline_to_parquet(code, result)
            return result

        http_recent = _fetch_kline_via_http(code, fetch_count)
        if http_recent is not None and len(http_recent) >= 30:
            result = _merge_local_and_api(local_df, http_recent, count)
            if persist:
                _save_kline_to_parquet(code, result)
            return result

        # 所有在线通道均失败 → 返回陈旧本地数据（总比报错好）
        if len(local_df) > count:
            return local_df.tail(count).reset_index(drop=True)
        return local_df

    # ---- 路径 B：本地无 parquet 或数据不足 ----
    cli_df = _fetch_kline_via_cli(code, count)
    if cli_df is not None:
        if len(cli_df) > count:
            cli_df = cli_df.tail(count).reset_index(drop=True)
        if persist:
            _save_kline_to_parquet(code, cli_df)
        return cli_df

    http_df = _fetch_kline_via_http(code, count)
    if http_df is not None and len(http_df) >= 30:
        if persist:
            _save_kline_to_parquet(code, http_df)
        return http_df

    # 无本地数据+在线通道也无有效数据（停牌/退市/网络全挂）
    if http_df is not None and len(http_df) == 0:
        # proxy确认无K线数据，返回空DF让调用方跳过（不抛异常）
        return http_df

    raise RuntimeError(f"fetch_kline failed for {code} (all channels)")


async def fetch_kline_async(code: str, sem: asyncio.Semaphore) -> pd.DataFrame:
    """异步获取K线数据"""
    async with sem:
        return await asyncio.to_thread(fetch_kline, code)


# ===================== 指标计算（v6_optimized calc_indicators_vec） =====================
def calc_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """计算全部技术指标（移植自v6_optimized calc_indicators_vec，含动态通道/退出线）"""
    st = STRATEGY
    dc_p = st["dc_period"]
    exit_p = st["exit_period"]
    atr_p = st["atr_period"]
    vol_win = st["vol_window"]

    df = df.copy()

    # 【v6动态海龟通道】计算三组通道，按波动率vol_ratio离散选择
    # 通道周期：base-5 / base / base+5（base从自适应参数获取）
    atr_pct = (df["close"].diff().abs() / df["close"].shift(1)).rolling(60).mean()
    atr_pct_ma = atr_pct.rolling(120).mean()
    vol_ratio = atr_pct / atr_pct_ma

    ch_base = st["dc_period"]
    ch_low = max(5, ch_base - 5)   # 低波动通道
    ch_high = ch_base + 5           # 高波动通道
    
    df[f"dc_high_{ch_low}"] = df["high"].rolling(ch_low).max().shift(1)
    df[f"dc_high_{ch_base}"] = df["high"].rolling(ch_base).max().shift(1)
    df[f"dc_high_{ch_high}"] = df["high"].rolling(ch_high).max().shift(1)
    df[f"dc_low_{ch_low}"] = df["low"].rolling(ch_low).min().shift(1)
    df[f"dc_low_{ch_base}"] = df["low"].rolling(ch_base).min().shift(1)
    df[f"dc_low_{ch_high}"] = df["low"].rolling(ch_high).min().shift(1)

    # 按波动率状态动态选择通道（阈值从自适应参数获取）
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

    # 【v6动态退出线】计算三组退出线，按趋势强度trend_str离散选择
    # 退出周期：base-2 / base / base+2（base从自适应参数获取）
    ex_base = st["exit_period"]
    ex_low = max(3, ex_base - 2)   # 弱趋势退出线
    ex_high = ex_base + 2           # 强趋势退出线
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

    # MA20斜率（多空趋势因子）
    df["ma20_slope"] = (df["ma20"] - df["ma20"].shift(5)) / df["ma20"].shift(5)

    # 资金面因子
    df["money_strength"] = df["macd_hist"]
    df["money_slope"] = df["money_strength"] - df["money_strength"].shift(3)

    # 试盘回踩因子
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

    # 【v6同步·新增】风险值（超买超卖振荡器，移植自主策略calc_indicators_vec）
    # 风险值 = EMA(100 × (C - LLV(L,34)) / (HHV(H,34) - LLV(L,34)), 3)
    # 0=底部超卖, 100=顶部超买
    llv_34 = df["low"].rolling(34).min()
    hhv_34 = df["high"].rolling(34).max()
    range_34 = hhv_34 - llv_34
    range_34 = range_34.replace(0, 1)  # 防止除零
    raw_risk = 100 * (df["close"] - llv_34) / range_34
    raw_risk = raw_risk.clip(0, 100)
    df["risk_value"] = raw_risk.ewm(span=3, adjust=False).mean()

    # 【v6同步·新增】堆量比（衡量中期量能扩张程度）
    # 堆量比 = MAX(MA(V,10)/LLV(MA(V,20),120), MA(V,15)/LLV(MA(V,40),300))
    ma_vol_10 = df["volume"].rolling(10).mean()
    ma_vol_20 = df["volume"].rolling(20).mean()
    ma_vol_15 = df["volume"].rolling(15).mean()
    ma_vol_40 = df["volume"].rolling(40).mean()
    llv_mavol20_120 = ma_vol_20.rolling(120).min()
    llv_mavol40_300 = ma_vol_40.rolling(300).min()
    llv_mavol20_120 = llv_mavol20_120.replace(0, 1)  # 防止除零
    llv_mavol40_300 = llv_mavol40_300.replace(0, 1)  # 防止除零
    vol_ratio_1 = ma_vol_10 / llv_mavol20_120
    vol_ratio_2 = ma_vol_15 / llv_mavol40_300
    df["vol_stack_ratio"] = np.maximum(vol_ratio_1.fillna(0), vol_ratio_2.fillna(0))

    # 【v6同步·新增】周线战略过滤（三周期共振）
    # 取每5根日K的收盘价均值作为周线收盘，20周≈100日
    weekly_close = df["close"].rolling(5).mean()
    weekly_ma20 = weekly_close.rolling(100).mean()
    df["weekly_trend"] = 0  # 默认走平
    df.loc[weekly_close > weekly_ma20 * 1.005, "weekly_trend"] = 1   # 周线上升
    df.loc[weekly_close < weekly_ma20 * 0.995, "weekly_trend"] = -1  # 周线下降

    # 【v6同步·新增】早鸟突破：10日唐奇安上轨（主升浪初期放量突破捕捉）
    df["dc_high_aggressive"] = df["high"].rolling(10).max().shift(1)

    return df


# ===================== 缠论核心（移植自v6_optimized） =====================
def merge_kline_include(df: pd.DataFrame) -> pd.DataFrame:
    """合并K线包含关系（优化版：栈式O(n)算法，替代原DataFrame.drop O(n²)）"""
    highs = df["high"].values.tolist()
    lows = df["low"].values.tolist()
    if len(highs) < 2:
        return pd.DataFrame({"high": highs, "low": lows})

    stack_h = [highs[0]]
    stack_l = [lows[0]]

    for i in range(1, len(highs)):
        h1, l1 = highs[i], lows[i]
        merged = False
        while stack_h:
            h0, l0 = stack_h[-1], stack_l[-1]
            if h0 >= h1 and l0 <= l1:
                # 栈顶包含当前K线：跳过当前（保留栈顶范围）
                merged = True
                break
            elif h1 >= h0 and l1 <= l0:
                # 当前包含栈顶：弹出栈顶，继续检查
                stack_h.pop()
                stack_l.pop()
            else:
                # 无包含关系：压入当前
                break
        if not merged:
            stack_h.append(h1)
            stack_l.append(l1)

    return pd.DataFrame({"high": stack_h, "low": stack_l})


def find_pivots_enhanced(df: pd.DataFrame) -> Tuple[List[int], List[int]]:
    """识别顶分型和底分型"""
    bars = merge_kline_include(df)
    tops, bottoms = [], []
    for i in range(1, len(bars) - 1):
        h, l = bars.loc[i, "high"], bars.loc[i, "low"]
        h1, h2 = bars.loc[i - 1, "high"], bars.loc[i + 1, "high"]
        l1, l2 = bars.loc[i - 1, "low"], bars.loc[i + 1, "low"]
        if h > h1 and h > h2:
            tops.append(i)
        if l < l1 and l < l2:
            bottoms.append(i)
    return tops, bottoms


def identify_strokes_std(tops, bottoms, bars):
    """标准笔识别"""
    if not tops or not bottoms:
        return []
    pivots = [(i, "top") for i in tops] + [(i, "bottom") for i in bottoms]
    pivots.sort(key=lambda x: x[0])
    strokes, last = [], pivots[0]
    for p in pivots[1:]:
        idx, t = p
        lidx, lt = last
        if t == lt:
            if t == "top":
                if bars.loc[idx, "high"] > bars.loc[lidx, "high"]:
                    last = p
            else:
                if bars.loc[idx, "low"] < bars.loc[lidx, "low"]:
                    last = p
            continue
        if abs(idx - lidx) < 5:
            continue
        if lt == "bottom" and t == "top" and bars.loc[idx, "high"] > bars.loc[lidx, "low"]:
            strokes.append((lidx, idx, "up"))
        elif lt == "top" and t == "bottom" and bars.loc[idx, "low"] < bars.loc[lidx, "high"]:
            strokes.append((lidx, idx, "down"))
        last = p
    return strokes


def calc_macd_area_section(df, pivot_idx, w, is_down):
    """计算MACD面积"""
    n = len(df)
    s, e = max(0, pivot_idx - w), min(n, pivot_idx + w + 1)
    seg = df.iloc[s:e]["macd_hist"]
    return abs(seg[seg < 0].sum()) if is_down else abs(seg[seg > 0].sum())


def detect_chan_signals(df: pd.DataFrame, pivot_win: int = 5, chan_threshold: float = 0.70) -> pd.DataFrame:
    """缠论买卖信号检测（移植自v6_optimized detect_chan_signals_optimized）"""
    n = len(df)
    df = df.copy()
    tops, bottoms = find_pivots_enhanced(df)
    df["chan_buy"] = False
    df["chan_buy_type"] = ""
    df["chan_sell"] = False

    # 一买：底背离
    for k in range(1, len(bottoms)):
        i1, i2 = bottoms[k - 1], bottoms[k]
        if df.loc[i2, "low"] >= df.loc[i1, "low"] * 0.97:
            continue
        a1 = calc_macd_area_section(df, i1, pivot_win + 2, True)
        a2 = calc_macd_area_section(df, i2, pivot_win + 2, True)
        if a2 < a1 * chan_threshold:
            cfm = min(i2 + pivot_win, n - 1)
            df.loc[cfm, "chan_buy"] = True
            df.loc[cfm, "chan_buy_type"] = "一买(底背离)"

    # 二买：回踩不破低
    buy_points = df[df["chan_buy"] == True].index.tolist()
    for bid in buy_points:
        blow = df.loc[bid, "low"]
        for b in bottoms:
            if b <= bid or b > bid + 60:
                break
            if df.loc[b, "low"] > blow * 1.01:
                df.loc[b, "chan_buy"] = True
                df.loc[b, "chan_buy_type"] = "二买(回踩不破低)"
                break

    # 三买：中枢上沿
    bars = df[["high", "low"]].reset_index(drop=True)
    strokes = identify_strokes_std(tops, bottoms, bars)
    if len(strokes) >= 3:
        for sidx in range(len(strokes) - 2):
            s1, s2, s3 = strokes[sidx:sidx + 3]
            ranges = []
            for si, ei, _ in [s1, s2, s3]:
                seg = bars.iloc[si:ei + 1]
                ranges.append((seg["low"].min(), seg["high"].max()))
            hub_low = max(r[0] for r in ranges)
            hub_high = min(r[1] for r in ranges)
            if hub_low >= hub_high:
                continue
            hub_end = s3[1]
            for b in bottoms:
                if b <= hub_end + 5 or b > hub_end + 40:
                    continue
                peak_after = df.loc[hub_end:b, "high"].max()
                if peak_after > hub_high * 1.02 and df.loc[b, "low"] > hub_high * 1.01:
                    df.loc[b, "chan_buy"] = True
                    df.loc[b, "chan_buy_type"] = "三买(中枢上沿)"

    # 顶背离卖出
    for k in range(1, len(tops)):
        i1, i2 = tops[k - 1], tops[k]
        if df.loc[i2, "high"] <= df.loc[i1, "high"]:
            continue
        a1 = calc_macd_area_section(df, i1, pivot_win + 2, False)
        a2 = calc_macd_area_section(df, i2, pivot_win + 2, False)
        if a2 < a1 * 0.8:
            cfm = min(i2 + pivot_win, n - 1)
            df.loc[cfm, "chan_sell"] = True

    return df


# ===================== 预评分（v6_optimized calc_stock_quality_score） =====================
def calc_stock_quality_score(df: pd.DataFrame) -> Tuple[float, Dict]:
    """
    计算整只股票的综合趋势质量评分（移植自v6_optimized）
    包含：基础趋势评分 + R² + 突破模拟 + 抄底先锋(X_7) + 估值极端低豁免 + 筹码集中度

    返回: (score, detail_dict)
    """
    st = STRATEGY
    if len(df) < 30:
        return 0.0, {}

    recent = df.copy()
    recent["trend_deviation"] = recent["close"] / recent["ma60"] - 1
    recent["trend_stability"] = 1 - recent["trend_deviation"].rolling(20).std() * 10
    recent["trend_stability"] = recent["trend_stability"].clip(0, 1)
    recent["trend_strength_score"] = (recent["close"] / recent["ma60"] - 1).clip(-1, 1) * 0.5 + 0.5
    recent["above_ma20_ratio"] = (recent["close"] > recent["ma20"]).rolling(20).mean()

    base_score = (
        recent["trend_stability"].mean() * 0.30 +
        recent["trend_strength_score"].mean() * 0.30 +
        recent["above_ma20_ratio"].mean() * 0.20
    ) * 100

    # R²
    closes = recent["close"].values
    n_pts = len(closes)
    x = np.arange(n_pts)
    x_mean = x.mean()
    y_mean = closes.mean()
    ss_xy = np.sum((x - x_mean) * (closes - y_mean))
    ss_xx = np.sum((x - x_mean) ** 2)
    ss_yy = np.sum((closes - y_mean) ** 2)
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_xx > 0 and ss_yy > 0 else 0

    # 突破模拟
    sim_trades = []
    in_position = False
    entry_price = 0
    entry_idx = 0
    exit_low_col = "exit_low" if "exit_low" in recent.columns else None

    for i in range(20, len(recent)):
        if not in_position:
            if "dc_high" in recent.columns and not pd.isna(recent["dc_high"].iloc[i]):
                if recent["close"].iloc[i] > recent["dc_high"].iloc[i]:
                    in_position = True
                    entry_price = recent["close"].iloc[i]
                    entry_idx = i
        else:
            should_exit = False
            if exit_low_col and not pd.isna(recent[exit_low_col].iloc[i]):
                if recent["close"].iloc[i] < recent[exit_low_col].iloc[i]:
                    should_exit = True
            if i - entry_idx >= 30:
                should_exit = True
            if should_exit:
                exit_price = recent["close"].iloc[i]
                pnl_pct = (exit_price / entry_price - 1) * 100
                sim_trades.append(pnl_pct)
                in_position = False

    if len(sim_trades) > 0:
        sim_win_rate = sum(1 for p in sim_trades if p > 0) / len(sim_trades)
        sim_avg_pnl = np.mean(sim_trades)
    else:
        sim_win_rate = 0.5
        sim_avg_pnl = 0

    r2_score = r_squared * 100
    sim_score = sim_win_rate * 70 + min(max(sim_avg_pnl * 3, 0), 30)
    score = base_score * 0.5 + r2_score * 0.20 + sim_score * 0.30

    if r_squared < 0.3:
        score = min(score, 35.0)
    if len(sim_trades) >= 3 and sim_win_rate <= 0.3:
        score = min(score, 35.0)
    if len(sim_trades) >= 3 and sim_avg_pnl < -1:
        score = min(score, 35.0)

    # ========== 抄底先锋（X_7主力吸货强度）==========
    x1 = recent["low"].shift(1)
    abs_diff = (recent["low"] - x1).abs()
    pos_diff = (recent["low"] - x1).clip(lower=0)
    sma_abs = abs_diff.ewm(alpha=1/3, adjust=False).mean()
    sma_pos = pos_diff.ewm(alpha=1/3, adjust=False).mean()
    x2 = pd.Series(np.where(sma_pos > 1e-10, sma_abs / sma_pos * 100, 9999.0), index=recent.index)
    x3 = (x2 * 10).ewm(alpha=2/4, adjust=False).mean()
    x4 = recent["low"].rolling(38).min()
    x5 = x3.rolling(38).max()
    accum_raw = pd.Series(0.0, index=recent.index)
    new_low_mask = recent["low"] <= x4
    accum_raw[new_low_mask] = (x3[new_low_mask] + x5[new_low_mask] * 2) / 2
    x7 = accum_raw.ewm(alpha=2/4, adjust=False).mean() / 618

    dip_signal = x7 >= 1
    dip_onset = dip_signal & ~dip_signal.shift(1, fill_value=False)

    dip_count = 0
    for i in range(len(recent)):
        if dip_onset.iloc[i]:
            future_after = recent.iloc[i + 1:min(i + 21, len(recent))]
            if len(future_after) > 0 and (future_after["close"] > future_after["dc_high"]).any():
                dip_count += 1

    dip_score = min(dip_count * 3.0, 15.0)
    score += dip_score

    # ========== 估值极端低豁免加分 ==========
    valuation = calc_valuation_percentile(recent)
    exemption_bonus = 0.0
    if st.get("extreme_value_exemption", True) and valuation["is_extreme"]:
        exemption_bonus = 15.0
        score += exemption_bonus

    # ========== 筹码集中度辅助 ==========
    chip_bonus = 0.0
    if "ma5" not in recent.columns:
        recent["ma5"] = recent["close"].rolling(5).mean()
    deviation = abs(recent["close"] / recent["ma60"] - 1).mean()
    if deviation < 0.05 and recent["ma5"].iloc[-1] > recent["ma20"].iloc[-1] > recent["ma60"].iloc[-1]:
        chip_bonus = 5.0
        score += chip_bonus

    detail = {
        "base_score": round(float(base_score), 1),
        "r_squared": round(float(r_squared), 3),
        "sim_trades": len(sim_trades),
        "sim_win_rate": round(float(sim_win_rate), 2),
        "dip_count": int(dip_count),
        "dip_score": round(float(dip_score), 1),
        "valuation_percentile": round(float(valuation["price_percentile"]), 3),
        "is_extreme_value": bool(valuation["is_extreme"]),
        "exemption_bonus": float(exemption_bonus),
        "chip_bonus": float(chip_bonus),
        "final_score": round(float(score), 1),
    }

    return round(score, 1), detail


# ===================== 自适应参数（v6_optimized calc_adaptive_params） =====================
def calc_adaptive_params(df, idx, market_slope=0, volatility_index=1.0):
    """计算自适应交易参数（移植自v6_optimized，含L4 R²止盈）"""
    st = STRATEGY
    if idx < 30:
        return {
            "risk_pct": st["base_risk_pct"],
            "stop_multiplier": st["base_atr_multiplier"],
            "chan_threshold": st["base_chan_threshold"],
            "add_threshold": 0.06,
            "take_profit_threshold": 0.40,
            "high_vol_risk_reduce": 1.0,
            "r2_20": 0.0,
        }

    close = df["close"].iloc[idx]
    ma60 = df["ma60"].iloc[idx] if not pd.isna(df["ma60"].iloc[idx]) else close
    trend_strength = (close - ma60) / ma60 if ma60 > 0 else 0
    total_trend = trend_strength + market_slope * 10

    high_vol_risk_reduce = 1.0
    if volatility_index > 1.5:
        high_vol_risk_reduce = 0.625
    elif volatility_index > 1.2:
        high_vol_risk_reduce = 0.75

    if volatility_index > 1.5:
        stop_multiplier = 3.0
    elif volatility_index > 1.2:
        stop_multiplier = 2.5
    elif volatility_index > 0.8:
        stop_multiplier = 2.0
    else:
        stop_multiplier = 1.5

    if total_trend > 0.20:
        chan_threshold = 0.85
    elif total_trend > 0.10:
        chan_threshold = 0.75
    elif total_trend > -0.05:
        chan_threshold = 0.65
    else:
        chan_threshold = 0.55

    if total_trend > 0.25:
        take_profit_threshold = 0.75
    elif total_trend > 0.15:
        take_profit_threshold = 0.60
    elif total_trend > 0.05:
        take_profit_threshold = 0.50
    else:
        take_profit_threshold = 0.40

    if total_trend > 0.03:
        risk_pct = min(0.10, st["base_risk_pct"] * 1.6) * high_vol_risk_reduce
    elif total_trend > 0.01:
        risk_pct = min(0.08, st["base_risk_pct"] * 1.3) * high_vol_risk_reduce
    elif total_trend > -0.01:
        risk_pct = st["base_risk_pct"] * high_vol_risk_reduce
    else:
        risk_pct = max(0.03, st["base_risk_pct"] * 0.5) * high_vol_risk_reduce

    if total_trend > 0.15:
        add_threshold = 0.04
    elif total_trend > 0.05:
        add_threshold = 0.06
    else:
        add_threshold = 0.08

    # L4：计算20日R²用于止盈调整
    closes_recent = df["close"].iloc[max(0, idx - 20):idx + 1].values
    r2_20 = calc_r2(closes_recent.tolist(), lookback=20)

    return {
        "risk_pct": risk_pct,
        "stop_multiplier": stop_multiplier,
        "chan_threshold": chan_threshold,
        "add_threshold": add_threshold,
        "take_profit_threshold": take_profit_threshold,
        "high_vol_risk_reduce": high_vol_risk_reduce,
        "r2_20": r2_20,
    }


# ===================== 大盘趋势计算（v6 L1宏观渐变） =====================
def calc_market_state(index_df: pd.DataFrame) -> Dict:
    """计算大盘状态和仓位系数（含L1宏观R²/MA60斜率渐变）"""
    st = STRATEGY
    df = index_df.copy()
    df["ma60"] = df["close"].rolling(60).mean()
    df["ma60_slope"] = (df["ma60"] - df["ma60"].shift(5)) / df["ma60"].shift(5)
    df["trend_strength"] = (df["close"] - df["ma60"]) / df["ma60"]

    last = df.iloc[-1]
    close = last["close"]
    ma60 = last["ma60"] if not pd.isna(last["ma60"]) else close
    trend_strength = last["trend_strength"] if not pd.isna(last["trend_strength"]) else 0
    ma60_slope = last["ma60_slope"] if not pd.isna(last["ma60_slope"]) else 0
    total_trend = trend_strength + ma60_slope * 10

    hard_cutoff = st["market_hard_cutoff"]
    allow_trade = bool(trend_strength >= hard_cutoff)

    # ========== L1宏观渐变：R²和MA60斜率判断仓位上限 ==========
    macro_r2 = calc_r2(df["close"].values.tolist(), lookback=60)
    macro_slope = float(ma60_slope) if not pd.isna(ma60_slope) else 0.0

    if macro_r2 < 0.05 or macro_slope < -0.005:
        macro_scale_cap = 0.3
    elif macro_r2 > 0.3 and macro_slope > 0.005:
        macro_scale_cap = 1.0
    else:
        r2_eff = max(0.05, min(0.3, macro_r2))
        if r2_eff >= 0.2:
            macro_scale_cap = 0.8 + (r2_eff - 0.2) / 0.1 * 0.2
        elif r2_eff >= 0.1:
            macro_scale_cap = 0.6 + (r2_eff - 0.1) / 0.1 * 0.2
        else:
            macro_scale_cap = 0.4 + (r2_eff - 0.05) / 0.05 * 0.2
        if macro_r2 > 0.3 and macro_slope <= 0.005:
            macro_scale_cap = 0.8

    # 【v6同步·优化3】R²保护：震荡市（macro_r2 < 0.15）强制压缩仓位上限至0.5
    if macro_r2 < 0.15:
        macro_scale_cap = min(macro_scale_cap, 0.5)

    # 仓位系数（移植自v6_optimized calc_continuous_position_scale）
    if trend_strength < hard_cutoff:
        pos_scale = 0.0
    else:
        raw_scale = 0.5 + 0.5 * np.tanh(total_trend * st["tanh_sensitivity"])
        pos_scale = st["min_position_scale"] + (st["max_position_scale"] - st["min_position_scale"]) * float(raw_scale)
        # 应用L1宏观渐变上限
        pos_scale = pos_scale * macro_scale_cap

    # 【v6同步·优化2】趋势因子：强趋势加仓1.2倍，弱趋势减仓至0.6倍
    if trend_strength > 0.15:
        pos_scale = min(pos_scale * 1.2, 1.0)
    elif trend_strength < 0:
        pos_scale = pos_scale * 0.6

    # 【v6同步·优化1】动态risk_value_ban：72 + trend_strength * 40，范围72~92
    risk_value_ban = float(np.clip(72 + trend_strength * 40, 72, 92))

    scan_date = last["date"].strftime("%Y-%m-%d") if hasattr(last["date"], "strftime") else str(last["date"])

    return {
        "scan_date": scan_date,
        "close": round(float(close), 2),
        "ma60": round(float(ma60), 2) if not pd.isna(ma60) else 0,
        "deviation_pct": round(float(trend_strength) * 100, 2),
        "allow_trade": allow_trade,
        "pos_scale": round(float(pos_scale), 4),
        "trend_strength": round(float(trend_strength), 4),
        "macro_r2": round(float(macro_r2), 4),
        "macro_slope": round(float(macro_slope), 6),
        "macro_scale_cap": round(float(macro_scale_cap), 4),
        "risk_value_ban": round(risk_value_ban, 2),
    }


# ===================== 单股扫描（第一阶段：评分+信号+ATR%收集） =====================
def scan_single_stock_phase1(code: str, market_pos_scale: float, market_slope: float,
                             idx_r2: float = 0.0, idx_dev: float = 0.0) -> Optional[Dict]:
    """
    第一阶段扫描：获取数据、计算指标、预评分、L2过滤、生成信号、收集ATR%
    返回包含信号信息和ATR%的字典，供第二阶段调整使用
    """
    st = STRATEGY
    try:
        df_raw = fetch_kline(code, KLINE_COUNT)
        if len(df_raw) < 120:
            return None

        # 数据新鲜度检查：最新K线超过7天视为退市/停牌，跳过
        last_date = pd.to_datetime(df_raw["date"].iloc[-1])
        if (pd.Timestamp.now() - last_date).days > 7:
            return None

        df = calc_indicators(df_raw)

        # 预评分过滤（v6增强评分 + 动态门槛）
        quality_score, score_detail = calc_stock_quality_score(df)
        # 【v6动态预评分门槛】根据上证指数R²和偏离度动态调整门槛
        base_threshold = st["pre_filter_threshold"]
        if idx_r2 > 0.5 and idx_dev > 0:
            dynamic_threshold = base_threshold - 5  # 牛市降门槛扩池子
        elif idx_r2 < 0.15 or idx_dev < -0.03:
            dynamic_threshold = base_threshold + 5  # 熊市升门槛精选
        else:
            dynamic_threshold = base_threshold
        if quality_score < dynamic_threshold:
            return {"code": code, "quality_score": quality_score, "score_detail": score_detail,
                    "passed": False, "buy": None, "sell": None, "atr_pct": None, "r2_120": None}

        # ========== L2修复：滚动计算120日R²（不再跳过，改为买入时仓位减半） ==========
        # 滚动计算：只用当前（最后一根K线）及之前120天的收盘价，避免未来函数
        _r2_closes = df["close"].iloc[max(0, len(df) - 120):].values.tolist()
        r2_120_rolling = calc_r2(_r2_closes, lookback=120)

        # ========== 优化：跳过全量adaptive_params_list循环 ==========
        # 只采样少量bar计算avg_threshold，只对最后一根K线计算adaptive_params
        n = len(df)
        if n > 30:
            sample_step = max(1, (n - 30) // 10)
            sample_indices = list(range(30, n, sample_step))[:10]
            chan_thresholds_sample = []
            for sidx in sample_indices:
                atr_series = df["atr"].iloc[max(0, sidx - 20):sidx + 1]
                atr_current = atr_series.iloc[-1]
                atr_ma20 = atr_series.mean()
                vol_idx = atr_current / atr_ma20 if atr_ma20 > 0 else 1.0
                s_params = calc_adaptive_params(df, sidx, market_slope, vol_idx)
                chan_thresholds_sample.append(s_params["chan_threshold"])
            avg_threshold = np.mean(chan_thresholds_sample) if chan_thresholds_sample else 0.70
        else:
            avg_threshold = 0.70

        # 缠论信号检测（需要完整历史做pivot检测，但只遍历pivot点，速度可接受）
        df = detect_chan_signals(df, pivot_win=5, chan_threshold=avg_threshold)

        # ========== 优化：只检查最后一根K线的信号（内联信号检查，替代原全量信号循环） ==========
        last_idx = n - 1
        last_row = df.iloc[last_idx]

        # 检查最后look根K线内是否有chan_buy
        look = st["chan_lookback"]
        chan_recent_last = False
        chan_type_last = ""
        for j in range(max(0, last_idx - look + 1), last_idx + 1):
            if df.loc[j, "chan_buy"]:
                chan_recent_last = True
                chan_type_last = df.loc[j, "chan_buy_type"]
                break

        # 信号条件检查（与主策略gen_signal_enhanced逻辑一致，只检查最后一根K线）
        breakout = not pd.isna(last_row["dc_high"]) and last_row["close"] > last_row["dc_high"]
        vol_ok = last_row["vol_effective"]
        chan_ok = chan_recent_last

        use_trend = st.get("use_trend_filter", True)
        use_money = st.get("use_money_filter", True)
        use_test_pullback = st.get("use_test_pullback", True)
        pullback_threshold = st.get("test_pullback_threshold", 0.02)
        test_vol_ratio = st.get("test_pullback_vol_ratio", 0.7)
        extreme_exemption = st.get("extreme_value_exemption", True)
        extreme_position_scale = st.get("extreme_value_position_scale", 0.3)

        trend_ok = False
        if use_trend:
            ma20_slope = last_row["ma20_slope"] if not pd.isna(last_row["ma20_slope"]) else 0
            price_above_ma60 = last_row["close"] > last_row["ma60"]
            trend_ok = (ma20_slope > 0.001) and price_above_ma60

        money_ok = False
        if use_money:
            money_strength = last_row["money_strength"] if not pd.isna(last_row["money_strength"]) else 0
            money_slope = last_row["money_slope"] if not pd.isna(last_row["money_slope"]) else 0
            money_ok = (money_strength > 0) and (money_slope > 0)

        score = 0.0
        if breakout: score += 1
        if vol_ok: score += 1
        if chan_ok: score += 2
        if trend_ok: score += 0.5
        if money_ok: score += 0.5

        # ========== 【v6同步·新增5个评分组件】与主策略gen_signal_enhanced完全对齐 ==========
        # 1. 风险值加分（risk_value < 33 低位超卖 +0.5）
        risk_val = float(last_row["risk_value"]) if "risk_value" in df.columns and not pd.isna(last_row["risk_value"]) else 50.0
        risk_score = 0.5 if risk_val < 33 else 0.0
        score += risk_score

        # 2. 堆量比评分（按市值分档动态阈值：>threshold +1.0，<threshold*0.3 -0.5）
        mc_est = estimate_market_cap(float(last_row["close"]))
        if mc_est >= 300:
            vs_threshold = st.get("vol_stack_threshold_large", 3.0)
        elif mc_est >= 100:
            vs_threshold = st.get("vol_stack_threshold_mid", 5.0)
        else:
            vs_threshold = st.get("vol_stack_threshold_small", 8.0)
        vol_stack_val = float(last_row["vol_stack_ratio"]) if "vol_stack_ratio" in df.columns and not pd.isna(last_row["vol_stack_ratio"]) else 1.0
        vol_stack_score = 0.0
        if vol_stack_val > vs_threshold:
            vol_stack_score = 1.0
        elif vol_stack_val < vs_threshold * 0.3:
            vol_stack_score = -0.5
        score += vol_stack_score

        # 3. 周线趋势（<0 -1.0，>0 +0.5）
        weekly_trend_val = float(last_row["weekly_trend"]) if "weekly_trend" in df.columns and not pd.isna(last_row["weekly_trend"]) else 0.0
        weekly_score = 0.0
        if weekly_trend_val < 0:
            weekly_score = -1.0
        elif weekly_trend_val > 0:
            weekly_score = 0.5
        score += weekly_score

        # 4. 早鸟突破评分（放量突破10日高点 +1.5，dc_high_aggressive缺失/NaN视为False）
        dc_high_agg = last_row["dc_high_aggressive"] if "dc_high_aggressive" in df.columns else np.nan
        vol_ma5_agg = float(last_row["vol_ma5"]) if not pd.isna(last_row["vol_ma5"]) else 0.0
        aggressive_break = bool((not pd.isna(dc_high_agg)) and (last_row["close"] > dc_high_agg) and (last_row["volume"] > vol_ma5_agg * 1.8))
        aggressive_score = 1.5 if aggressive_break else 0.0
        score += aggressive_score

        min_score = st["min_entry_score"]
        max_score = st["max_entry_score"]
        valuation = calc_valuation_percentile(df.iloc[:last_idx + 1])
        is_extreme_value = extreme_exemption and valuation["is_extreme"]

        if is_extreme_value:
            adjusted_min_score = max(0.5, min_score - 0.5)
            effective_pos_scale = min(market_pos_scale, extreme_position_scale)
        else:
            adjusted_min_score = min_score
            effective_pos_scale = market_pos_scale

        # 【v6同步·优化1】动态risk_value_ban（72 + trend_strength*40，范围72~92）与高风险仓位压缩
        ts_val = float(last_row["trend_strength"]) if "trend_strength" in df.columns and not pd.isna(last_row["trend_strength"]) else 0.0
        risk_value_ban = max(72.0, min(92.0, 72.0 + ts_val * 40))
        if risk_val > risk_value_ban:
            effective_pos_scale *= 0.6

        required_score = adjusted_min_score + (max_score - adjusted_min_score) * (1.0 - effective_pos_scale) * 0.6
        required_score = max(adjusted_min_score, min(max_score, required_score))

        # 【v6同步】低波动跳过（与主策略run_multi_backtest买入过滤对齐：ATR占股价比过低则拦截全部买入入口）
        _atr_pct = float(last_row["volatility_ratio"]) if "volatility_ratio" in df.columns and not pd.isna(last_row["volatility_ratio"]) else 0.03
        _low_vol_skip = _atr_pct < st.get("min_atr_pct", 0.015)

        stock_trend_ok = True
        if st["stock_trend_filter"] and "ma60_direction" in df.columns:
            stock_trend_ok = last_row["ma60_direction"] or effective_pos_scale > 0.85

        buy_signal = False
        buy_type = ""
        if score >= required_score and stock_trend_ok and not _low_vol_skip:
            buy_signal = True
            if is_extreme_value:
                buy_type = "估值极端抄底"
            elif chan_ok and breakout:
                buy_type = "缠论+突破"
            elif chan_ok:
                buy_type = "缠论买点"
            elif breakout:
                buy_type = "纯突破"
            else:
                buy_type = "综合评分"

        # 试盘回踩
        if use_test_pullback and not buy_signal and not _low_vol_skip:
            test_occurred = last_row["test_occurred"] if "test_occurred" in df.columns else False
            price = last_row["close"]
            ma20 = last_row["ma20"] if not pd.isna(last_row["ma20"]) else price
            pullback_cond = abs(price / ma20 - 1) < pullback_threshold
            vol_ma5 = last_row["vol_ma5"] if not pd.isna(last_row["vol_ma5"]) else 0
            vol_cond = last_row["volume"] < vol_ma5 * test_vol_ratio
            if test_occurred and pullback_cond and vol_cond and (trend_ok or money_ok) and effective_pos_scale > 0.5:
                buy_signal = True
                buy_type = "试盘回踩"

        # 【v6同步】早鸟突破独立触发路径（不要求close>ma60，仅保留风控ban与低波动拦截）
        if aggressive_break and risk_val <= risk_value_ban and not buy_signal and not _low_vol_skip:
            buy_signal = True
            buy_type = "早鸟突破"

        # 卖出信号
        exit_sig = not pd.isna(last_row["exit_low"]) and last_row["close"] < last_row["exit_low"]
        sell_signal = bool(exit_sig or last_row["chan_sell"])

        # 计算最后一根K线的adaptive_params（用于buy_info）
        if n > 30:
            atr_series_last = df["atr"].iloc[max(0, last_idx - 20):last_idx + 1]
            atr_current_last = atr_series_last.iloc[-1]
            atr_ma20_last = atr_series_last.mean()
            vol_idx_last = atr_current_last / atr_ma20_last if atr_ma20_last > 0 else 1.0
            last_params = calc_adaptive_params(df, last_idx, market_slope, vol_idx_last)
        else:
            last_params = {"risk_pct": st["base_risk_pct"], "stop_multiplier": st["base_atr_multiplier"],
                           "chan_threshold": st["base_chan_threshold"], "r2_20": 0.0}

        # 检查最新一根K线
        buy_info = None
        sell_info = None

        # 买入信号
        if buy_signal:
            close = last_row["close"]
            atr = last_row["atr"]
            dc_high = last_row["dc_high"]

            risk_pct = last_params["risk_pct"]
            stop_multiplier_base = last_params["stop_multiplier"]

            # 波动率自适应调整（个股内部）
            vol_percentile = last_row["volatility_percentile"] if not pd.isna(last_row["volatility_percentile"]) else 0.5
            if st["volatility_adaptive"]:
                vol_stop_multiplier = st["volatility_stop_multiplier_min"] + (st["volatility_stop_multiplier_max"] - st["volatility_stop_multiplier_min"]) * vol_percentile
                vol_pos_reduce = 1.0 - st["volatility_position_scale_factor"] * vol_percentile
            else:
                vol_stop_multiplier = 1.0
                vol_pos_reduce = 1.0

            actual_stop_multiplier = stop_multiplier_base * vol_stop_multiplier
            actual_risk_pct = risk_pct * vol_pos_reduce

            stop_loss = close - atr * actual_stop_multiplier if atr > 0 else close * 0.95

            stop_distance_pct = (atr * actual_stop_multiplier) / close if close > 0 else 0.05
            suggested_position = (actual_risk_pct * market_pos_scale) / stop_distance_pct if stop_distance_pct > 0 else 0
            suggested_position = min(suggested_position, 0.45)

            # 【L2修复】滚动R² < 0.15时仓位减半（不直接跳过）
            if r2_120_rolling < 0.15:
                suggested_position *= 0.5

            chan_type = chan_type_last if chan_type_last else (buy_type if buy_type else "无")

            # ========== L4 R²止盈 ==========
            r2_20 = last_params.get("r2_20", 0.0)
            if r2_20 > 0.15:
                take_profit_pct = 0.15
            elif r2_20 > 0.05:
                take_profit_pct = 0.25
            else:
                take_profit_pct = 0.40

            # 【v6同步·优化4】动态阶梯止盈触发线：强趋势20%，弱趋势10%，中性15%
            _sell_ts = float(last_row["trend_strength"]) if "trend_strength" in last_row.index and not pd.isna(last_row["trend_strength"]) else 0.0
            if _sell_ts > 0.15:
                _tier_trigger = 0.20
            elif _sell_ts < -0.05:
                _tier_trigger = 0.10
            else:
                _tier_trigger = 0.15

            # 计算止盈价
            take_profit_price = round(float(close * (1 + take_profit_pct)), 2)

            # ========== 分批买入价位区间 ==========
            ma5_val = float(last_row["ma5"]) if "ma5" in last_row.index and not pd.isna(last_row["ma5"]) else float("nan")
            ma20_val = float(last_row["ma20"]) if "ma20" in last_row.index and not pd.isna(last_row["ma20"]) else float("nan")
            stop_loss_val = float(stop_loss)

            def _safe_round(v, n=2):
                try:
                    if v is None or (isinstance(v, float) and (pd.isna(v) or v == float("inf") or v == float("-inf"))):
                        return None
                    return round(float(v), n)
                except Exception:
                    return None

            def _fmt_zone(low, high):
                lo = _safe_round(low, 2)
                hi = _safe_round(high, 2)
                if lo is None or hi is None:
                    return None
                if lo > hi:
                    lo, hi = hi, lo
                return f"{lo}~{hi}"

            # 第一批：MA5附近，下限不低于止损上方2%
            zone1_low = None
            zone1_high = None
            if not pd.isna(ma5_val) and ma5_val > 0:
                zone1_low = max(ma5_val * 0.99, stop_loss_val * 1.02)
                zone1_high = ma5_val * 1.01
            entry_zone_1 = _fmt_zone(zone1_low, zone1_high)
            if entry_zone_1 is None:
                entry_zone_1 = _fmt_zone(close * 0.99, close * 1.01)

            # 第二批：MA20附近深回踩，下限不低于止损
            zone2_low = None
            zone2_high = None
            if not pd.isna(ma20_val) and ma20_val > 0:
                zone2_low = max(ma20_val * 0.98, stop_loss_val * 1.01)
                zone2_high = ma20_val * 1.02
            entry_zone_2 = _fmt_zone(zone2_low, zone2_high)
            if entry_zone_2 is None:
                entry_zone_2 = _fmt_zone(close * 0.97, close * 0.99)

            # 第三批：突破追涨，dc_high上方0.2%确认
            entry_breakout = None
            if not pd.isna(dc_high) and dc_high > 0:
                entry_breakout = _safe_round(dc_high * 1.002, 2)
            if entry_breakout is None:
                entry_breakout = _safe_round(close * 1.005, 2)

            # 安全检查：所有区间下限不得低于止损价
            def _clamp_zone_low(zone_str, sl):
                if zone_str is None:
                    return zone_str
                try:
                    parts = zone_str.split("~")
                    lo, hi = float(parts[0]), float(parts[1])
                    if lo < sl:
                        lo = round(sl, 2)
                    if lo > hi:
                        hi = lo
                    return f"{round(lo,2)}~{round(hi,2)}"
                except Exception:
                    return zone_str

            entry_zone_1 = _clamp_zone_low(entry_zone_1, stop_loss_val)
            entry_zone_2 = _clamp_zone_low(entry_zone_2, stop_loss_val)

            buy_info = {
                "code": code,
                "close": round(float(close), 2),
                "dc_high": round(float(dc_high), 2) if not pd.isna(dc_high) else 0,
                "ma5": _safe_round(ma5_val, 2) if not pd.isna(ma5_val) else 0,
                "ma20": _safe_round(ma20_val, 2) if not pd.isna(ma20_val) else 0,
                "entry_zone_1": entry_zone_1,
                "entry_zone_2": entry_zone_2,
                "entry_breakout": entry_breakout,
                "buy_range": entry_zone_1 if entry_zone_1 else f"{round(float(close)*0.99,2)}~{round(float(close)*1.01,2)}",
                "atr": round(float(atr), 2) if not pd.isna(atr) else 0,
                "stop_loss": round(float(stop_loss), 2),
                "stop_pct": round(float((close - stop_loss) / close * 100), 1) if close > 0 else 0,
                "take_profit": take_profit_price,
                "take_profit_pct": int(take_profit_pct * 100),
                "suggested_position_pct": round(float(suggested_position * 100), 1),
                "chan_type": str(chan_type),
                "vol_effective": bool(last_row["vol_effective"]),
                "r2_20": round(float(r2_20), 3),
                "r2_120_rolling": round(float(r2_120_rolling), 3),
                "tier_trigger": round(float(_tier_trigger), 2),
                "stop_multiplier_base": round(float(stop_multiplier_base), 2),
                "vol_stop_multiplier": round(float(vol_stop_multiplier), 2),
            }

        # 卖出信号
        if sell_signal:
            close = last_row["close"]
            exit_low = last_row["exit_low"]
            chan_sell = bool(last_row["chan_sell"])
            exit_break = not pd.isna(exit_low) and close < exit_low

            sell_reasons = []
            if exit_break:
                sell_reasons.append("跌破动态退出线")
            if chan_sell:
                sell_reasons.append("缠论顶背离")

            sell_info = {
                "code": code,
                "close": round(float(close), 2),
                "exit_low": round(float(exit_low), 2) if not pd.isna(exit_low) else 0,
                "chan_sell": bool(chan_sell),
                "exit_break": bool(exit_break),
                "reasons": "、".join(sell_reasons),
            }

        # ========== L3收集ATR% ==========
        valid_atr = df["atr"].dropna()
        if len(valid_atr) > 0:
            valid_close = df["close"].loc[valid_atr.index]
            atr_pct = float((valid_atr / valid_close * 100).mean())
        else:
            atr_pct = 0.0

        return {
            "code": code,
            "quality_score": quality_score,
            "score_detail": score_detail,
            "passed": True,
            "r2_120": round(float(r2_120_rolling), 3),
            "atr_pct": round(float(atr_pct), 2),
            "buy": buy_info,
            "sell": sell_info,
            "signal_debug": {
                "breakout": bool(breakout),
                "vol_ok": bool(vol_ok),
                "chan_ok": bool(chan_ok),
                "trend_ok": bool(trend_ok),
                "money_ok": bool(money_ok),
                "risk_val": round(float(risk_val), 2),
                "risk_score": risk_score,
                "mc_est": mc_est,
                "vs_threshold": vs_threshold,
                "vol_stack_val": round(float(vol_stack_val), 3),
                "vol_stack_score": vol_stack_score,
                "weekly_trend": weekly_trend_val,
                "weekly_score": weekly_score,
                "aggressive_break": bool(aggressive_break),
                "aggressive_score": aggressive_score,
                "score": round(float(score), 4),
                "required_score": round(float(required_score), 4),
                "effective_pos_scale": round(float(effective_pos_scale), 4),
                "risk_value_ban": round(float(risk_value_ban), 2),
                "low_vol_skip": bool(_low_vol_skip),
                "stock_trend_ok": bool(stock_trend_ok),
                "is_extreme_value": bool(is_extreme_value),
                "buy_signal": bool(buy_signal),
                "buy_type": buy_type,
            },
        }

    except Exception as e:
        print(f"  [ERROR] {code}: {str(e)[:100]}")
        return None


# ===================== 多进程worker =====================
def _mp_worker(args):
    """多进程worker：处理一批股票"""
    codes, market_pos_scale, market_slope, idx_r2, idx_dev, strategy_copy = args
    global STRATEGY
    STRATEGY.update(strategy_copy)
    results = []
    for code in codes:
        try:
            r = scan_single_stock_phase1(code, market_pos_scale, market_slope, idx_r2, idx_dev)
            results.append(r)
        except Exception as e:
            print(f"  [ERROR] {code}: {str(e)[:100]}")
            results.append(None)
    return results


async def scan_single_stock_phase1_async(code: str, market_pos_scale: float, market_slope: float,
                                         sem: asyncio.Semaphore, idx_r2: float = 0.0, idx_dev: float = 0.0) -> Optional[Dict]:
    """异步第一阶段扫描"""
    async with sem:
        return await asyncio.to_thread(scan_single_stock_phase1, code, market_pos_scale, market_slope, idx_r2, idx_dev)


# ===================== L3跨股波动率百分位调整（第二阶段） =====================
def apply_l3_vol_percentile_adjustment(results: List[Dict]) -> None:
    """
    第二阶段：根据所有通过评分股票的ATR%计算跨股波动率百分位，
    调整止损倍数和建议仓位（原地修改results中的buy_info）
    """
    # 收集所有通过评分股票的ATR%
    atr_pct_map = {}
    all_atr_vals = []
    for r in results:
        if r and r.get("passed") and r.get("atr_pct") is not None:
            atr_pct_map[r["code"]] = r["atr_pct"]
            all_atr_vals.append(r["atr_pct"])

    if not all_atr_vals:
        return

    for r in results:
        if not r or not r.get("buy"):
            continue

        buy = r["buy"]
        stock_atr_pct = atr_pct_map.get(r["code"], 0)

        # 计算跨股波动率百分位
        vol_percentile = sum(1 for a in all_atr_vals if a <= stock_atr_pct) / len(all_atr_vals) * 100

        # L3自适应ATR倍数
        if vol_percentile > 70:
            adaptive_atr_mult = 2.5 + (vol_percentile - 70) / 30 * 0.5
        elif vol_percentile >= 40:
            adaptive_atr_mult = 2.0 + (vol_percentile - 40) / 30 * 0.5
        else:
            adaptive_atr_mult = 1.5 + vol_percentile / 40 * 0.5

        # 重新计算止损位（用L3调整后的倍数）
        close = buy["close"]
        atr = buy["atr"]
        # 原 stop_multiplier_base * vol_stop_multiplier → 替换为 L3 adaptive_atr_mult
        new_stop_multiplier = adaptive_atr_mult
        stop_loss = close - atr * new_stop_multiplier if atr > 0 else close * 0.95

        buy["stop_loss"] = round(float(stop_loss), 2)
        buy["stop_pct"] = round(float((close - stop_loss) / close * 100), 1) if close > 0 else 0
        buy["vol_percentile"] = round(float(vol_percentile), 1)
        buy["l3_atr_mult"] = round(float(adaptive_atr_mult), 2)

        # 调整建议仓位
        stop_distance_pct = (atr * new_stop_multiplier) / close if close > 0 else 0.05
        # 使用原始risk_pct（从buy_info中无法直接获取，用估算）
        # 保持原suggested_position但根据波动率百分位微调
        if vol_percentile > 70:
            # 高波动股票降低仓位
            buy["suggested_position_pct"] = round(buy["suggested_position_pct"] * 0.7, 1)
        elif vol_percentile < 30:
            # 低波动股票可以略增仓位
            buy["suggested_position_pct"] = round(min(buy["suggested_position_pct"] * 1.15, 45.0), 1)


# ===================== 主流程 =====================
async def main():
    result_mode = sys.argv[1] if len(sys.argv) > 1 else "display_only"
    print(f"[参数] result_mode={result_mode}")
    print(f"[版本] 龟缠量化v6_optimized 每日信号扫描")

    # ===================== 交易日判断 =====================
    _today_str = time.strftime('%Y-%m-%d')
    _flag_dir = os.environ.get("TRADING_DAY_FLAG_DIR", f"{_ROOT}/data")
    _flag_file = os.path.join(_flag_dir, f"not_trading_day_{_today_str}.txt")
    if os.path.exists(_flag_file):
        print(f"[跳过] 今天 {_today_str} 非交易日（标记文件已存在），退出")
        return
    if time.localtime().tm_wday >= 5:
        print(f"[跳过] 今天是周末，非交易日")
        os.makedirs(os.path.dirname(_flag_file), exist_ok=True)
        with open(_flag_file, 'w') as f:
            f.write(f"weekend: {_today_str}")
        return
    try:
        _is_trading_day = False
        for _ep in [KLINE_API] + KLINE_API_FALLBACKS:
            try:
                _resp = requests.get(f"{_ep}?param=sh000001,day,,,1,qfq", timeout=10, headers=HTTP_HEADERS)
                if _resp.status_code != 200 or _resp.text.lstrip().startswith("<"):
                    continue
                _j = _resp.json()
                _v = _j.get('data', {}).get('sh000001', {})
                _bars = _v.get('qfqday') or _v.get('day')
                if _bars:
                    _latest = _bars[-1][0]
                    if _latest == _today_str:
                        _is_trading_day = True
                    break
            except Exception:
                continue
        if not _is_trading_day:
            print(f"[跳过] 今天 {_today_str} 非交易日（最新K线未更新）")
            os.makedirs(os.path.dirname(_flag_file), exist_ok=True)
            with open(_flag_file, 'w') as f:
                f.write(f"holiday: {_today_str}, last_kline: {_latest if '_latest' in dir() else 'unknown'}")
            return
    except Exception as _e:
        print(f"[警告] 交易日判断API失败: {_e}，继续执行")
    # ===================== 交易日判断结束 =====================

    sdk = CodeActSDK()

    try:
        # 读取股票代码列表（优先主板全量，回退沪深300）
        codes = []
        code_name_map = {}
        import os as _os
        if _os.path.exists(MAIN_BOARD_FILE):
            with open(MAIN_BOARD_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split(",", 1)
                    code = parts[0]
                    name = parts[1] if len(parts) > 1 else ""
                    # 过滤ST、退市、PT股票
                    if any(kw in name.upper() for kw in ["ST", "退", "PT"]):
                        continue
                    codes.append(code)
                    if name:
                        code_name_map[code] = name
        else:
            with open(HS300_FILE, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    codes.append(line)
        print(f"[扫描] 共 {len(codes)} 只股票（已过滤ST/退市/PT）")

        # 获取大盘指数数据
        print("[大盘] 获取上证指数数据...")
        index_df = fetch_kline(INDEX_CODE, KLINE_COUNT)
        market_state = calc_market_state(index_df)
        print(f"[大盘] 日期={market_state['scan_date']} 收盘={market_state['close']} "
              f"MA60={market_state['ma60']} 偏离={market_state['deviation_pct']}% "
              f"允许交易={market_state['allow_trade']} 仓位系数={market_state['pos_scale']}")
        print(f"[L1宏观] R²={market_state['macro_r2']} MA60斜率={market_state['macro_slope']} "
              f"仓位上限={market_state['macro_scale_cap']}")

        # 获取深证成指数据
        print("[大盘] 获取深证成指数据...")
        try:
            sz_index_df = fetch_kline(SZ_INDEX_CODE, KLINE_COUNT)
            sz_close = float(sz_index_df["close"].iloc[-1])
            sz_ma60 = float(sz_index_df["close"].rolling(60).mean().iloc[-1])
            sz_dev = round((sz_close - sz_ma60) / sz_ma60 * 100, 2)
            market_state['sz_close'] = sz_close
            market_state['sz_ma60'] = round(sz_ma60, 2)
            market_state['sz_deviation_pct'] = sz_dev
            print(f"[大盘] 深证成指 收盘={sz_close} MA60={round(sz_ma60,2)} 偏离={sz_dev}%")
            # 深证成指辅助风控
            if sz_dev < -8:
                market_state['allow_trade'] = False
                market_state['sz_risk_warning'] = "深证偏离<-8%，禁止买入"
                print(f"[风控] 深证偏离{sz_dev}%<-8%，强制禁止买入")
            elif sz_dev < -5:
                market_state['pos_scale'] = round(market_state['pos_scale'] * 0.5, 4)
                market_state['sz_risk_warning'] = f"深证偏离{sz_dev}%<-5%，仓位减半"
                print(f"[风控] 深证偏离{sz_dev}%<-5%，仓位减半至{market_state['pos_scale']}")
            else:
                market_state['sz_risk_warning'] = ""
        except Exception as e:
            print(f"[大盘] 深证成指获取失败: {e}")
            market_state['sz_close'] = None
            market_state['sz_ma60'] = None
            market_state['sz_deviation_pct'] = None
            market_state['sz_risk_warning'] = ""

        # ========== 自适应参数：根据市场状态加载优化参数 ==========
        current_state = classify_market_state(
            market_state['macro_r2'], market_state['macro_slope'], market_state['trend_strength'] / 100)
        adaptive_info = apply_adaptive_params(current_state)
        market_state['market_state'] = current_state
        market_state['adaptive_params'] = adaptive_info
        if adaptive_info['applied']:
            p = adaptive_info['params']
            print(f"[自适应] 市场状态={current_state} → 已加载优化参数: "
                  f"entry={p['min_entry_score']:.3f} atr={p['atr_multiplier']:.3f} "
                  f"ch={p['channel_base']} exit={p['exit_base']} "
                  f"vr=[{p['vol_ratio_low']:.3f},{p['vol_ratio_high']:.3f}] "
                  f"ts=[{p['trend_str_low']:.3f},{p['trend_str_high']:.3f}]")
        else:
            print(f"[自适应] 市场状态={current_state} → 无可用优化参数，使用v6默认参数")

        # 计算大盘MA20斜率（用于自适应参数）
        index_df["ma20"] = index_df["close"].rolling(20).mean()
        index_df["ma20_slope"] = (index_df["ma20"] - index_df["ma20"].shift(5)) / index_df["ma20"].shift(5)
        market_slope = index_df["ma20_slope"].iloc[-1] if not pd.isna(index_df["ma20_slope"].iloc[-1]) else 0.0

        # ========== 第一阶段：并发扫描所有股票（asyncio，I/O与CPU重叠） ==========
        print(f"[Phase1] 开始并发扫描 {len(codes)} 只股票（并发={MAX_CONCURRENCY}）...")
        sem = asyncio.Semaphore(MAX_CONCURRENCY)
        tasks = [scan_single_stock_phase1_async(code, market_state["pos_scale"], market_slope, sem,
                                                 market_state["macro_r2"], market_state["trend_strength"])
                 for code in codes]
        # 带进度日志的gather：每500只打印一次
        results_raw = []
        done_count = 0
        for coro in asyncio.as_completed(tasks):
            r = await coro
            results_raw.append(r)
            done_count += 1
            if done_count % 500 == 0 or done_count == len(codes):
                print(f"[Phase1] 进度 {done_count}/{len(codes)} ({done_count*100//len(codes)}%)")

        # 过滤异常结果
        results = []
        failed_count = 0
        for i, r in enumerate(results_raw):
            if isinstance(r, Exception):
                failed_count += 1
            elif r is None:
                failed_count += 1
            else:
                results.append(r)

        # ========== 第二阶段：L3跨股波动率百分位调整 ==========
        print("[Phase2] 开始第二阶段L3跨股波动率百分位调整...")
        apply_l3_vol_percentile_adjustment(results)

        # 统计
        total_scanned = len(codes)
        total_failed = failed_count
        total_passed = sum(1 for r in results if r.get("passed"))
        total_l2_filtered = sum(1 for r in results if r.get("l2_filtered"))
        buy_signals = []
        for r in results:
            if r.get("buy"):
                b = r["buy"]
                b["quality_score"] = r.get("quality_score", 0)
                buy_signals.append(b)
        sell_signals = [r["sell"] for r in results if r.get("sell")]

        # 为买入信号补充股票名称（优先本地映射，缺失时通过腾讯API批量获取）
        _missing_name_codes = [b["code"] for b in buy_signals if not code_name_map.get(b["code"], "")]
        if _missing_name_codes:
            try:
                _batch_codes = ",".join(_missing_name_codes[:50])
                _resp = requests.get(f"https://qt.gtimg.cn/q={_batch_codes}", timeout=10, headers=HTTP_HEADERS)
                _resp.encoding = "gbk"
                for _line in _resp.text.strip().split("\n"):
                    _line = _line.strip()
                    if not _line or "=" not in _line:
                        continue
                    try:
                        _payload = _line.split("=", 1)[1].strip(' ";')
                        _parts = _payload.split("~")
                        if len(_parts) > 2:
                            _c = _parts[2] if len(_parts) > 2 else ""
                            _n = _parts[1] if len(_parts) > 1 else ""
                            # 从v_code前缀恢复带市场前缀的code
                            for _mc in _missing_name_codes:
                                if _mc.endswith(_c) and _n:
                                    code_name_map[_mc] = _n
                                    break
                    except Exception:
                        continue
            except Exception as _e:
                print(f"[名称] 腾讯API批量获取名称失败: {_e}")
        for b in buy_signals:
            b["name"] = code_name_map.get(b["code"], "")

        print(f"[统计] 扫描总数={total_scanned} 失败={total_failed} "
              f"通过预评分={total_passed} L2过滤={total_l2_filtered} "
              f"买入信号={len(buy_signals)} 卖出信号={len(sell_signals)}")

        # 大盘不允许交易时清空买入信号
        if not market_state["allow_trade"]:
            print("[大盘] 偏离MA60超-3%，禁止买入信号")
            buy_signals = []

        # 按综合质量评分降序排列（评分越高=股票越好），同评分按仓位排序
        buy_signals.sort(key=lambda x: (x.get("quality_score", 0), x.get("suggested_position_pct", 0)), reverse=True)
        PUSH_TOP_N = 10
        push_buy_signals = buy_signals[:PUSH_TOP_N]

        # 构建输出消息
        message = build_scan_report(
            market_state, total_scanned, total_failed, total_passed, total_l2_filtered,
            push_buy_signals, sell_signals, result_mode
        )
        # 消息中补充实际买入信号总数
        if len(buy_signals) > PUSH_TOP_N:
            message += f"\n\n📋 共{len(buy_signals)}只买入信号，仅展示Top{PUSH_TOP_N}，完整列表见报告"

        # 写入完整报告到文件
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        report_path = os.path.join(OUTPUT_DIR, "daily_signal_scan_report.txt")
        full_report = build_full_report(
            market_state, total_scanned, total_failed, total_passed, total_l2_filtered,
            buy_signals, sell_signals, results
        )
        with open(report_path, "w", encoding="utf-8") as f:
            f.write(full_report)
        print(f"[输出] 完整报告已保存: {report_path}")

        # 保存信号数据供模拟交易跟踪使用
        signals_path = os.path.join(OUTPUT_DIR, "latest_signals.json")
        signals_data = {
            "scan_date": str(market_state["scan_date"]),
            "market_state": current_state,
            "buy_signals": buy_signals,
            "sell_signals": sell_signals,
        }
        with open(signals_path, "w", encoding="utf-8") as f:
            json.dump(signals_data, f, ensure_ascii=False, indent=2, default=str)
        print(f"[输出] 信号数据已保存: {signals_path}")

        abs_report_path = os.path.abspath(report_path)

        # 映射 result_mode
        actual_mode = result_mode
        if result_mode == "auto":
            if buy_signals or sell_signals:
                actual_mode = "display_only"
            else:
                actual_mode = "no_reply"

        # 构建message
        if actual_mode == "no_reply":
            final_message = "NO_REPLY"
        else:
            final_message = message + f"\n\n完整报告：[daily_signal_scan_report](computer://{abs_report_path})"

        await sdk.submit_result(
            result_mode=actual_mode,
            status="success",
            message=final_message,
            data={
                "scan_date": str(market_state["scan_date"]),
                "total_scanned": int(total_scanned),
                "total_failed": int(total_failed),
                "total_passed": int(total_passed),
                "total_l2_filtered": int(total_l2_filtered),
                "buy_count": int(len(buy_signals)),
                "sell_count": int(len(sell_signals)),
                "allow_trade": bool(market_state["allow_trade"]),
                "macro_r2": float(market_state["macro_r2"]),
                "macro_scale_cap": float(market_state["macro_scale_cap"]),
                "pos_scale": float(market_state["pos_scale"]),
                "report_path": str(report_path),
            },
        )

    except Exception as e:
        import traceback
        traceback.print_exc()
        await sdk.submit_result(
            result_mode="notify",
            status="error",
            message=f"每日信号扫描执行失败: {str(e)[:200]}",
            data={"error_type": type(e).__name__},
        )


def build_scan_report(market_state, total_scanned, total_failed, total_passed, total_l2_filtered,
                      buy_signals, sell_signals, result_mode) -> str:
    """构建用户摘要消息（简洁版）"""
    lines = []
    date = market_state['scan_date']
    ms = market_state.get('market_state', '?')
    close = market_state['close']
    dev = market_state['deviation_pct']
    allow = '✅' if market_state['allow_trade'] else '❌禁买'
    sz_close = market_state.get('sz_close')
    sz_dev = market_state.get('sz_deviation_pct', 'N/A')
    sz_str = f" 深证:{sz_close}({sz_dev}%)" if sz_close else ""
    sz_warn = market_state.get('sz_risk_warning', '')
    warn_str = f" ⚠️{sz_warn}" if sz_warn else ""

    lines.append(f"📊 {date} | {ms} | 上证:{close}({dev}%){allow}{sz_str}{warn_str}")
    lines.append(f"扫描{total_scanned}只 失败{total_failed} | 买入{len(buy_signals)} 卖出{len(sell_signals)}")

    if buy_signals:
        lines.append("")
        for b in buy_signals:
            _name = b.get("name", "")
            _name_str = f" {_name}" if _name else ""
            _entry = b.get("entry_zone_1") or b.get("buy_range") or b["close"]
            lines.append(f"🔔 {b['code']}{_name_str} 评分{b.get('quality_score','?')} 收盘{b['close']} 买入区间{_entry} 止损{b['stop_loss']} 止盈{b.get('take_profit', 'N/A')} 仓位{b['suggested_position_pct']}% {b.get('chan_type','')}")

    if sell_signals:
        lines.append("")
        for s in sell_signals:
            lines.append(f"⚠️ {s['code']} @{s['close']} {s['reasons']}")

    if not buy_signals and not sell_signals:
        if not market_state["allow_trade"]:
            lines.append("大盘偏离超-3%，禁止买入")
        else:
            lines.append("今日无活跃信号")

    return "\n".join(lines)


def build_full_report(market_state, total_scanned, total_failed, total_passed, total_l2_filtered,
                      buy_signals, sell_signals, results) -> str:
    """构建完整报告文件"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"每日信号扫描完整报告 v6_optimized — {market_state['scan_date']}")
    lines.append("=" * 70)
    lines.append("")

    lines.append("【大盘状态】")
    lines.append(f"  扫描日期: {market_state['scan_date']}")
    lines.append(f"  上证收盘: {market_state['close']}")
    lines.append(f"  MA60: {market_state['ma60']}")
    lines.append(f"  偏离度: {market_state['deviation_pct']}%")
    sz_close = market_state.get('sz_close')
    if sz_close is not None:
        lines.append(f"  深证收盘: {sz_close}")
        lines.append(f"  深证MA60: {market_state.get('sz_ma60','N/A')}")
        lines.append(f"  深证偏离度: {market_state.get('sz_deviation_pct','N/A')}%")
    lines.append(f"  趋势强度: {market_state['trend_strength']}")
    lines.append(f"  允许交易: {'是' if market_state['allow_trade'] else '否'}")
    lines.append(f"  L1宏观R²: {market_state['macro_r2']}")
    lines.append(f"  L1 MA60斜率: {market_state['macro_slope']}")
    lines.append(f"  L1仓位上限(macro_scale_cap): {market_state['macro_scale_cap']}")
    lines.append(f"  L1风险值禁线(risk_value_ban): {market_state.get('risk_value_ban', 'N/A')}")
    lines.append(f"  最终仓位系数: {market_state['pos_scale']}")
    lines.append("")

    lines.append("【扫描统计】")
    lines.append(f"  扫描总数: {total_scanned}")
    lines.append(f"  获取失败: {total_failed}")
    lines.append(f"  通过预评分: {total_passed}")
    lines.append(f"  L2行业R²过滤: {total_l2_filtered}")
    lines.append(f"  买入信号数: {len(buy_signals)}")
    lines.append(f"  卖出信号数: {len(sell_signals)}")
    lines.append("")

    REPORT_TOP_N = 10

    if buy_signals:
        lines.append(f"【买入信号 Top{REPORT_TOP_N}（按综合质量评分排序）】")
        lines.append("-" * 70)
        for i, b in enumerate(buy_signals[:REPORT_TOP_N], 1):
            _name = b.get("name", "")
            _name_str = f"（{_name}）" if _name else ""
            lines.append(f"  #{i} {b['code']}{_name_str}  评分:{b.get('quality_score', 'N/A')}")
            lines.append(f"    收盘价: {b['close']}  缠论信号: {b['chan_type']}")
            lines.append(f"    MA5支撑: {b.get('ma5', 'N/A')}  MA20支撑: {b.get('ma20', 'N/A')}")
            lines.append(f"    止损位: {b['stop_loss']} (距收盘 {b['stop_pct']}%)  止盈: {b.get('take_profit', 'N/A')}")
            lines.append(f"    ── 分批买入方案 ──")
            lines.append(f"    第一批(MA5附近): {b.get('entry_zone_1', 'N/A')}")
            lines.append(f"    第二批(MA20深回踩): {b.get('entry_zone_2', 'N/A')}")
            lines.append(f"    突破追涨: {b.get('entry_breakout', 'N/A')}")
            lines.append(f"    建议仓位: {b['suggested_position_pct']}%  ATR: {b['atr']}")
            lines.append("")
        if len(buy_signals) > REPORT_TOP_N:
            lines.append(f"  （买入信号共{len(buy_signals)}只，仅展示Top{REPORT_TOP_N}）")
            lines.append("")
    else:
        lines.append("【买入信号】今日无买入信号")
        lines.append("")

    if sell_signals:
        lines.append(f"【卖出信号 Top{REPORT_TOP_N}】")
        lines.append("-" * 70)
        for i, s in enumerate(sell_signals[:REPORT_TOP_N], 1):
            lines.append(f"  #{i} {s['code']} @{s['close']}  {s['reasons']}")
        if len(sell_signals) > REPORT_TOP_N:
            lines.append("")
            lines.append(f"  （卖出信号共{len(sell_signals)}只，仅展示Top{REPORT_TOP_N}）")
        lines.append("")
    else:
        lines.append("【卖出信号】今日无卖出信号")
        lines.append("")

    lines.append("=" * 70)
    lines.append(f"报告生成完毕 — 龟缠量化v6_optimized — {market_state['scan_date']}")
    lines.append("=" * 70)

    return "\n".join(lines)


if __name__ == "__main__":
    asyncio.run(main())
