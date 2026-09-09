#!/usr/bin/env python3
"""
龟缠安泰 v5.3.4.1 — 动态核心参数版
基于 v5.3.3 修改：海龟通道周期+退出线周期+预评分门槛随市场状态动态调整
继承 v5.3.3 全部逻辑：
1. min_entry_score 从 2.0 降至 1.7（增加候选池，解决集中度过高）
2. 增加"估值极端低豁免条款"（PE/PB历史分位<10%时允许小仓位试错）
3. 增加熊市末期抄底信号（极端估值+恐慌性抛售后反弹）
4. 保留 L1-L4 四层自适应引擎全部逻辑
5. 保留 2008 年熊市完美空仓的宏观硬截断
"""

import json
import glob
import subprocess
import sys
import os
import argparse

# 持久化第三方库路径（optuna等，避免沙箱重启后重装）
_lib_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'python_libs')
if os.path.exists(_lib_path):
    sys.path.insert(0, _lib_path)
import requests
from datetime import datetime, timedelta
from pathlib import Path
from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Dict
import numpy as np
import pandas as pd
import matplotlib

# ===================== 配置加载 =====================
# 【修改】删除所有硬编码路径，改用 config/loader.py 统一管理
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config.loader import load_config, get_config, get_path
from scripts.trading_rules import calc_trade_cost as _shared_calc_trade_cost

_cfg = load_config()

if sys.platform.startswith("win"):
    matplotlib.rcParams["font.sans-serif"] = ["SimHei"]
elif sys.platform == "darwin":
    matplotlib.rcParams["font.sans-serif"] = ["PingFang SC"]
else:
    matplotlib.rcParams["font.sans-serif"] = ["WenQuanYi Micro Hei", "Noto Sans CJK SC", "DejaVu Sans"]
matplotlib.use('Agg')
matplotlib.rcParams['axes.unicode_minus'] = False
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import time
import warnings
import random
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
warnings.filterwarnings('ignore')

# ===================== 路径配置（从 config/settings.yaml 加载） =====================
# 【修改】删除所有硬编码路径，统一从配置文件读取
_paths = _cfg['paths']
BASE_DIR = _paths['base_dir']
DATA_DIR = _paths['data_dir']
STOCK_DATA_DIR = _paths['stock_data_dir']
RAW_DATA_DIR = _paths['raw_data_dir']
INDEX_DATA_DIR = _paths['index_data_dir']
HS300_CODES_FILE = _paths['hs300_codes_file']
MAIN_BOARD_CODES_FILE = _paths['main_board_codes_file']
SINA_API = "http://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
LOG_FILE = _paths['log_file']
SKILL_PATH_DP = _paths['skill_path_dp']
STATE_FILE = _paths['state_file']
PARAMS_FILE = _paths['params_file']
PARAMS_HISTORY = _paths['params_history']
DEVIATION_LOG = _paths['deviation_log']
SIGNAL_LOG = _paths['signal_log']
OPT_LOG_DIR = _paths['opt_log_dir']

# ===================== 全局配置（从 config/settings.yaml 加载） =====================
# 【修改】DEFAULT_CONFIG 从配置文件构建，不再硬编码
DEFAULT_CONFIG = {
    "data_source": _cfg.get('data_source', 'coze'),
    "coze_skill_path": SKILL_PATH_DP,
    "cache_dir": _paths['cache_dir'],
    "output_dir": _paths['output_dir'],
    "wechat_push": _cfg.get('wechat_push', {"enable": False, "sckey": ""}),
    "strategy": _cfg['strategy'],
    "trade_cost": _cfg['trade_cost'],
    "filter": _cfg['filter'],
    "risk": _cfg['risk'],
    "plot_enable": _cfg.get('plot_enable', True),
    "market_trend": _cfg.get('market_trend', {}),
}

# ===================== 数据结构 =====================
@dataclass
class Trade:
    code: str
    name: str = ""
    buy_date: object = None
    buy_price: float = 0.0
    shares: int = 0
    buy_value: float = 0.0
    atr_at_entry: float = 0.0
    stop_loss: float = 0.0
    chan_buy_type: str = ""
    add_count: int = 0
    sell_date: object = None
    sell_price: float = 0.0
    sell_value: float = 0.0
    pnl: float = 0.0
    pnl_pct: float = 0.0
    sell_reason: str = ""
    holding_days: int = 0
    partial_sold: bool = False
    remaining_shares: int = 0

@dataclass
class Position:
    code: str
    trade: Trade
    cost_total: float
    current_shares: int

# ===================== 工具函数 =====================
def wechat_send(title: str, content: str, sckey: str):
    if not sckey:
        return
    url = f"https://sctapi.ftqq.com/{sckey}.send"
    data = {"title": title, "desp": content}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        print(f"推送失败: {e}")

def load_cache(code: str, cache_dir: str) -> Optional[pd.DataFrame]:
    cache_file = Path(cache_dir) / f"{code}.csv"
    if not cache_file.exists():
        return None
    df = pd.read_csv(cache_file, parse_dates=["date"])
    df = df.sort_values("date").reset_index(drop=True)
    return df

def save_cache(code: str, df: pd.DataFrame, cache_dir: str):
    Path(cache_dir).mkdir(exist_ok=True)
    cache_file = Path(cache_dir) / f"{code}.csv"
    df.to_csv(cache_file, index=False, encoding="utf-8-sig")

# ===================== 行情数据 =====================
def fetch_kline_coze(code: str, count: int, skill_path: str) -> pd.DataFrame:
    cmd = [sys.executable, skill_path, "call", "kline",
           "--param", f"code={code}", "--param", "period=day",
           "--param", f"count={count}", "--param", "fq=qfq"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"Coze skill err: {result.stderr}")
    data = json.loads(result.stdout)
    if "data" not in data or not data["data"]:
        raise RuntimeError("No kline data")
    df = pd.DataFrame(data["data"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = pd.to_numeric(df[c])
    return df

def fetch_kline_akshare(code: str, count: int) -> pd.DataFrame:
    import akshare as ak
    if code.startswith("sh"):
        sym = code.replace("sh", "") + ".SH"
    elif code.startswith("sz"):
        sym = code.replace("sz", "") + ".SZ"
    else:
        sym = code
    df = ak.stock_zh_a_daily(symbol=sym, adjust="hfq")
    df = df.tail(count).copy()
    df.rename(columns={
        "date": "date", "open": "open", "high": "high",
        "low": "low", "close": "close", "volume": "volume"
    }, inplace=True)
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "close", "high", "low", "volume"]:
        df[c] = pd.to_numeric(df[c])
    return df

def get_kline(code: str, count: int, cfg: dict) -> pd.DataFrame:
    cache_df = load_cache(code, cfg["cache_dir"])
    today = pd.Timestamp.now().date()
    if cache_df is not None and cache_df["date"].max().date() == today:
        return cache_df
    # 缓存有足够数据时直接返回（支持回测场景，避免API调用）
    if cache_df is not None and len(cache_df) >= count * 0.8:
        return cache_df
    # 【parquet回退】API失败时从本地parquet读取（代码归一化：补sh/sz前缀）
    try:
        if cfg["data_source"] == "coze":
            df = fetch_kline_coze(code, count, cfg["coze_skill_path"])
        else:
            df = fetch_kline_akshare(code, count)
        save_cache(code, df, cfg["cache_dir"])
        return df
    except Exception:
        _normalized = code
        if not code.startswith(("sh", "sz")):
            _normalized = ("sh" if code.startswith("6") else "sz") + code
        _parquet_path = os.path.join(STOCK_DATA_DIR, f"{_normalized}.parquet")
        if os.path.exists(_parquet_path):
            df = pd.read_parquet(_parquet_path)
            df["date"] = pd.to_datetime(df["date"])
            return df
        raise

# ===================== 大盘趋势择时模块 =====================
def get_market_trend(index_code: str, cfg: dict, lookback: int = 60) -> dict:
    try:
        df = get_kline(index_code, count=lookback + 60, cfg=cfg)
        if len(df) < 60:
            return {'trend': 'unknown', 'position_scale': 0.5, 'description': '数据不足，半仓', 'ma20_slope': 0}
        df['ma20'] = df['close'].rolling(20).mean()
        df['ma60'] = df['close'].rolling(60).mean()
        last = df.iloc[-1]
        price, ma20, ma60 = last['close'], last['ma20'], last['ma60']
        ma20_5d_ago = df['ma20'].iloc[-6] if len(df) >= 6 else ma20
        ma20_slope = (ma20 - ma20_5d_ago) / ma20_5d_ago if ma20_5d_ago > 0 else 0
        
        if price > ma20 and ma20 > ma60 and ma20_slope > 0.002:
            return {'trend': 'strong', 'position_scale': 1.0, 'description': '强势多头', 'ma20_slope': ma20_slope}
        elif price > ma60 and ma20_slope > -0.003:
            return {'trend': 'moderate', 'position_scale': 0.8, 'description': '震荡偏多', 'ma20_slope': ma20_slope}
        elif price < ma60 and ma20_slope < -0.005:
            return {'trend': 'bear', 'position_scale': 0.0, 'description': '空头趋势', 'ma20_slope': ma20_slope}
        else:
            return {'trend': 'neutral', 'position_scale': 0.5, 'description': '中性', 'ma20_slope': ma20_slope}
    except Exception as e:
        print(f"大盘趋势获取失败: {e}")
        return {'trend': 'unknown', 'position_scale': 0.5, 'description': '异常，半仓', 'ma20_slope': 0}

def calc_market_trend_series(index_code: str, cfg: dict, bt_df: pd.DataFrame) -> pd.Series:
    try:
        df_index = get_kline(index_code, count=800, cfg=cfg)
        df_index['ma20'] = df_index['close'].rolling(20).mean()
        df_index['ma60'] = df_index['close'].rolling(60).mean()
        df_index['ma20_slope'] = (df_index['ma20'] - df_index['ma20'].shift(5)) / df_index['ma20'].shift(5)
        scales = []
        for dt in bt_df['date']:
            idx_data = df_index[df_index['date'] <= dt]
            if len(idx_data) == 0:
                scales.append(0.5)
                continue
            row = idx_data.iloc[-1]
            price, ma20, ma60 = row['close'], row['ma20'], row['ma60']
            slope = row['ma20_slope'] if not pd.isna(row['ma20_slope']) else 0
            
            if price > ma20 and ma20 > ma60 and slope > 0.002:
                scales.append(1.0)
            elif price > ma60 and slope > -0.003:
                scales.append(0.8)
            elif price < ma60 and slope < -0.005:
                scales.append(0.0)
            else:
                scales.append(0.5)
        scales_series = pd.Series(scales)
        scales_smooth = scales_series.rolling(3, min_periods=1).mean()
        return scales_smooth
    except Exception as e:
        print(f"大盘趋势序列计算失败: {e}")
        return pd.Series([1.0] * len(bt_df))

def calc_r2(closes, lookback=60):
    """计算R²（线性回归拟合度）"""
    n = min(lookback, len(closes))
    if n < 3:
        return 0.0
    x = np.arange(n)
    y = np.array(closes[-n:], dtype=float)
    r = np.corrcoef(x, y)[0, 1]
    return float(r ** 2) if not np.isnan(r) else 0.0

# ===================== 连续自适应仓位（含L1宏观渐变） =====================
def calc_continuous_position_scale(index_code: str, cfg: dict, bt_df: pd.DataFrame) -> pd.Series:
    try:
        df_index = get_kline(index_code, count=800, cfg=cfg)
        df_index['ma60'] = df_index['close'].rolling(60).mean()
        df_index['trend_strength'] = (df_index['close'] - df_index['ma60']) / df_index['ma60']
        df_index['ma60_slope'] = (df_index['ma60'] - df_index['ma60'].shift(5)) / df_index['ma60'].shift(5)
        df_index['total_trend'] = df_index['trend_strength'] + df_index['ma60_slope'] * 10
        
        min_scale = cfg['strategy'].get('min_position_scale', 0.0)
        max_scale = cfg['strategy'].get('max_position_scale', 1.00)
        sensitivity = cfg['strategy'].get('tanh_sensitivity', 4.0)
        hard_cutoff = cfg['strategy'].get('market_hard_cutoff', -0.03)

        scales = []
        for i, dt in enumerate(bt_df['date']):
            idx_data = df_index[df_index['date'] <= dt]
            if len(idx_data) == 0:
                scales.append(0.5)
                continue
            row = idx_data.iloc[-1]
            total_trend = row['total_trend'] if not pd.isna(row['total_trend']) else 0
            trend_strength = row['trend_strength'] if not pd.isna(row['trend_strength']) else 0

            # 【V5.3.4.1改进】硬截断改为最低0.1仓位，不再归零，避免完全错过个股机会
            # 【新增】个股强势豁免：个股比自身MA60高5%以上时，给予6成仓位
            if trend_strength < hard_cutoff:
                stock_row = bt_df.iloc[i]
                stock_ma60 = stock_row.get('ma60', np.nan)
                stock_close = stock_row.get('close', np.nan)
                if not pd.isna(stock_ma60) and stock_ma60 != 0:
                    stock_relative = stock_close / stock_ma60
                    if stock_relative > 1.05:
                        scales.append(0.6)  # 个股强势豁免，给6成仓位
                        continue
                scales.append(0.1)  # 保持最低仓位
                continue

            # L1：宏观R²和MA60斜率判断仓位上限
            df_index_close = idx_data['close'].values
            macro_r2 = calc_r2(df_index_close.tolist(), lookback=60)
            if len(idx_data) >= 65:
                ma60_now = idx_data['ma60'].iloc[-1]
                ma60_5ago = idx_data['ma60'].iloc[-6] if len(idx_data) >= 6 else ma60_now
                macro_slope = (ma60_now - ma60_5ago) / ma60_5ago if ma60_5ago != 0 else 0
            else:
                macro_slope = 0

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

            # 【优化3】R²保护：震荡市（macro_r2 < 0.15）强制压缩仓位上限至0.5
            if macro_r2 < 0.15:
                macro_scale_cap = min(macro_scale_cap, 0.5)

            raw_scale = 0.5 + 0.5 * np.tanh(total_trend * sensitivity)
            scale = min_scale + (max_scale - min_scale) * raw_scale
            scale = scale * macro_scale_cap
            scales.append(scale)
        
        scales_series = pd.Series(scales)
        scales_smooth = scales_series.rolling(3, min_periods=1).mean()
        return scales_smooth
    except Exception as e:
        print(f"连续仓位系数计算失败: {e}")
        return pd.Series([0.5] * len(bt_df))

def calc_market_slope_series(index_code: str, cfg: dict, bt_df: pd.DataFrame) -> pd.Series:
    try:
        df_index = get_kline(index_code, count=800, cfg=cfg)
        df_index['ma20'] = df_index['close'].rolling(20).mean()
        df_index['ma20_slope'] = (df_index['ma20'] - df_index['ma20'].shift(5)) / df_index['ma20'].shift(5)
        
        slopes = []
        for dt in bt_df['date']:
            idx_data = df_index[df_index['date'] <= dt]
            if len(idx_data) == 0:
                slopes.append(0.0)
            else:
                row = idx_data.iloc[-1]
                slopes.append(row['ma20_slope'] if not pd.isna(row['ma20_slope']) else 0.0)
        return pd.Series(slopes)
    except Exception as e:
        print(f"MA20斜率计算失败: {e}")
        return pd.Series([0.0] * len(bt_df))

# ===================== 自适应参数计算函数 =====================
def calc_adaptive_params(df, idx, st_cfg, market_slope=0, volatility_index=1.0):
    if idx < 30:
        return {
            'risk_pct': st_cfg.get('base_risk_pct', 0.10),
            'stop_multiplier': st_cfg.get('stop_multiplier_base', st_cfg.get('base_atr_multiplier', 2.0)),
            'chan_threshold': st_cfg.get('base_chan_threshold', 0.70),
            'add_threshold': st_cfg.get('add_threshold_base', 0.06),
            'take_profit_threshold': st_cfg.get('take_profit_base', 0.40),
            'high_vol_risk_reduce': 1.0,
            'r2_20': 0.0
        }
    
    close = df['close'].iloc[idx]
    ma60 = df['ma60'].iloc[idx] if not pd.isna(df['ma60'].iloc[idx]) else close
    trend_strength = (close - ma60) / ma60 if ma60 > 0 else 0
    total_trend = trend_strength + market_slope * 10
    
    high_vol_risk_reduce = 1.0
    if volatility_index > 1.5:
        high_vol_risk_reduce = 0.625
    elif volatility_index > 1.2:
        high_vol_risk_reduce = 0.75
    
    _stop_base = st_cfg.get('stop_multiplier_base', 2.0)
    if volatility_index > 1.5:
        stop_multiplier = _stop_base * 1.5
    elif volatility_index > 1.2:
        stop_multiplier = _stop_base * 1.25
    elif volatility_index > 0.8:
        stop_multiplier = _stop_base
    else:
        stop_multiplier = _stop_base * 0.75
    
    if total_trend > 0.20:
        chan_threshold = 0.85
    elif total_trend > 0.10:
        chan_threshold = 0.75
    elif total_trend > -0.05:
        chan_threshold = 0.65
    else:
        chan_threshold = 0.55
    
    _tp_base = st_cfg.get('take_profit_base', 0.40)
    if total_trend > 0.25:
        take_profit_threshold = _tp_base * 1.875
    elif total_trend > 0.15:
        take_profit_threshold = _tp_base * 1.5
    elif total_trend > 0.05:
        take_profit_threshold = _tp_base * 1.25
    else:
        take_profit_threshold = _tp_base
    
    if total_trend > 0.03:
        risk_pct = min(0.10, st_cfg.get('base_risk_pct', 0.10) * 1.6) * high_vol_risk_reduce
    elif total_trend > 0.01:
        risk_pct = min(0.08, st_cfg.get('base_risk_pct', 0.10) * 1.3) * high_vol_risk_reduce
    elif total_trend > -0.01:
        risk_pct = st_cfg.get('base_risk_pct', 0.10) * high_vol_risk_reduce
    else:
        risk_pct = max(0.03, st_cfg.get('base_risk_pct', 0.10) * 0.5) * high_vol_risk_reduce
    
    _add_base = st_cfg.get('add_threshold_base', 0.06)
    if total_trend > 0.15:
        add_threshold = _add_base * 0.67
    elif total_trend > 0.05:
        add_threshold = _add_base
    else:
        add_threshold = _add_base * 1.33
    
    # L4：计算20日R²用于止盈调整
    closes_recent = df['close'].iloc[max(0, idx-20):idx+1].values
    r2_20 = calc_r2(closes_recent.tolist(), lookback=20)
    
    return {
        'risk_pct': risk_pct,
        'stop_multiplier': stop_multiplier,
        'chan_threshold': chan_threshold,
        'add_threshold': add_threshold,
        'take_profit_threshold': take_profit_threshold,
        'high_vol_risk_reduce': high_vol_risk_reduce,
        'r2_20': r2_20
    }

# ===================== 缠论核心 =====================
def merge_kline_include(df: pd.DataFrame) -> pd.DataFrame:
    bars = df[["high", "low"]].copy().reset_index(drop=True)
    i = 1
    while i < len(bars) - 1:
        h0, l0 = bars.loc[i-1, "high"], bars.loc[i-1, "low"]
        h1, l1 = bars.loc[i, "high"], bars.loc[i, "low"]
        if h0 >= h1 and l0 <= l1:
            bars.loc[i, "high"] = h0
            bars.loc[i, "low"] = l0
            bars = bars.drop(i-1).reset_index(drop=True)
            i = max(1, i - 1)
        elif h1 >= h0 and l1 <= l0:
            bars.loc[i-1, "high"] = h1
            bars.loc[i-1, "low"] = l1
            bars = bars.drop(i).reset_index(drop=True)
            i = max(1, i - 1)
        else:
            i += 1
    return bars

def find_pivots_enhanced(df: pd.DataFrame) -> Tuple[List[int], List[int]]:
    bars = merge_kline_include(df)
    tops, bottoms = [], []
    # 【v5.3.4.1修复】原代码 range(1, len(bars)-1) 引用 i+1（未来K线）确认分型，是未来函数
    # 改为 range(2, len(bars))，只用 i/i-1/i-2 三根K线判断，不引用任何未来数据
    for i in range(2, len(bars)):
        h, l = bars.loc[i, "high"], bars.loc[i, "low"]
        h1, h2 = bars.loc[i-1, "high"], bars.loc[i-2, "high"]  # 改：i+1 → i-2
        l1, l2 = bars.loc[i-1, "low"], bars.loc[i-2, "low"]    # 改：i+1 → i-2
        if h > h1 and h > h2:
            tops.append(i)
        if l < l1 and l < l2:
            bottoms.append(i)
    return tops, bottoms

def identify_strokes_std(tops, bottoms, bars):
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
    n = len(df)
    # 【v5.3.4.1修复】只往回看，不往前看，消除未来函数
    # 原代码 e = pivot_idx + w + 1 会用到pivot_win+2=7根未来K线
    # 信号在 i2+5 触发，改为只用到 pivot_idx 本身，确保零未来函数
    s, e = max(0, pivot_idx - w), min(n, pivot_idx + 1)
    seg = df.iloc[s:e]["macd_hist"]
    return abs(seg[seg < 0].sum()) if is_down else abs(seg[seg > 0].sum())

def detect_chan_signals_optimized(df, pivot_win=5, chan_threshold=0.70):
    n = len(df)
    tops, bottoms = find_pivots_enhanced(df)
    df["chan_buy"] = False
    df["chan_buy_type"] = ""
    df["chan_sell"] = False

    for k in range(1, len(bottoms)):
        i1, i2 = bottoms[k-1], bottoms[k]
        if df.loc[i2, "low"] >= df.loc[i1, "low"] * 0.97:
            continue
        a1 = calc_macd_area_section(df, i1, pivot_win+2, True)
        a2 = calc_macd_area_section(df, i2, pivot_win+2, True)
        if a2 < a1 * chan_threshold:
            cfm = min(i2 + pivot_win, n-1)
            df.loc[cfm, "chan_buy"] = True
            df.loc[cfm, "chan_buy_type"] = "一买(底背离)"

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

    bars = df[["high", "low"]].reset_index(drop=True)
    strokes = identify_strokes_std(tops, bottoms, bars)
    if len(strokes) >= 3:
        for sidx in range(len(strokes)-2):
            s1, s2, s3 = strokes[sidx:sidx+3]
            ranges = []
            for si, ei, _ in [s1, s2, s3]:
                seg = bars.iloc[si:ei+1]
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

    for k in range(1, len(tops)):
        i1, i2 = tops[k-1], tops[k]
        if df.loc[i2, "high"] <= df.loc[i1, "high"]:
            continue
        a1 = calc_macd_area_section(df, i1, pivot_win+2, False)
        a2 = calc_macd_area_section(df, i2, pivot_win+2, False)
        if a2 < a1 * 0.8:
            cfm = min(i2 + pivot_win, n-1)
            df.loc[cfm, "chan_sell"] = True
    return df

# ===================== 指标计算 =====================
def calc_indicators_vec(df, st_cfg):
    dc_p, exit_p, atr_p = st_cfg["dc_period"], st_cfg["exit_period"], st_cfg["atr_period"]
    vol_win = st_cfg["vol_window"]

    # 【v5.3.4.1动态通道】计算多组海龟通道，运行时按波动率状态选择
    # 通道周期：base-5 / base / base+5
    atr_pct = (df["close"].diff().abs() / df["close"].shift(1)).rolling(60).mean()
    atr_pct_ma = atr_pct.rolling(120).mean()
    vol_ratio = atr_pct / atr_pct_ma

    ch_base = dc_p
    ch_low = max(5, ch_base - 5)
    ch_high = ch_base + 5
    df[f"dc_high_{ch_low}"] = df["high"].rolling(ch_low).max().shift(1)
    df[f"dc_high_{ch_base}"] = df["high"].rolling(ch_base).max().shift(1)
    df[f"dc_high_{ch_high}"] = df["high"].rolling(ch_high).max().shift(1)
    df[f"dc_low_{ch_low}"] = df["low"].rolling(ch_low).min().shift(1)
    df[f"dc_low_{ch_base}"] = df["low"].rolling(ch_base).min().shift(1)
    df[f"dc_low_{ch_high}"] = df["low"].rolling(ch_high).min().shift(1)
    
    ex_base = exit_p
    ex_low = max(3, ex_base - 2)
    ex_high = ex_base + 2
    df[f"exit_low_{ex_low}"] = df["low"].rolling(ex_low).min().shift(1)
    df[f"exit_low_{ex_base}"] = df["low"].rolling(ex_base).min().shift(1)
    df[f"exit_low_{ex_high}"] = df["low"].rolling(ex_high).min().shift(1)

    # 按波动率状态动态选择通道（阈值从st_cfg获取）
    vr_high = st_cfg.get("vol_ratio_high", 1.3)
    vr_low = st_cfg.get("vol_ratio_low", 0.7)
    dc_high = pd.Series(np.nan, index=df.index)
    dc_low = pd.Series(np.nan, index=df.index)
    exit_low = pd.Series(np.nan, index=df.index)
    high_vol = vol_ratio > vr_high
    low_vol = vol_ratio < vr_low
    dc_high[high_vol] = df.loc[high_vol, f"dc_high_{ch_high}"]
    dc_low[high_vol] = df.loc[high_vol, f"dc_low_{ch_high}"]
    dc_high[low_vol] = df.loc[low_vol, f"dc_high_{ch_low}"]
    dc_low[low_vol] = df.loc[low_vol, f"dc_low_{ch_low}"]
    dc_high[~high_vol & ~low_vol] = df.loc[~high_vol & ~low_vol, f"dc_high_{ch_base}"]
    dc_low[~high_vol & ~low_vol] = df.loc[~high_vol & ~low_vol, f"dc_low_{ch_base}"]
    # 退出线：强趋势让利润跑, 弱趋势锁利润
    trend_str = (df["close"] - df["close"].rolling(60).mean()) / df["close"].rolling(60).mean()
    ts_high = st_cfg.get("trend_str_high", 0.15)
    ts_low = st_cfg.get("trend_str_low", -0.05)
    strong_trend = trend_str > ts_high
    weak_trend = trend_str < ts_low
    exit_low[strong_trend] = df.loc[strong_trend, f"exit_low_{ex_high}"]
    exit_low[weak_trend] = df.loc[weak_trend, f"exit_low_{ex_low}"]
    exit_low[~strong_trend & ~weak_trend] = df.loc[~strong_trend & ~weak_trend, f"exit_low_{ex_base}"]

    df["dc_high"] = dc_high
    df["dc_low"] = dc_low
    df["exit_low"] = exit_low
    df["prev_close"] = df["close"].shift(1)
    tr1 = df["high"] - df["low"]
    tr2 = abs(df["high"] - df["prev_close"])
    tr3 = abs(df["low"] - df["prev_close"])
    df["tr"] = np.maximum(np.maximum(tr1, tr2), tr3)
    df["atr"] = df["tr"].rolling(atr_p).mean()
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["dif"] = ema12 - ema26
    df["dea"] = df["dif"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = 2 * (df["dif"] - df["dea"])
    df["vol_ma"] = df["volume"].rolling(20).mean()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_q75"] = df["volume"].rolling(vol_win).quantile(0.75)
    df["vol_effective"] = df["volume"] > df["vol_q75"]
    df["ma20"] = df["close"].rolling(20).mean()
    df["ma60"] = df["close"].rolling(60).mean()
    df["trend_strength"] = (df["close"] - df["ma60"]) / df["ma60"]
    
    df["ma20_slope"] = (df["ma20"] - df["ma20"].shift(5)) / df["ma20"].shift(5)
    df["money_strength"] = df["macd_hist"]
    df["money_slope"] = df["money_strength"] - df["money_strength"].shift(3)
    
    df["test_day"] = (df["close"] / df["close"].shift(1) - 1) >= st_cfg.get("test_gain_threshold", 0.06)
    df["test_occurred"] = df["test_day"].rolling(st_cfg.get("test_period", 30)).sum() > 0
    
    df['volatility_ratio'] = df['atr'] / df['close']
    df['volatility_ma'] = df['volatility_ratio'].rolling(120).mean()
    df['volatility_percentile'] = df['volatility_ratio'].rolling(120).rank(pct=True)
    df['volatility_percentile'] = df['volatility_percentile'].fillna(0.5)
    df['ma60_direction'] = df['ma60'] > df['ma60'].shift(st_cfg.get('stock_trend_lookback', 20))

    # 【新增】风险值（超买超卖振荡器，借鉴中和应泰强龙风控）
    # 风险值 = EMA(100 × (C - LLV(L,34)) / (HHV(H,34) - LLV(L,34)), 3)
    # 0=底部超卖, 100=顶部超买
    llv_34 = df['low'].rolling(34).min()
    hhv_34 = df['high'].rolling(34).max()
    range_34 = hhv_34 - llv_34
    range_34 = range_34.replace(0, 1)  # 防止除零
    raw_risk = 100 * (df['close'] - llv_34) / range_34
    raw_risk = raw_risk.clip(0, 100)
    df['risk_value'] = raw_risk.ewm(span=3, adjust=False).mean()

    # 【新增】堆量比（借鉴中和应泰强龙战法选股）
    # 堆量比 = MAX(MA(V,10)/LLV(MA(V,20),120), MA(V,15)/LLV(MA(V,40),300))
    # 衡量中期量能扩张程度
    ma_vol_10 = df['volume'].rolling(10).mean()
    ma_vol_20 = df['volume'].rolling(20).mean()
    ma_vol_15 = df['volume'].rolling(15).mean()
    ma_vol_40 = df['volume'].rolling(40).mean()
    llv_mavol20_120 = ma_vol_20.rolling(120).min()
    llv_mavol40_300 = ma_vol_40.rolling(300).min()
    llv_mavol20_120 = llv_mavol20_120.replace(0, 1)
    llv_mavol40_300 = llv_mavol40_300.replace(0, 1)
    vol_ratio_1 = ma_vol_10 / llv_mavol20_120
    vol_ratio_2 = ma_vol_15 / llv_mavol40_300
    df['vol_stack_ratio'] = np.maximum(vol_ratio_1.fillna(0), vol_ratio_2.fillna(0))

    # 【新增】量比（换手率代理指标，借鉴中和应泰强龙换手）
    # 量比 = VOL / MA(VOL, 5)，衡量当日成交量相对近5日均值的倍数
    ma_vol_5 = df['volume'].rolling(5).mean()
    ma_vol_5 = ma_vol_5.replace(0, 1)
    df['vol_turnover'] = df['volume'] / ma_vol_5

    # 【新增】周线战略过滤（借鉴中和应泰三周期共振法则）
    # 从日线合成周线MA20方向：1=上升, 0=走平, -1=下降
    # 取每5根日K的收盘价均值作为周线收盘
    weekly_close = df['close'].rolling(5).mean()
    weekly_ma20 = weekly_close.rolling(100).mean()  # 20周≈100日
    df['weekly_trend'] = 0  # 默认走平
    df.loc[weekly_close > weekly_ma20 * 1.005, 'weekly_trend'] = 1   # 周线上升
    df.loc[weekly_close < weekly_ma20 * 0.995, 'weekly_trend'] = -1  # 周线下降

    # 【新增】早鸟突破：10日唐奇安上轨（主升浪初期放量突破捕捉）
    # vol_ma5 已在上方计算，此处只需新增 dc_high_aggressive
    df["dc_high_aggressive"] = df["high"].rolling(10).max().shift(1)

    return df

# ===================== 【V5.3.4新增】估值分位计算 =====================
def calc_valuation_percentile(df: pd.DataFrame, lookback: int = 250) -> Dict[str, float]:
    """
    计算当前价格在历史区间内的估值分位（用价格替代PE/PB，因为数据源无财务数据）
    在真实场景中，应替换为PE/PB历史分位
    """
    if len(df) < lookback:
        return {'price_percentile': 0.5, 'is_extreme': False}
    
    recent = df.tail(lookback)
    current_price = df['close'].iloc[-1]
    min_price = recent['close'].min()
    max_price = recent['close'].max()
    
    if max_price == min_price:
        percentile = 0.5
    else:
        percentile = (current_price - min_price) / (max_price - min_price)
    
    is_extreme = percentile < 0.10
    
    return {
        'price_percentile': percentile,
        'is_extreme': is_extreme,
        'min_price': min_price,
        'max_price': max_price
    }

# ===================== 整股预评分（含估值豁免） =====================
def calc_stock_quality_score(df: pd.DataFrame, st_cfg: dict) -> float:
    """
    计算整只股票的综合趋势质量评分
    【V5.3.4新增】估值极端低时给予豁免加分
    """
    if len(df) < 30:
        return 0.0

    recent = df.copy()
    recent['trend_deviation'] = recent['close'] / recent['ma60'] - 1
    recent['trend_stability'] = 1 - recent['trend_deviation'].rolling(20).std() * 10
    recent['trend_stability'] = recent['trend_stability'].clip(0, 1)
    recent['trend_strength_score'] = (recent['close'] / recent['ma60'] - 1).clip(-1, 1) * 0.5 + 0.5
    recent['above_ma20_ratio'] = (recent['close'] > recent['ma20']).rolling(20).mean()

    base_score = (
        recent['trend_stability'].mean() * 0.30 +
        recent['trend_strength_score'].mean() * 0.30 +
        recent['above_ma20_ratio'].mean() * 0.20
    ) * 100

    # R²
    closes = recent['close'].values
    n_pts = len(closes)
    x = np.arange(n_pts)
    x_mean = x.mean()
    y_mean = closes.mean()
    ss_xy = np.sum((x - x_mean) * (closes - y_mean))
    ss_xx = np.sum((x - x_mean) ** 2)
    ss_yy = np.sum((closes - y_mean) ** 2)
    r_squared = (ss_xy ** 2) / (ss_xx * ss_yy) if ss_xx > 0 and ss_yy > 0 else 0

    # 历史突破模拟
    sim_trades = []
    in_position = False
    entry_price = 0
    entry_idx = 0
    exit_low_col = 'dc_low_10' if 'dc_low_10' in recent.columns else None
    if exit_low_col is None and 'exit_low' in recent.columns:
        exit_low_col = 'exit_low'
    for i in range(20, len(recent)):
        if not in_position:
            if 'dc_high' in recent.columns and not pd.isna(recent['dc_high'].iloc[i]):
                if recent['close'].iloc[i] > recent['dc_high'].iloc[i]:
                    in_position = True
                    entry_price = recent['close'].iloc[i]
                    entry_idx = i
        else:
            should_exit = False
            if exit_low_col and not pd.isna(recent[exit_low_col].iloc[i]):
                if recent['close'].iloc[i] < recent[exit_low_col].iloc[i]:
                    should_exit = True
            if i - entry_idx >= 30:
                should_exit = True
            if should_exit:
                exit_price = recent['close'].iloc[i]
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

    # 抄底先锋（X_7主力吸货强度）
    x1 = recent['low'].shift(1)
    abs_diff = (recent['low'] - x1).abs()
    pos_diff = (recent['low'] - x1).clip(lower=0)
    sma_abs = abs_diff.ewm(alpha=1/3, adjust=False).mean()
    sma_pos = pos_diff.ewm(alpha=1/3, adjust=False).mean()
    x2 = pd.Series(np.where(sma_pos > 1e-10, sma_abs / sma_pos * 100, 9999.0), index=recent.index)
    x3 = (x2 * 10).ewm(alpha=2/4, adjust=False).mean()
    x4 = recent['low'].rolling(38).min()
    x5 = x3.rolling(38).max()
    accum_raw = pd.Series(0.0, index=recent.index)
    new_low_mask = recent['low'] <= x4
    accum_raw[new_low_mask] = (x3[new_low_mask] + x5[new_low_mask] * 2) / 2
    x7 = accum_raw.ewm(alpha=2/4, adjust=False).mean() / 618

    dip_signal = x7 >= 1
    dip_onset = dip_signal & ~dip_signal.shift(1, fill_value=False)

    dip_count = 0
    for i in range(len(recent)):
        if dip_onset.iloc[i]:
            future_after = recent.iloc[i + 1:min(i + 21, len(recent))]
            if len(future_after) > 0 and (future_after['close'] > future_after['dc_high']).any():
                dip_count += 1

    dip_score = min(dip_count * 3.0, 15.0)
    score += dip_score

    # 【V5.3.4新增】估值极端低豁免加分
    # 【修改】优先使用真实PE/PB分位（df中valuation_percentile列），无则回退到价格分位
    if "valuation_percentile" in recent.columns:
        val_pct = recent["valuation_percentile"].iloc[-1]
        is_extreme = (val_pct is not None) and (not pd.isna(val_pct)) and (val_pct < 0.10)
        val_label = f"PE/PB分位{val_pct:.1%}" if not pd.isna(val_pct) else "PE/PB分位N/A"
    else:
        valuation = calc_valuation_percentile(recent)
        is_extreme = valuation['is_extreme']
        val_label = f"价格分位{valuation['price_percentile']:.1%}"
    if st_cfg.get('extreme_value_exemption', True) and is_extreme:
        exemption_bonus = 15.0
        score += exemption_bonus
        print(f"   【估值豁免】{val_label} < 10%，额外+{exemption_bonus}分")

    # 筹码集中度辅助
    if 'ma5' not in recent.columns:
        recent['ma5'] = recent['close'].rolling(5).mean()
    deviation = abs(recent['close'] / recent['ma60'] - 1).mean()
    if deviation < 0.05 and recent['ma5'].iloc[-1] > recent['ma20'].iloc[-1] > recent['ma60'].iloc[-1]:
        score += 5

    print(f"   [V5.3.4评分] 基础:{base_score:.1f} | R²:{r_squared:.3f} | 模拟{len(sim_trades)}笔胜率{sim_win_rate*100:.0f}% | 主力吸货信号{dip_count}(+{dip_score:.1f}) | 最终:{score:.1f}")
    return round(score, 1)

# ===================== 信号生成（含估值极端抄底） =====================
# 【v5.3.4.1重构】gen_signal_enhanced 改为向量化计算，消除 for i in range(n) 逐行循环
# 原代码两段 O(n)~O(n*look) 循环 → 全部用 pandas/numpy 向量运算，逻辑完全等价
def gen_signal_enhanced(df, st_cfg, adaptive_params):
    n = len(df)
    df["buy_signal"] = False
    df["sell_signal"] = False
    df["chan_recent_type"] = ""
    df["entry_score"] = 0.0
    look = st_cfg["chan_lookback"]

    # ---------- 向量化 chan_recent ----------
    # 改：原 for i 循环 + 内层 for j 展开 → rolling sum
    chan_recent = df["chan_buy"].rolling(look, min_periods=1).sum() > 0
    # 改：chan_label 用 ffill(limit) 替代逐行赋值（仅影响展示字段，不影响交易逻辑）
    chan_label = df["chan_buy_type"].replace("", np.nan).ffill(limit=max(1, look - 1)).fillna("")
    df["chan_recent_type"] = chan_label

    # ---------- 参数 ----------
    min_score = st_cfg.get('min_entry_score', 1.7)
    max_score = st_cfg.get('max_entry_score', 3.5)
    stock_trend_filter = st_cfg.get('stock_trend_filter', True)
    use_trend = st_cfg.get('use_trend_filter', True)
    use_money = st_cfg.get('use_money_filter', True)
    use_test_pullback = st_cfg.get('use_test_pullback', True)
    pullback_threshold = st_cfg.get('test_pullback_threshold', 0.02)
    vol_ratio = st_cfg.get('test_pullback_vol_ratio', 0.7)
    extreme_exemption = st_cfg.get('extreme_value_exemption', True)
    extreme_position_scale = st_cfg.get('extreme_value_position_scale', 0.3)

    # ---------- 向量化条件计算 ----------
    # 改：原逐行 df.loc[i, ...] → 全列布尔运算
    breakout = (~df["dc_high"].isna()) & (df["close"] > df["dc_high"])
    vol_ok = df["vol_effective"]
    chan_ok = chan_recent

    if use_trend:
        ma20_slope = df["ma20_slope"].fillna(0)
        price_above_ma60 = df["close"] > df["ma60"]
        trend_ok = (ma20_slope > 0.001) & price_above_ma60
    else:
        trend_ok = pd.Series(False, index=df.index)

    if use_money:
        money_strength = df["money_strength"].fillna(0)
        money_slope = df["money_slope"].fillna(0)
        money_ok = (money_strength > 0) & (money_slope > 0)
    else:
        money_ok = pd.Series(False, index=df.index)

    # ---------- 向量化评分 ----------
    # 改：原逐行 if/score+= → 向量加法
    score = pd.Series(0.0, index=df.index)
    score += breakout.astype(float) * 1
    score += vol_ok.astype(float) * 1
    score += chan_ok.astype(float) * 2
    score += trend_ok.astype(float) * 0.5
    score += money_ok.astype(float) * 0.5

    # 风险值
    risk_val = df["risk_value"].fillna(50) if "risk_value" in df.columns else pd.Series(50.0, index=df.index)
    score += (risk_val < 33).astype(float) * 0.5

    # 堆量比（按市值分档动态阈值）
    vol_stack = df["vol_stack_ratio"].fillna(1.0) if "vol_stack_ratio" in df.columns else pd.Series(1.0, index=df.index)
    _mc_est = st_cfg.get('mc_est', 150)
    if _mc_est >= 300:
        _vs_threshold = st_cfg.get('vol_stack_threshold_large', 3.0)
    elif _mc_est >= 100:
        _vs_threshold = st_cfg.get('vol_stack_threshold_mid', 5.0)
    else:
        _vs_threshold = st_cfg.get('vol_stack_threshold_small', 8.0)
    _vs_penalty = _vs_threshold * 0.3
    score += (vol_stack > _vs_threshold).astype(float) * 1.0
    score -= (vol_stack < _vs_penalty).astype(float) * 0.5

    # 周线趋势
    weekly_trend = df["weekly_trend"].fillna(0) if "weekly_trend" in df.columns else pd.Series(0.0, index=df.index)
    score -= (weekly_trend < 0).astype(float) * 1.0
    score += (weekly_trend > 0).astype(float) * 0.5

    # 【新增】早鸟突破：主升浪初期放量突破10日高点，不要求close>ma60
    dc_high_agg = df["dc_high_aggressive"] if "dc_high_aggressive" in df.columns else pd.Series(np.inf, index=df.index)
    aggressive_break = (df["close"] > dc_high_agg) & (df["volume"] > df["vol_ma5"].fillna(0) * 1.8)
    score += aggressive_break.astype(float) * 1.5

    # ---------- 估值极端低 ----------
    # 【修改】优先使用真实PE/PB分位（valuation_percentile列），无则回退到价格分位
    if "valuation_percentile" in df.columns:
        # 真实PE/PB分位（由run_multi_backtest通过data.valuation注入）
        val_pct = df["valuation_percentile"]
        is_extreme_val = (val_pct < 0.10) & (~val_pct.isna())
    else:
        # 回退：价格分位（原逻辑，用价格rolling min/max近似）
        rolling_min = df["close"].rolling(250, min_periods=250).min()
        rolling_max = df["close"].rolling(250, min_periods=250).max()
        price_range = rolling_max - rolling_min
        price_percentile = pd.Series(0.5, index=df.index)  # < 250 根时默认 0.5
        valid_range = price_range > 0
        price_percentile[valid_range] = (df.loc[valid_range, "close"] - rolling_min[valid_range]) / price_range[valid_range]
        is_extreme_val = (price_percentile < 0.10) & (~rolling_min.isna())
    is_extreme_value = extreme_exemption & is_extreme_val

    # ---------- 仓位 & 入场门槛 ----------
    pos_scale = df["position_scale"].fillna(0.5) if "position_scale" in df.columns else pd.Series(0.5, index=df.index)
    # 改：原逐行 if is_extreme → np.where
    pos_scale_adj = pd.Series(
        np.where(is_extreme_value.values, np.minimum(pos_scale.values, extreme_position_scale), pos_scale.values),
        index=df.index
    )
    # 【优化1】动态risk_value_ban：70 + trend_strength * 50，范围65~95，趋势强时放宽
    _ts_vals = df["trend_strength"].fillna(0).values if "trend_strength" in df.columns else np.zeros(len(df))
    risk_value_ban = np.clip(72 + _ts_vals * 40, 72, 92)
    high_risk = risk_val.values > risk_value_ban
    pos_scale_adj = pd.Series(
        np.where(high_risk, pos_scale_adj.values * 0.6, pos_scale_adj.values),
        index=df.index
    )
    adjusted_min_score = np.where(is_extreme_value.values, max(0.5, min_score - 0.5), min_score)
    required_score = adjusted_min_score + (max_score - adjusted_min_score) * (1.0 - pos_scale_adj.values) * 0.6
    required_score = np.maximum(adjusted_min_score, np.minimum(max_score, required_score))

    # ---------- stock_trend_ok ----------
    stock_trend_ok = pd.Series(True, index=df.index)
    if stock_trend_filter and "ma60_direction" in df.columns:
        stock_trend_ok = df["ma60_direction"] | (pos_scale_adj > 0.85)
    # 【修改】删除 risk_val 禁止买入逻辑，改为上方仓位缓冲

    # ---------- 买入信号 ----------
    buy_signal = (score.values >= required_score) & stock_trend_ok.values

    # ---------- chan_buy_type 赋值 ----------
    # 改：原逐行 if/elif → np.where 嵌套
    existing_types = df["chan_buy_type"].values
    new_types = np.where(
        buy_signal & is_extreme_value.values, "估值极端抄底",
        np.where(
            buy_signal & chan_ok.values & breakout.values, "缠论+突破",
            np.where(
                buy_signal & chan_ok.values, "缠论买点",
                np.where(
                    buy_signal & breakout.values, "纯突破",
                    np.where(buy_signal, "综合评分", existing_types)
                )
            )
        )
    )

    # ---------- 试盘回踩 ----------
    if use_test_pullback:
        test_occurred = df["test_occurred"] if "test_occurred" in df.columns else pd.Series(False, index=df.index)
        ma20_filled = df["ma20"].fillna(df["close"])
        pullback_cond = (df["close"] / ma20_filled - 1).abs() < pullback_threshold
        vol_ma5 = df["vol_ma5"].fillna(0)
        vol_cond = df["volume"] < vol_ma5 * vol_ratio
        test_pullback_sig = test_occurred & pullback_cond & vol_cond & (trend_ok | money_ok) & (pos_scale_adj > 0.5)
        # 仅在尚未标记买入的行补充
        new_from_pullback = test_pullback_sig & (~pd.Series(buy_signal, index=df.index))
        buy_signal = buy_signal | new_from_pullback.values
        new_types = np.where(new_from_pullback.values, "试盘回踩", new_types)

    # 【新增】早鸟突破独立触发路径（不要求close>ma60，仅保留风控ban）
    # 当 aggressive_break 为 True 但未通过正常评分路径时，独立触发买入
    aggressive_buy = aggressive_break & (risk_val <= risk_value_ban) & (~pd.Series(buy_signal, index=df.index))
    buy_signal = buy_signal | aggressive_buy.values
    new_types = np.where(aggressive_buy.values, "早鸟突破", new_types)
    # 同步更新 chan_recent_type（交易记录使用此字段显示买入类型）
    df["chan_recent_type"] = np.where(aggressive_buy.values, "早鸟突破", df["chan_recent_type"].values)

    df["buy_signal"] = buy_signal
    df["chan_buy_type"] = new_types
    df["entry_score"] = score.values

    # ---------- 卖出信号 ----------
    exit_sig = (~df["exit_low"].isna()) & (df["close"] < df["exit_low"])
    df["sell_signal"] = exit_sig | df["chan_sell"]

    return df

# ===================== 成本计算、市值估计 =====================
def calc_trade_cost(price, shares, side, trade_cfg, mc):
    return _shared_calc_trade_cost(price, shares, side, trade_cfg, mc)

def estimate_market_cap(price: float) -> float:
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

# ===================== 回测引擎 =====================
# 【变更3】行业分类映射（用于行业集中度检查）
# 申万一级行业近似映射；回测中无需联网，未在表中的股票返回"其他"
_INDUSTRY_MAP = {
    # 食品饮料
    "sh600519": "食品饮料", "sz000858": "食品饮料", "sh600809": "食品饮料",
    "sz000568": "食品饮料", "sh603369": "食品饮料", "sh600779": "食品饮料",
    "sz000596": "食品饮料", "sh603589": "食品饮料", "sh603198": "食品饮料",
    "sh600132": "食品饮料", "sh600600": "食品饮料", "sz000729": "食品饮料",
    "sh603288": "食品饮料", "sz000895": "食品饮料", "sh603899": "食品饮料",
    "sz300999": "食品饮料", "sh605499": "食品饮料", "sh603027": "食品饮料",
    # 银行
    "sh600036": "银行", "sh601398": "银行", "sh601288": "银行", "sh601988": "银行",
    "sh601939": "银行", "sh601328": "银行", "sh600000": "银行", "sh601166": "银行",
    "sh600016": "银行", "sh601818": "银行", "sz000001": "银行", "sh600015": "银行",
    "sh601169": "银行", "sh601009": "银行", "sh601229": "银行", "sz002142": "银行",
    "sh601838": "银行", "sh601916": "银行", "sh600926": "银行", "sh601077": "银行",
    # 非银金融
    "sh601318": "非银金融", "sh601628": "非银金融", "sh601601": "非银金融",
    "sh601336": "非银金融", "sh601688": "非银金融", "sh600030": "非银金融",
    "sz000776": "非银金融", "sh601211": "非银金融", "sh600999": "非银金融",
    "sh601878": "非银金融", "sh600837": "非银金融", "sh601788": "非银金融",
    "sh601066": "非银金融",
    # 汽车
    "sz002594": "汽车", "sh600104": "汽车", "sz000625": "汽车", "sh601238": "汽车",
    "sh601633": "汽车", "sz000800": "汽车", "sh600006": "汽车", "sz000550": "汽车",
    "sh601127": "汽车", "sz002460": "汽车", "sz300750": "汽车",
    # 家用电器
    "sz000651": "家用电器", "sz000333": "家用电器", "sh600690": "家用电器",
    "sz002032": "家用电器", "sz002508": "家用电器", "sh603868": "家用电器",
    "sz002242": "家用电器", "sz002705": "家用电器", "sh603551": "家用电器",
    "sz002677": "家用电器", "sh603486": "家用电器", "sh600854": "家用电器",
}

def _get_industry(code):
    return _INDUSTRY_MAP.get(code, "其他")


def run_multi_backtest(code_list, cfg, bt_start, bt_end):
    st_cfg = cfg["strategy"]
    risk_cfg = cfg["risk"]
    trade_cfg = cfg["trade_cost"]
    filter_cfg = cfg["filter"]
    trend_cfg = cfg.get("market_trend", {"enable": False})
    market_slope_threshold = st_cfg.get("market_slope_threshold", 0.001)
    # 【变更3】行业集中度上限（单一行业总市值占账户净值比例）
    industry_max_pct = risk_cfg.get("industry_max_pct", 0.40)

    init_cap = st_cfg["initial_capital"]
    capital = init_cap
    positions = {}
    all_trades = []
    equity_records = []
    bt_df_dict = {}

    stock_data = {}
    stock_atr_pcts = {}

    # 【动态参数切换】加载所有市场状态参数，回测期间按日期切换
    state_params_map = {}
    _opt_file = os.path.join(DATA_DIR, "optimal_params.json")
    if os.path.exists(_opt_file):
        with open(_opt_file, 'r', encoding='utf-8') as f:
            _opt_data = json.load(f)
        for _r in _opt_data.get('results', []):
            if _r.get('status') == 'adopted':
                state_params_map[_r['state']] = _r['params']

    # 构建日期→市场状态映射
    date_to_state = {}
    if state_params_map:
        _bt_start_str = bt_start[:10] if isinstance(bt_start, str) else str(bt_start)[:10]
        _bt_end_str = bt_end[:10] if isinstance(bt_end, str) else str(bt_end)[:10]
        _state_series = get_state_series(_bt_start_str, _bt_end_str)
        for _t in _state_series:
            date_to_state[_t['date']] = _t['state']
        print(f"📊 动态参数切换已启用: {list(state_params_map.keys())} 状态, {len(date_to_state)}天映射")

    for code in code_list:
        try:
            df_raw = get_kline(code, count=800, cfg=cfg)
            if len(df_raw) < 120:
                print(f"{code} 数据不足，跳过")
                continue

            # 【v5.3.4.1修复】截断到bt_end之前，防止信号检测使用回测期末之后的数据（未来函数根治）
            df_raw = df_raw[df_raw["date"] <= bt_end].copy()

            # 【新增】注入真实PE/PB估值分位（从data.valuation模块获取）
            try:
                from data.valuation import get_valuation_series
                _val_series = get_valuation_series(code, df_raw)
                if _val_series is not None:
                    df_raw["valuation_percentile"] = _val_series
            except Exception:
                pass  # 估值模块不可用时回退到价格分位

            df = calc_indicators_vec(df_raw.copy(), st_cfg)

            # 流动性过滤已移除（回测不模拟滑点，且误杀小盘好票）

            # 【v5.3.4.1改进】预评分改为每日动态计算，不再一次性过滤
            stock_score = 50.0
            print(f"✅ {code} 进入回测（预评分改为每日动态计算）")

            # L2：行业层R²（已移至每日循环中滚动计算，避免未来函数）
            print(f"✅ {code} 进入回测（L2 R²改为每日滚动计算，无未来函数）")

            if trend_cfg.get("enable", False):
                index_code = trend_cfg.get("index_code", "sh000001")
                position_scales = calc_continuous_position_scale(index_code, cfg, df)
                df['position_scale'] = position_scales
                df['market_slope'] = calc_market_slope_series(index_code, cfg, df)
            else:
                df['position_scale'] = 0.5
                df['market_slope'] = 0.0

            # 保存position_scale和market_slope（与状态无关，基于指数）
            _pos_scales = df['position_scale'].values.copy() if 'position_scale' in df.columns else None
            _mkt_slopes = df['market_slope'].values.copy() if 'market_slope' in df.columns else None

            # 【动态参数切换】为每个市场状态生成信号
            _states_to_gen = list(state_params_map.keys()) if state_params_map else ['_default']
            state_stock_data = {}

            for _st_name in _states_to_gen:
                _st_cfg = dict(st_cfg)
                _st_params = state_params_map.get(_st_name)
                if _st_params:
                    _st_cfg['min_entry_score'] = _st_params['min_entry_score']
                    _st_cfg['base_atr_multiplier'] = _st_params['atr_multiplier']
                    _st_cfg['dc_period'] = _st_params['channel_base']
                    _st_cfg['exit_period'] = _st_params['exit_base']
                    _st_cfg['vol_ratio_high'] = _st_params['vol_ratio_high']
                    _st_cfg['vol_ratio_low'] = _st_params['vol_ratio_low']
                    _st_cfg['trend_str_high'] = _st_params['trend_str_high']
                    _st_cfg['trend_str_low'] = _st_params['trend_str_low']
                    _st_cfg['trail_stop_pct'] = _st_params.get('trail_stop_pct', 0.05)
                    _st_cfg['risk_value_ban'] = _st_params.get('risk_value_ban', 80)
                    _st_cfg['score_threshold'] = _st_params.get('score_threshold', 2.0)
                    # 【扩展自适应参数】7个新增参数接入
                    _st_cfg['base_risk_pct'] = _st_params.get('base_risk_pct', 0.10)
                    _st_cfg['max_concurrent_positions'] = _st_params.get('max_concurrent_positions', 5)
                    _st_cfg['take_profit_base'] = _st_params.get('take_profit_base', 0.40)
                    _st_cfg['stop_multiplier_base'] = _st_params.get('stop_multiplier_base', 2.0)
                    _st_cfg['add_threshold_base'] = _st_params.get('add_threshold_base', 0.06)
                    _st_cfg['pre_filter_threshold'] = _st_params.get('pre_filter_threshold', 40)
                    _st_cfg['volatility_position_scale_factor'] = _st_params.get('vol_pos_factor', 0.15)
                    # 【新增】vol_stack分档阈值接入
                    _st_cfg['vol_stack_threshold_large'] = _st_params.get('vol_stack_threshold_large', 3.0)
                    _st_cfg['vol_stack_threshold_mid'] = _st_params.get('vol_stack_threshold_mid', 5.0)
                    _st_cfg['vol_stack_threshold_small'] = _st_params.get('vol_stack_threshold_small', 8.0)

                # 用该状态的参数重新计算指标
                df_s = calc_indicators_vec(df_raw.copy(), _st_cfg)
                if _pos_scales is not None:
                    df_s['position_scale'] = _pos_scales
                if _mkt_slopes is not None:
                    df_s['market_slope'] = _mkt_slopes

                # 计算自适应参数
                apl = []
                for idx in range(len(df_s)):
                    if idx < 30:
                        params = {
                            'risk_pct': _st_cfg.get('base_risk_pct', 0.10),
                            'stop_multiplier': _st_cfg.get('base_atr_multiplier', 2.0),
                            'chan_threshold': _st_cfg.get('base_chan_threshold', 0.70),
                            'add_threshold': 0.06,
                            'take_profit_threshold': 0.40,
                            'high_vol_risk_reduce': 1.0,
                            'r2_20': 0.0
                        }
                    else:
                        market_slope = df_s.loc[idx, 'market_slope'] if 'market_slope' in df_s.columns else 0
                        atr_series = df_s['atr'].iloc[max(0, idx-20):idx+1]
                        atr_current = atr_series.iloc[-1]
                        atr_ma20 = atr_series.mean()
                        volatility_index = atr_current / atr_ma20 if atr_ma20 > 0 else 1.0
                        params = calc_adaptive_params(df_s, idx, _st_cfg, market_slope, volatility_index)
                    apl.append(params)

                avg_threshold = np.mean([p['chan_threshold'] for p in apl[30:]]) if len(df_s) > 30 else 0.70
                df_s = detect_chan_signals_optimized(df_s, pivot_win=5, chan_threshold=avg_threshold)
                # 【新增】注入mc_est供gen_signal_enhanced按市值分档
                _st_cfg['mc_est'] = estimate_market_cap(df_raw['close'].iloc[0])
                df_s = gen_signal_enhanced(df_s, _st_cfg, apl[-1] if apl else {})

                mask = (df_s["date"] >= bt_start) & (df_s["date"] <= bt_end)
                ms = int(np.argmax(mask.values)) if mask.any() else 0
                bt_df_s = df_s[mask].reset_index(drop=True)

                if len(bt_df_s) < 30 and _st_name == _states_to_gen[0]:
                    print(f"{code} 回测区间数据不足")
                    break
                if len(bt_df_s) < 30:
                    continue

                state_stock_data[_st_name] = {
                    'bt_df': bt_df_s,
                    'full_df': df_s,
                    'adaptive_params_list': apl,
                    'mask_start': ms,
                    'st_cfg': _st_cfg,
                }

            if not state_stock_data:
                continue

            # 用第一个状态的数据作为默认（用于绘图等）
            _first_sd = list(state_stock_data.values())[0]
            bt_df = _first_sd['bt_df']
            bt_df_dict[code] = bt_df
            mc_est = estimate_market_cap(bt_df['close'].iloc[0])

            # L3收集ATR%（与状态无关，用默认df）
            _default_df_s = list(state_stock_data.values())[0]
            _full_df = df_raw.copy()
            # 重新计算default指标用于ATR%（只用回测前数据）
            _df_for_atr = calc_indicators_vec(_full_df, st_cfg)
            pre_atr_df = _df_for_atr[_df_for_atr['date'] < bt_start]
            if len(pre_atr_df) > 20:
                valid_atr = pre_atr_df['atr'].dropna()
            else:
                valid_atr = _default_df_s['bt_df']['atr'].dropna()
            if len(valid_atr) > 0:
                valid_close = (pre_atr_df['close'].loc[valid_atr.index] if len(pre_atr_df) > 20
                               else _default_df_s['bt_df']['close'].loc[valid_atr.index])
                stock_atr_pct = float((valid_atr / valid_close * 100).mean())
            else:
                stock_atr_pct = 0
            stock_atr_pcts[code] = stock_atr_pct

            stock_data[code] = {
                'state_data': state_stock_data,
                'mc_est': mc_est,
                'score': stock_score
            }

        except Exception as e:
            print(f"{code} 准备数据异常: {str(e)}")
            import traceback
            traceback.print_exc()
            continue

    if not stock_data:
        return [], pd.DataFrame(), init_cap, {}, 0, None

    stock_date_index = {}
    for code, sd in stock_data.items():
        stock_date_index[code] = {}
        for _st_name, _st_sd in sd['state_data'].items():
            _bt_df = _st_sd['bt_df']
            stock_date_index[code][_st_name] = {_bt_df.iloc[i]["date"]: i for i in range(len(_bt_df))}

    _all_date_sets = []
    for _code_dsi in stock_date_index.values():
        for _st_dsi in _code_dsi.values():
            _all_date_sets.append(set(_st_dsi.keys()))
    all_dates = sorted(set().union(*_all_date_sets)) if _all_date_sets else []
    max_add = st_cfg["max_add_count"]
    last_known_close = {}
    max_dd = 0

    # 【变更4】账户级回撤熔断状态
    # circuit_dd_halve: 回撤达到此阈值时所有持仓减半
    # circuit_dd_stop:  回撤达到此阈值时清仓并停止交易至月末
    _dd_halve = risk_cfg.get("circuit_dd_halve", 0.15)
    _dd_stop = risk_cfg.get("circuit_dd_stop", 0.25)
    _peak_equity = init_cap
    _circuit_stop_until = None  # datetime.date; 该日期之前（含当日）不开新仓
    _halved_peak = None  # 记录上一次已触发减半时的peak，避免重复减半

    for dt in all_dates:
        # 查找当前日期的市场状态
        dt_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt)[:10]
        current_state = date_to_state.get(dt_str, None)
        if not current_state:
            current_state = '_default'

        daily_rows = {}
        daily_state_sd = {}
        for code, sd in stock_data.items():
            # 选择当前状态的数据，如果没有则用第一个可用状态
            _st_data = sd['state_data']
            if current_state in _st_data:
                _st_sd = _st_data[current_state]
            else:
                _st_sd = list(_st_data.values())[0]
            _st_name_actual = current_state if current_state in _st_data else list(_st_data.keys())[0]

            _st_dsi = stock_date_index[code].get(_st_name_actual, {})
            bt_idx = _st_dsi.get(dt)
            if bt_idx is not None:
                row = _st_sd['bt_df'].iloc[bt_idx]
                daily_rows[code] = (bt_idx, row)
                daily_state_sd[code] = _st_sd
                last_known_close[code] = row["close"]

        if not daily_rows:
            continue

        daily_params = {}
        for code, (bt_idx, row) in daily_rows.items():
            sd = stock_data[code]
            _st_sd = daily_state_sd[code]
            _st_cfg = _st_sd['st_cfg']
            close = row["close"]
            low = row["low"]
            high = row["high"]
            atr = row["atr"]
            pos_scale = row.get("position_scale", 0.5)
            trend_strength = row.get("trend_strength", 0)

            volatility_percentile = row.get("volatility_percentile", 0.5)
            vol_adaptive = st_cfg.get('volatility_adaptive', True)
            vol_stop_min = st_cfg.get('volatility_stop_multiplier_min', 1.5)
            vol_stop_max = st_cfg.get('volatility_stop_multiplier_max', 3.0)
            _cur_sp = state_params_map.get(current_state, {})
            vol_pos_factor = _cur_sp.get('vol_pos_factor', st_cfg.get('volatility_position_scale_factor', 0.15))

            if vol_adaptive:
                vol_stop_multiplier = vol_stop_min + (vol_stop_max - vol_stop_min) * volatility_percentile
                vol_pos_reduce = 1.0 - vol_pos_factor * volatility_percentile
            else:
                vol_stop_multiplier = 1.0
                vol_pos_reduce = 1.0

            adaptive_params_list = _st_sd['adaptive_params_list']
            mask_start = _st_sd['mask_start']
            params = adaptive_params_list[bt_idx + mask_start] if bt_idx + mask_start < len(adaptive_params_list) else {
                'risk_pct': 0.10, 'stop_multiplier': 2.0, 'chan_threshold': 0.70,
                'add_threshold': 0.06, 'take_profit_threshold': 0.40,
                'high_vol_risk_reduce': 1.0, 'r2_20': 0.0
            }
            risk_pct = params['risk_pct'] * vol_pos_reduce
            stock_atr_pct = stock_atr_pcts.get(code, 0)
            all_atr_vals = list(stock_atr_pcts.values())
            if all_atr_vals:
                vol_percentile = sum(1 for a in all_atr_vals if a <= stock_atr_pct) / len(all_atr_vals) * 100
            else:
                vol_percentile = 50
            if vol_percentile > 70:
                adaptive_atr_mult = 2.5 + (vol_percentile - 70) / 30 * 0.5
            elif vol_percentile >= 40:
                adaptive_atr_mult = 2.0 + (vol_percentile - 40) / 30 * 0.5
            else:
                adaptive_atr_mult = 1.5 + vol_percentile / 40 * 0.5
            stop_multiplier = params['stop_multiplier'] * vol_stop_multiplier
            add_threshold = params['add_threshold']
            take_profit_threshold = params['take_profit_threshold']

            # 【优化2】趋势因子：强趋势加仓1.2倍，弱趋势减仓至0.6倍（阈值从-0.10放宽到0）
            if trend_strength > 0.15:
                pos_scale = min(pos_scale * 1.2, 1.0)
            elif trend_strength < 0:
                pos_scale = pos_scale * 0.6

            # 【新增】风险值仓位调整（借鉴中和应泰强龙风控，自适应）
            risk_val = row.get("risk_value", 50)
            if pd.isna(risk_val):
                risk_val = 50
            risk_ban = _st_cfg.get('risk_value_ban', 80)
            risk_warn = risk_ban * 0.84  # 警戒线=ban线×0.84（原80→67）
            if risk_val > risk_warn:
                pos_scale *= 0.5  # 警戒区仓位减半

            # 【新增】量比仓位调整（借鉴中和应泰强龙换手）
            vol_turn = row.get("vol_turnover", 1.0)
            if pd.isna(vol_turn):
                vol_turn = 1.0
            if vol_turn > 15:
                pos_scale *= 0.5  # 投机过热减仓
            # vol_turn < 0.5 在买入时跳过（流动性不足）

            # 【新增】信号置信度仓位（借鉴中和应泰无强扭转应对法则，自适应）
            entry_score = row.get("entry_score", 3.0)
            if pd.isna(entry_score):
                entry_score = 3.0
            score_thresh = _st_cfg.get('score_threshold', 2.0)
            if entry_score < score_thresh:
                pos_scale *= 0.5  # 边际信号半仓
            elif entry_score < score_thresh + 1.0:
                pos_scale *= 0.8  # 中等信号八成仓

            # L4：基于20日R²调整止盈
            r2_20 = params.get('r2_20', 0.0)
            if r2_20 > 0.15:
                take_profit_pct = 0.15
            elif r2_20 > 0.05:
                take_profit_pct = 0.25
            else:
                take_profit_pct = 0.40

            # 【L2修复】滚动计算120日R²（只用当前日期及之前的数据，避免未来函数）
            _cur_st_sd_r2 = daily_state_sd.get(code)
            if _cur_st_sd_r2 is not None:
                _full_df_r2 = _cur_st_sd_r2.get('full_df', _cur_st_sd_r2['bt_df'])
                _ms_r2 = _cur_st_sd_r2.get('mask_start', 0)
                _full_idx_r2 = _ms_r2 + bt_idx
                _r2_lookback = min(120, _full_idx_r2 + 1)
                _r2_closes = _full_df_r2['close'].iloc[max(0, _full_idx_r2 - _r2_lookback + 1):_full_idx_r2 + 1].values.tolist()
                current_r2_120 = calc_r2(_r2_closes, lookback=120)
            else:
                current_r2_120 = 0.0

            daily_params[code] = {
                'close': close, 'low': low, 'high': high, 'atr': atr,
                'pos_scale': pos_scale, 'trend_strength': trend_strength,
                'risk_pct': risk_pct, 'stop_multiplier': stop_multiplier,
                'add_threshold': add_threshold, 'take_profit_threshold': take_profit_threshold,
                'take_profit_pct': take_profit_pct,
                'buy_signal': row.get("buy_signal", False),
                'sell_signal': row.get("sell_signal", False),
                'chan_recent_type': row.get("chan_recent_type", ""),
                'vol_turnover': vol_turn,  # 【新增】量比
                'risk_value': risk_val,    # 【新增】风险值
                'ma20': row.get("ma20", close),       # 【新增】阶梯止盈用
                'ma60': row.get("ma60", close),       # 【新增】趋势锁仓用
                'ma20_slope': row.get("ma20_slope", 0), # 【新增】趋势锁仓用
                'r2_120_rolling': current_r2_120,     # 【L2修复】滚动120日R²
            }

        # 卖出
        to_del = []
        for c, pos in list(positions.items()):
            if c not in daily_rows:
                continue

            dp = daily_params[c]
            sd = stock_data[c]
            mc_est = sd['mc_est']
            close = dp['close']
            low = dp['low']

            t = pos.trade
            sl = t.stop_loss
            sell_flag = False
            sell_reason = ""
            sell_price = close
            sell_shares = pos.current_shares

            # 【优化4】动态阶梯止盈触发线：强趋势提高至25%，弱趋势降低至10%
            pnl = (close - t.buy_price) / t.buy_price
            ma20_val = dp.get('ma20', close)
            ma60_val = dp.get('ma60', close)
            ma20_s = dp.get('ma20_slope', 0)

            _sell_ts = dp.get('trend_strength', 0)
            if _sell_ts > 0.15:
                _tier_trigger = 0.20
            elif _sell_ts < -0.05:
                _tier_trigger = 0.10
            else:
                _tier_trigger = 0.15

            if pnl > _tier_trigger:
                # 浮盈超触发线：用MA20×0.98作为离场线
                tier_stop = ma20_val * 0.98
                tier_stop = max(tier_stop, t.buy_price)
                if tier_stop > sl:
                    sl = tier_stop
            elif pnl > 0.05:
                # 浮盈5%-触发线：止损上移到成本价（保本）
                if t.buy_price > sl:
                    sl = t.buy_price
            # pnl <= 0.05：保持原ATR止损不变

            if low <= sl:
                sell_flag = True
                sell_price = sl
                if pnl > _tier_trigger:
                    sell_reason = f"阶梯止盈(MA20×0.98) {sl:.2f}"
                elif pnl > 0.05:
                    sell_reason = f"保本止损 {sl:.2f}"
                else:
                    sell_reason = f"ATR止损 {sl:.2f}"

            # 动态止盈（部分卖出）保持不变
            if not sell_flag and not t.partial_sold:
                pnl_pct = (close - t.buy_price) / t.buy_price
                if pnl_pct > dp['take_profit_threshold']:
                    sell_shares = int(pos.current_shares * dp['take_profit_pct'])
                    if sell_shares >= 100:
                        sell_flag = True
                        sell_price = close
                        sell_reason = f"动态止盈(卖{int(dp['take_profit_pct']*100)}%)"
                        t.partial_sold = True
                        t.remaining_shares = pos.current_shares - sell_shares

            # 【新增】趋势延续锁仓：强趋势中屏蔽海龟退出信号，让利润奔跑
            trend_lock = (close > ma20_val) and (ma20_val > ma60_val) and (ma20_s > 0.02)

            if dp['sell_signal'] and not sell_flag and not trend_lock:
                sell_flag = True
                sell_reason = "海龟退出/缠论顶背离"
                sell_shares = pos.current_shares

            if sell_flag and sell_shares > 0:
                exec_sell, net_sell, comm, tax = calc_trade_cost(
                    sell_price, sell_shares, "sell", trade_cfg, mc_est
                )
                capital += net_sell
                if sell_shares == pos.current_shares:
                    t.sell_date = dt
                    t.sell_price = exec_sell
                    t.sell_value = net_sell
                    t.pnl = net_sell - pos.cost_total
                    t.pnl_pct = t.pnl / pos.cost_total * 100
                    t.sell_reason = sell_reason
                    t.holding_days = (dt - t.buy_date).days
                    all_trades.append(t)
                    to_del.append(c)
                else:
                    pos.current_shares -= sell_shares
                    pos.cost_total = pos.cost_total * (1 - sell_shares / (pos.current_shares + sell_shares))
                    partial_trade = Trade(
                        code=c,
                        buy_date=t.buy_date,
                        buy_price=t.buy_price,
                        shares=sell_shares,
                        buy_value=pos.cost_total * (sell_shares / (pos.current_shares + sell_shares)),
                        atr_at_entry=t.atr_at_entry,
                        stop_loss=t.stop_loss,
                        chan_buy_type=t.chan_buy_type + "(部分)",
                        sell_date=dt,
                        sell_price=exec_sell,
                        sell_value=net_sell,
                        pnl=net_sell - pos.cost_total * (sell_shares / (pos.current_shares + sell_shares)),
                        pnl_pct=(exec_sell - t.buy_price) / t.buy_price * 100,
                        sell_reason=sell_reason,
                        holding_days=(dt - t.buy_date).days
                    )
                    all_trades.append(partial_trade)

        for c in to_del:
            del positions[c]

        # 【变更4】账户级回撤熔断 — 在今日卖出执行完毕、买入之前
        _mv = 0.0
        for _cc, _pp in positions.items():
            if _cc in daily_rows:
                _mv += _pp.current_shares * daily_rows[_cc][1]["close"]
            elif _cc in last_known_close:
                _mv += _pp.current_shares * last_known_close[_cc]
        _equity_today = capital + _mv
        if _equity_today > _peak_equity:
            _peak_equity = _equity_today
        _dd_now = (_equity_today - _peak_equity) / _peak_equity if _peak_equity > 0 else 0.0

        # 触发清仓熔断：清仓并停止开新仓至当月末
        if _dd_now <= -_dd_stop and positions:
            _cb_to_del = []
            for _cc, _pp in list(positions.items()):
                if _cc not in daily_rows:
                    continue
                _row_c = daily_rows[_cc][1]
                _sd_c = stock_data[_cc]
                _mc_c = _sd_c['mc_est']
                _px_c = _row_c["close"]
                _sh_c = _pp.current_shares
                _ex, _net, _comm, _tax = calc_trade_cost(_px_c, _sh_c, "sell", trade_cfg, _mc_c)
                capital += _net
                _tt = _pp.trade
                _tt.sell_date = dt
                _tt.sell_price = _ex
                _tt.sell_value = _net
                _tt.pnl = _net - _pp.cost_total
                _tt.pnl_pct = _tt.pnl / _pp.cost_total * 100
                _tt.sell_reason = f"熔断清仓(DD{_dd_now*100:.1f}%)"
                _tt.holding_days = (dt - _tt.buy_date).days
                all_trades.append(_tt)
                _cb_to_del.append(_cc)
            for _cc in _cb_to_del:
                del positions[_cc]
            # 停止至当月末
            if hasattr(dt, 'replace'):
                if dt.month == 12:
                    _circuit_stop_until = dt.replace(day=31)
                else:
                    _next_month = dt.replace(month=dt.month + 1, day=1)
                    _circuit_stop_until = _next_month - timedelta(days=1)
        # 触发减半熔断（peak创新高后重置，每个peak只减半一次）
        elif _dd_now <= -_dd_halve and positions and _halved_peak != _peak_equity:
            _halved_peak = _peak_equity
            for _cc, _pp in list(positions.items()):
                if _cc not in daily_rows:
                    continue
                _row_c = daily_rows[_cc][1]
                _sd_c = stock_data[_cc]
                _mc_c = _sd_c['mc_est']
                _px_c = _row_c["close"]
                _sh_c = _pp.current_shares
                _half = (_sh_c // 2 // 100) * 100
                if _half < 100:
                    continue
                _ex, _net, _comm, _tax = calc_trade_cost(_px_c, _half, "sell", trade_cfg, _mc_c)
                capital += _net
                _orig_cost = _pp.cost_total
                _pp.current_shares -= _half
                _pp.cost_total = _orig_cost * (_pp.current_shares / _sh_c) if _sh_c > 0 else _orig_cost
                _tt = _pp.trade
                _partial = Trade(
                    code=_cc, buy_date=_tt.buy_date, buy_price=_tt.buy_price,
                    shares=_half, buy_value=_orig_cost * (_half / _sh_c),
                    atr_at_entry=_tt.atr_at_entry, stop_loss=_tt.stop_loss,
                    chan_buy_type=_tt.chan_buy_type + "(熔断减半)",
                    sell_date=dt, sell_price=_ex, sell_value=_net,
                    pnl=_net - _orig_cost * (_half / _sh_c),
                    pnl_pct=(_ex - _tt.buy_price) / _tt.buy_price * 100,
                    sell_reason=f"熔断减半(DD{_dd_now*100:.1f}%)",
                    holding_days=(dt - _tt.buy_date).days
                )
                all_trades.append(_partial)

        # 熔断停止期内禁止开新仓
        _circuit_block_buy = False
        if _circuit_stop_until is not None and hasattr(dt, 'date'):
            try:
                if dt.date() <= _circuit_stop_until:
                    _circuit_block_buy = True
            except Exception:
                pass

        # 买入
        _cur_sp = state_params_map.get(current_state, {})
        max_concurrent = _cur_sp.get('max_concurrent_positions', st_cfg.get('max_concurrent_positions', 5))
        buy_candidates = sorted(
            [(code, bt_idx, row) for code, (bt_idx, row) in daily_rows.items()
             if code in stock_data],
            key=lambda x: stock_data[x[0]]['score'],
            reverse=True
        )

        for code, bt_idx, row in buy_candidates:
            # 【变更4】熔断停止期内禁止任何买入/加仓
            if _circuit_block_buy:
                break
            dp = daily_params[code]
            sd = stock_data[code]
            mc_est = sd['mc_est']
            close = dp['close']
            high = dp['high']
            atr = dp['atr']
            pos_scale = dp['pos_scale']

            # 【L2修复】滚动R² < 0.15时仓位减半（不直接跳过）
            _r2_120 = dp.get('r2_120_rolling', 0.0)
            if _r2_120 < 0.15:
                pos_scale = pos_scale * 0.5

            if pos_scale <= 0.01:
                continue

            if code not in positions and dp['buy_signal']:
                if len(positions) >= max_concurrent:
                    continue
                if pd.isna(atr) or atr <= 0:
                    continue
                # 【新增】ST/退市股票过滤
                if st_cfg.get('exclude_st', True):
                    try:
                        from data.valuation import is_st_stock
                        if is_st_stock(code):
                            continue
                    except Exception:
                        pass
                # 【新增】波动率过滤：ATR占股价比过低的大盘股跳过
                _min_atr_pct = st_cfg.get('min_atr_pct', 0.015)
                if atr / close < _min_atr_pct:
                    continue
                # 【新增】量比<0.5流动性不足，跳过买入
                vol_turn = dp.get('vol_turnover', 1.0)
                if vol_turn < 0.5:
                    continue

                # 【变更3】行业集中度检查：买入后该行业总仓位占比不得超过industry_max_pct
                # 用当前权益（现金+持仓市值）作为分母
                _eq_now = capital + sum(
                    p.current_shares * daily_rows.get(cc, (None, {}))[1].get("close", last_known_close.get(cc, 0))
                    for cc, p in positions.items()
                )
                _new_industry = _get_industry(code)
                _cur_ind_exposure = 0.0
                for _ec, _ep in positions.items():
                    if _get_industry(_ec) == _new_industry:
                        _epx = daily_rows.get(_ec, (None, {}))[1].get("close", last_known_close.get(_ec, 0))
                        _cur_ind_exposure += _ep.current_shares * _epx
                # 预估本次买入市值（与下面 max_cap_share 口径一致）
                _est_new_value = capital * risk_cfg["single_max_pos"] * pos_scale
                if _eq_now > 0 and (_cur_ind_exposure + _est_new_value) / _eq_now > industry_max_pct:
                    continue

                # 【v5.3.4.1改进】每日动态预评分检查（使用全量历史数据，不受回测起点影响）
                _cur_st_sd = daily_state_sd.get(code)
                if _cur_st_sd is not None and bt_idx >= 30:
                    _full_df = _cur_st_sd.get('full_df', _cur_st_sd['bt_df'])
                    _ms = _cur_st_sd.get('mask_start', 0)
                    _full_idx = _ms + bt_idx
                    _lookback = min(250, _full_idx + 1)
                    _score_df = _full_df.iloc[max(0, _full_idx - _lookback + 1):_full_idx + 1]
                    if len(_score_df) >= 30:
                        import io as _io
                        _old_out = sys.stdout
                        sys.stdout = _io.StringIO()
                        try:
                            _daily_score = calc_stock_quality_score(_score_df, _cur_st_sd.get('st_cfg', st_cfg))
                        finally:
                            sys.stdout = _old_out
                        _cur_sp = state_params_map.get(current_state, {})
                        _daily_thresh = _cur_sp.get('pre_filter_threshold', st_cfg.get('pre_filter_threshold', 40))
                        if _daily_score < _daily_thresh:
                            continue

                risk_unit = capital * dp['risk_pct']
                stop_dist = dp['stop_multiplier'] * atr
                max_risk_share = int(risk_unit / stop_dist * pos_scale)
                max_cap_share = int((capital * risk_cfg["single_max_pos"] * pos_scale) / close)
                shares = min(max_risk_share, max_cap_share)
                shares = (shares // 100) * 100

                if shares < 100:
                    continue

                exec_buy, cost_total, comm, tax = calc_trade_cost(
                    close, shares, "buy", trade_cfg, mc_est
                )
                if cost_total > capital:
                    continue

                capital -= cost_total
                sl = exec_buy - stop_dist
                new_trade = Trade(
                    code=code, buy_date=dt, buy_price=exec_buy, shares=shares,
                    buy_value=cost_total, atr_at_entry=atr, stop_loss=sl,
                    chan_buy_type=dp['chan_recent_type'],
                    partial_sold=False,
                    remaining_shares=0
                )
                positions[code] = Position(
                    code=code, trade=new_trade, cost_total=cost_total,
                    current_shares=shares
                )

            # 加仓
            if code in positions and pos_scale > 0.5:
                pos = positions[code]
                t = pos.trade
                if t.add_count < max_add:
                    pnl_pct = (close - t.buy_price) / t.buy_price
                    threshold = dp['add_threshold'] * (1 + t.add_count * 0.7)
                    if pnl_pct > threshold:
                        add_tp = t.buy_price + (t.add_count + 1) * st_cfg["add_interval_atr"] * atr
                        if high >= add_tp or pnl_pct > threshold * 2:
                            add_shares = t.shares // 2
                            if add_shares < 100:
                                add_shares = t.shares // 3
                            if add_shares >= 100:
                                exec_add, add_cost, _, _ = calc_trade_cost(
                                    add_tp, add_shares, "buy", trade_cfg, mc_est
                                )
                                if add_cost <= capital:
                                    capital -= add_cost
                                    pos.current_shares += add_shares
                                    all_cost = pos.cost_total + add_cost
                                    avg_cost = all_cost / pos.current_shares
                                    t.buy_price = avg_cost
                                    t.stop_loss = avg_cost - dp['stop_multiplier'] * atr
                                    pos.cost_total = all_cost
                                    t.add_count += 1

        market_val = 0
        for c, p in positions.items():
            if c in daily_rows:
                market_val += p.current_shares * daily_rows[c][1]["close"]
            elif c in last_known_close:
                market_val += p.current_shares * last_known_close[c]

        daily_equity = capital + market_val
        equity_records.append({"date": dt, "equity": daily_equity})
        # ============================================================
        # 【本地化适配】保存每日快照（供 performance_monitor 读取）
        # ============================================================
        try:
            _snapshot_dir = os.path.join(DATA_DIR, "snapshots")
            os.makedirs(_snapshot_dir, exist_ok=True)
            _snapshot_file = os.path.join(_snapshot_dir, f"snapshot_{dt.strftime('%Y%m%d')}.json")
            _pos_list = []
            for _c, _p in positions.items():
                _price = daily_rows.get(_c, (None, {}))[1].get('close', 0) if daily_rows.get(_c) else 0
                _pos_list.append({
                    'code': _c,
                    'shares': _p.current_shares,
                    'price': round(float(_price), 2)
                })
            _snapshot_data = {
                'date': dt.strftime('%Y-%m-%d'),
                'equity': round(float(daily_equity), 2),
                'cash': round(float(capital), 2),
                'position_count': len(_pos_list),
                'positions': _pos_list,
                'max_drawdown': round(float(max_dd) if max_dd else 0, 4)
            }
            with open(_snapshot_file, 'w', encoding='utf-8') as _f:
                json.dump(_snapshot_data, _f)
            _snapshot_files = sorted(glob.glob(os.path.join(_snapshot_dir, "snapshot_*.json")))
            for _old_f in _snapshot_files[:-30]:
                try:
                    os.remove(_old_f)
                except:
                    pass
        except Exception as _e:
            pass
        # ============================================================

    if equity_records:
        eq_df = pd.DataFrame(equity_records)
        eq_df = eq_df.sort_values("date").reset_index(drop=True)
        eq_df['peak'] = eq_df['equity'].expanding().max()
        eq_df['drawdown'] = (eq_df['equity'] - eq_df['peak']) / eq_df['peak']
        max_dd = eq_df['drawdown'].min() if len(eq_df) > 0 else 0
        max_dd_date = eq_df[eq_df['drawdown'] == max_dd]['date'].iloc[0] if len(eq_df) > 0 else None
    else:
        eq_df = pd.DataFrame()
        max_dd = 0
        max_dd_date = None

    final_cap = capital
    return all_trades, eq_df, final_cap, bt_df_dict, max_dd, max_dd_date

# ===================== 绘图 =====================
def plot_backtest_report(code, trades, eq_df, final_cap, bt_df, init_cap, max_dd, output_dir, save_name):
    fig, axes = plt.subplots(3, 1, figsize=(16, 12), gridspec_kw={'height_ratios': [3, 2, 1]})
    fig.suptitle(f'龟缠安泰v5.3.4.1 攻守兼备升级版 — {code}', fontsize=16, fontweight='bold')

    ax1 = axes[0]
    ax1.plot(bt_df["date"], bt_df["close"], color='#333333', linewidth=1, label='收盘价')
    if "dc_high" in bt_df.columns:
        ax1.plot(bt_df["date"], bt_df["dc_high"], '--', color='orange', linewidth=0.8, alpha=0.6, label='唐奇安上轨')
    if "dc_low" in bt_df.columns:
        ax1.plot(bt_df["date"], bt_df["dc_low"], '--', color='purple', linewidth=0.8, alpha=0.5, label='唐奇安下轨')
    if "ma60" in bt_df.columns:
        ax1.plot(bt_df["date"], bt_df["ma60"], '-', color='blue', linewidth=0.8, alpha=0.4, label='MA60')

    buy_dates = [t.buy_date for t in trades if t.code == code]
    buy_prices = [t.buy_price for t in trades if t.code == code]
    sell_dates = [t.sell_date for t in trades if t.code == code and t.sell_date is not None]
    sell_prices = [t.sell_price for t in trades if t.code == code and t.sell_date is not None]

    if buy_dates:
        ax1.scatter(buy_dates, buy_prices, marker='^', color='red', s=100, zorder=5, label=f'买入({len(buy_dates)})')
    if sell_dates:
        ax1.scatter(sell_dates, sell_prices, marker='v', color='green', s=100, zorder=5, label=f'卖出({len(sell_dates)})')

    ax1.set_ylabel('价格', fontsize=11)
    ax1.legend(loc='upper left', fontsize=8, ncol=3)
    ax1.grid(True, alpha=0.3)
    ax1.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))

    ax2 = axes[1]
    if len(eq_df) > 0:
        ax2.fill_between(eq_df["date"], eq_df["equity"], init_cap,
                         where=eq_df["equity"] >= init_cap, color='green', alpha=0.15, label='盈利区间')
        ax2.fill_between(eq_df["date"], eq_df["equity"], init_cap,
                         where=eq_df["equity"] < init_cap, color='red', alpha=0.15, label='亏损区间')
        ax2.plot(eq_df["date"], eq_df["equity"], color='#2196F3', linewidth=1.5, label='账户净值')
        ax2.axhline(y=init_cap, color='gray', linestyle='--', linewidth=0.8, label=f'初始资金({init_cap:,.0f})')

        if max_dd < -0.01:
            max_dd_row = eq_df[eq_df['drawdown'] == eq_df['drawdown'].min()]
            if len(max_dd_row) > 0:
                dd_date = max_dd_row.iloc[0]['date']
                dd_value = max_dd_row.iloc[0]['equity']
                ax2.annotate(f'最大回撤: {max_dd*100:.1f}%',
                             (dd_date, dd_value),
                             textcoords="offset points", xytext=(30, -20),
                             fontsize=10, color='red', arrowprops=dict(arrowstyle='->', color='red'))

        total_ret = (final_cap - init_cap) / init_cap * 100
        ax2.set_title(f'资金曲线 | 期末: {final_cap:,.2f} | 总收益: {total_ret:+.2f}% | 最大回撤: {max_dd*100:.2f}%', fontsize=11)
    ax2.set_ylabel('资金', fontsize=11)
    ax2.legend(loc='upper left', fontsize=8)
    ax2.grid(True, alpha=0.3)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))

    ax3 = axes[2]
    ax3.axis('off')
    col_labels = ['序号', '买入日', '买入价', '股数', '卖出日', '卖出价', '盈亏%', '持仓天', '卖出原因']
    table_data = []
    for i, t in enumerate(trades):
        if t.code == code:
            table_data.append([
                i + 1,
                t.buy_date.strftime('%Y-%m-%d') if t.buy_date else '-',
                f'{t.buy_price:.2f}',
                f'{t.shares}',
                t.sell_date.strftime('%Y-%m-%d') if t.sell_date else '-',
                f'{t.sell_price:.2f}' if t.sell_price else '-',
                f'{t.pnl_pct:+.1f}%',
                f'{t.holding_days}',
                t.sell_reason[:12]
            ])
    if table_data:
        table = ax3.table(cellText=table_data, colLabels=col_labels, loc='center',
                          cellLoc='center', colColours=['#4CAF50'] * len(col_labels))
        table.auto_set_font_size(False)
        table.set_fontsize(8)
        table.scale(1, 1.2)
        for i, row_data in enumerate(table_data):
            pnl_val = float(row_data[6].replace('%', '').replace('+', ''))
            color = '#c8e6c9' if pnl_val >= 0 else '#ffcdd2'
            for j in range(len(col_labels)):
                table[i + 1, j].set_facecolor(color)

    plt.tight_layout(rect=[0, 0, 1, 0.96])
    Path(output_dir).mkdir(exist_ok=True)
    save_path = Path(output_dir) / f"{save_name}.png"
    plt.savefig(save_path, dpi=150, bbox_inches='tight')
    plt.close()
    print(f"图表已保存: {save_path}")
    return str(save_path)


# ============================================================
# 市场状态识别模块（原 market_state.py）
# 市场状态识别器 - 自适应参数优化系统第二步
# 功能：
# 1. 基于上证指数计算市场状态特征（MA60斜率、R²、波动率、偏离度）
# 2. 将每日市场划分为4个状态：牛市/熊市/震荡/转折期
# 3. 对历史每一天打标签，生成状态时间线
# 4. 提供实时查询接口：输入日期返回市场状态
# ============================================================
#!/usr/bin/env python3
"""
市场状态识别器 - 自适应参数优化系统第二步
功能：
1. 基于上证指数计算市场状态特征（MA60斜率、R²、波动率、偏离度）
2. 将每日市场划分为4个状态：牛市/熊市/震荡/转折期
3. 对历史每一天打标签，生成状态时间线
4. 提供实时查询接口：输入日期返回市场状态
"""



# ============ 特征计算 ============
# calc_r2 已由 v5.3.4.1 主文件提供（功能等价），此处不再重复定义

def calc_features(df_index):
    """计算上证指数的全部市场状态特征"""
    df = df_index.copy().sort_values('date').reset_index(drop=True)
    
    # MA60
    df['ma60'] = df['close'].rolling(60).mean()
    
    # MA60斜率（5日变化率）
    df['ma60_slope'] = (df['ma60'] - df['ma60'].shift(5)) / df['ma60'].shift(5)
    
    # 价格偏离MA60
    df['deviation'] = (df['close'] / df['ma60'] - 1)
    
    # 60日R²（趋势线性度）
    r2_values = [0.0] * len(df)
    for i in range(60, len(df)):
        r2_values[i] = calc_r2(df['close'].iloc[:i+1].values, lookback=60)
    df['r2_60'] = r2_values
    
    # 波动率（60日收益率标准差）
    df['daily_ret'] = df['close'].pct_change()
    df['volatility'] = df['daily_ret'].rolling(60).std() * np.sqrt(250)  # 年化
    
    # 成交量趋势（20日均量 vs 60日均量）
    if 'volume' in df.columns:
        df['vol_ma20'] = df['volume'].rolling(20).mean()
        df['vol_ma60'] = df['volume'].rolling(60).mean()
        df['vol_trend'] = df['vol_ma20'] / df['vol_ma60']
    else:
        df['vol_trend'] = 1.0
    
    return df

# ============ 状态分类 ============
def classify_state(row):
    """
    规则分类：根据特征将每日划分为4个状态
    牛市：MA60上升 + R²高 + 价格在MA60上方
    熊市：MA60下降 + R²高 + 价格在MA60下方
    震荡：R²低（趋势不明）或MA60斜率接近0
    转折：R²从高位快速下降（趋势正在瓦解）
    """
    slope = row.get('ma60_slope', 0) or 0
    r2 = row.get('r2_60', 0) or 0
    dev = row.get('deviation', 0) or 0
    vol = row.get('volatility', 0) or 0
    
    # 转折期：R²从高位下降（趋势瓦解）
    # 判定条件：R²在0.3-0.5之间且斜率与偏离度方向不一致
    if 0.15 < r2 < 0.5:
        if (slope > 0 and dev < -0.01) or (slope < 0 and dev > 0.01):
            return 'transition'
    
    # 牛市：趋势向上 + 价格在均线上方
    if slope > 0.003 and r2 > 0.5 and dev > 0:
        return 'bull'
    
    # 熊市：趋势向下 + 价格在均线下方
    if slope < -0.003 and r2 > 0.3 and dev < -0.02:
        return 'bear'
    
    # 震荡：趋势不明
    if r2 < 0.15 or abs(slope) < 0.002:
        return 'sideways'
    
    # 介于牛熊之间但方向明确
    if slope > 0 and dev > -0.01:
        return 'bull' if r2 > 0.3 else 'sideways'
    if slope < 0 and dev < 0.01:
        return 'bear' if r2 > 0.2 else 'sideways'
    
    return 'sideways'

def build_state_timeline(df_features):
    """对历史每一天打标签"""
    df = df_features.copy()
    df['market_state'] = df.apply(classify_state, axis=1)
    return df

# ============ 主流程 ============
def generate_timeline():
    """生成完整的市场状态时间线"""
    # 读取上证指数数据
    idx_path = os.path.join(INDEX_DATA_DIR, "sh000001.parquet")
    if not os.path.exists(idx_path):
        raise FileNotFoundError("上证指数数据不存在，请先运行 data_pipeline.py --mode index")
    
    df_index = pd.read_parquet(idx_path)
    print(f"上证指数数据: {len(df_index)} 条, {df_index['date'].min().date()}~{df_index['date'].max().date()}")
    
    # 计算特征
    print("计算市场状态特征...")
    df_features = calc_features(df_index)
    
    # 分类
    print("生成状态标签...")
    df_timeline = build_state_timeline(df_features)
    
    # 统计
    state_counts = df_timeline['market_state'].value_counts()
    print("\n状态分布:")
    for state, count in state_counts.items():
        pct = count / len(df_timeline) * 100
        print(f"  {state}: {count}天 ({pct:.1f}%)")
    
    # 保存时间线
    timeline_data = []
    for _, row in df_timeline.iterrows():
        if pd.isna(row.get('ma60')):
            continue
        timeline_data.append({
            'date': row['date'].strftime('%Y-%m-%d'),
            'close': round(float(row['close']), 2),
            'ma60': round(float(row['ma60']), 2),
            'ma60_slope': round(float(row['ma60_slope']), 6) if pd.notna(row['ma60_slope']) else 0,
            'r2_60': round(float(row['r2_60']), 4),
            'deviation': round(float(row['deviation']), 4),
            'volatility': round(float(row['volatility']), 4) if pd.notna(row['volatility']) else 0,
            'state': row['market_state']
        })
    
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(timeline_data, f, ensure_ascii=False)
    print(f"\n状态时间线已保存到 {STATE_FILE} ({len(timeline_data)} 天)")
    
    return df_timeline

# ============ 查询接口 ============
def get_state(date_str=None):
    """
    查询某一天的市场状态
    date_str: "YYYY-MM-DD" 格式，None则返回最新状态
    返回: dict {state, r2_60, deviation, ma60_slope, volatility}
    """
    if not os.path.exists(STATE_FILE):
        generate_timeline()
    
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        timeline = json.load(f)
    
    if not timeline:
        return {'state': 'unknown', 'description': '无数据'}
    
    if date_str is None:
        entry = timeline[-1]
    else:
        # 找到 <= date_str 的最后一天
        entry = None
        for t in timeline:
            if t['date'] <= date_str:
                entry = t
            else:
                break
    
    if entry is None:
        return {'state': 'unknown', 'description': '日期超出范围'}
    
    descriptions = {
        'bull': '牛市（趋势向上，价格在均线上方）',
        'bear': '熊市（趋势向下，价格在均线下方）',
        'sideways': '震荡（趋势不明）',
        'transition': '转折期（趋势正在瓦解）'
    }
    
    return {
        'date': entry['date'],
        'state': entry['state'],
        'description': descriptions.get(entry['state'], '未知'),
        'r2_60': entry['r2_60'],
        'deviation': entry['deviation'],
        'ma60_slope': entry['ma60_slope'],
        'volatility': entry['volatility'],
        'close': entry['close'],
        'ma60': entry['ma60']
    }

def get_state_series(start_date, end_date):
    """获取一段时间的状态序列"""
    if not os.path.exists(STATE_FILE):
        generate_timeline()
    
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        timeline = json.load(f)
    
    return [t for t in timeline if start_date <= t['date'] <= end_date]

# ============ 主程序 ============


# ============================================================
# 数据管道模块（原 data_pipeline.py）
# A股数据管道 - 自适应参数优化系统第一步
# 数据源：stock-data-skill CLI（腾讯行情API，前复权）
# 功能：
# 1. 从沪深300成分股列表批量下载日线数据（2000条/股，约8年）
# 2. 下载上证指数数据
# 3. 本地parquet存储
# 4. 增量更新（重新拉取最新2000条覆盖更新）
# 5. 查询接口 get_kline(code, start, end) -> DataFrame
# ============================================================
#!/usr/bin/env python3
"""
A股数据管道 - 自适应参数优化系统第一步
数据源：stock-data-skill CLI（腾讯行情API，前复权）
功能：
1. 从沪深300成分股列表批量下载日线数据（2000条/股，约8年）
2. 下载上证指数数据
3. 本地parquet存储
4. 增量更新（重新拉取最新2000条覆盖更新）
5. 查询接口 get_kline(code, start, end) -> DataFrame
"""



# ============ 数据下载 ============
def dp_fetch_kline(code: str, count: int = 2000) -> pd.DataFrame:
    """
    通过stock-data-skill CLI下载K线数据
    code: 纯代码格式，如 sh600396
    count: K线条数（最大2000）
    返回: DataFrame(date, open, high, low, close, volume)
    """
    cmd = [sys.executable, SKILL_PATH_DP, "call", "kline",
           "--param", f"code={code}", "--param", "period=day",
           "--param", f"count={count}", "--param", "fq=qfq"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"CLI错误: {result.stderr[:200]}")
    
    data = json.loads(result.stdout)
    if "data" not in data or not data["data"]:
        raise RuntimeError(f"无数据: {code}")
    
    df = pd.DataFrame(data["data"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "close", "high", "low", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    
    return df

def save_stock_data(df: pd.DataFrame, code: str) -> int:
    """保存股票数据到parquet"""
    filepath = os.path.join(STOCK_DATA_DIR, f"{code}.parquet")
    df.to_parquet(filepath, index=False)
    return len(df)

def download_one_stock(code: str, count: int = 2000) -> dict:
    """下载单只股票并保存，返回结果状态"""
    try:
        df = dp_fetch_kline(code, count)
        rows = save_stock_data(df, code)
        return {'code': code, 'status': 'ok', 'rows': rows,
                'date_range': f"{df['date'].min().date()}~{df['date'].max().date()}"}
    except Exception as e:
        return {'code': code, 'status': 'fail', 'error': str(e)[:100]}

def load_hs300_codes() -> list:
    """加载沪深300成分股代码列表"""
    if os.path.exists(HS300_CODES_FILE):
        with open(HS300_CODES_FILE, 'r') as f:
            codes = [line.strip() for line in f if line.strip()]
        return codes
    return []

def load_main_board_codes() -> list:
    """加载沪深主板全部股票代码列表"""
    if os.path.exists(MAIN_BOARD_CODES_FILE):
        with open(MAIN_BOARD_CODES_FILE, 'r', encoding='utf-8') as f:
            codes = []
            for line in f:
                line = line.strip()
                if line:
                    parts = line.split(',')
                    codes.append(parts[0])  # 只取代码部分
            return codes
    return []

# ============ 新浪API数据下载（长历史+复权）============
def fetch_sina_kline(code, datalen=5000):
    """从新浪API获取不复权日线数据（最多5000条≈21年）"""
    url = f'{SINA_API}?symbol={code}&scale=240&ma=no&datalen={datalen}'
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    resp = urllib.request.urlopen(req, timeout=15)
    data = json.loads(resp.read().decode('utf-8'))
    if not data:
        return None
    df = pd.DataFrame(data)
    df['date'] = pd.to_datetime(df['day'])
    for c in ['open', 'high', 'low', 'close', 'volume']:
        df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.drop('day', axis=1).sort_values('date').reset_index(drop=True)

def fetch_tencent_qfq(code, count=640):
    """从腾讯API获取前复权日线数据（640条≈2.5年）"""
    cmd = [sys.executable, SKILL_PATH_DP, "call", "kline",
           "--param", f"code={code}", "--param", "period=day",
           "--param", f"count={count}", "--param", "fq=qfq"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        return None
    data = json.loads(result.stdout)
    if "data" not in data or not data["data"]:
        return None
    df = pd.DataFrame(data["data"])
    df["date"] = pd.to_datetime(df["date"])
    for c in ["open", "close", "high", "low", "volume"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors='coerce')
    return df.sort_values('date').reset_index(drop=True)

def calc_adjustment_factor(sina_df, tencent_df):
    """用腾讯前复权/新浪不复权计算复权因子，分段处理除权日"""
    merged = pd.merge(
        sina_df[['date', 'close']].rename(columns={'close': 'raw_close'}),
        tencent_df[['date', 'close']].rename(columns={'close': 'qfq_close'}),
        on='date', how='inner')
    if len(merged) < 10:
        return None
    merged['factor'] = merged['qfq_close'] / merged['raw_close']
    factors = merged['factor'].values
    dates = merged['date'].values
    # 分段：因子变化>5%为除权日
    segments = []
    seg_start = 0
    for i in range(1, len(factors)):
        if abs(factors[i] - factors[i-1]) / factors[i-1] > 0.05:
            segments.append((seg_start, i-1, np.median(factors[seg_start:i])))
            seg_start = i
    segments.append((seg_start, len(factors)-1, np.median(factors[seg_start:])))
    latest_factor = segments[-1][2]
    factor_map = {}
    for seg_idx, (start, end, factor) in enumerate(segments):
        for i in range(start, end+1):
            factor_map[dates[i]] = factor / latest_factor
    return factor_map, latest_factor

def apply_adjustment(sina_df, factor_map, latest_factor):
    """将复权因子应用到新浪数据"""
    df = sina_df.copy()
    sorted_factors = sorted(factor_map.items())
    earliest_date = sorted_factors[0][0]
    earliest_factor = sorted_factors[0][1]
    factors = []
    for date in df['date']:
        if date in factor_map:
            factors.append(factor_map[date])
        elif date < earliest_date:
            factors.append(earliest_factor)
        else:
            f = latest_factor
            for d, fac in sorted_factors:
                if d <= date:
                    f = fac
                else:
                    break
            factors.append(f)
    factors = np.array(factors)
    for c in ['open', 'high', 'low', 'close']:
        df[c] = df[c] * factors
    return df

def download_and_adjust(code, datalen=5000):
    """下载单只股票：新浪不复权 + 腾讯前复权 → 复权因子 → 前复权长历史"""
    try:
        sina_df = fetch_sina_kline(code, datalen)
        if sina_df is None or len(sina_df) < 60:
            return {'code': code, 'status': 'fail', 'error': 'sina无数据'}
        sina_df.to_parquet(os.path.join(RAW_DATA_DIR, f"{code}.parquet"), index=False)

        # 读已有腾讯前复权数据
        qfq_path = os.path.join(STOCK_DATA_DIR, f"{code}.parquet")
        tencent_df = pd.read_parquet(qfq_path) if os.path.exists(qfq_path) else fetch_tencent_qfq(code)
        if tencent_df is None or len(tencent_df) < 10:
            sina_df.to_parquet(qfq_path, index=False)
            return {'code': code, 'status': 'ok', 'rows': len(sina_df), 'adjusted': False}

        result = calc_adjustment_factor(sina_df, tencent_df)
        if result is None:
            sina_df.to_parquet(qfq_path, index=False)
            return {'code': code, 'status': 'ok', 'rows': len(sina_df), 'adjusted': False}

        adjusted_df = apply_adjustment(sina_df, *result)
        adjusted_df.to_parquet(qfq_path, index=False)
        return {'code': code, 'status': 'ok', 'rows': len(adjusted_df), 'adjusted': True}
    except Exception as e:
        return {'code': code, 'status': 'fail', 'error': str(e)[:80]}

# ============ 批量下载 ============
def batch_download(codes=None, count=2000, max_workers=5, batch_report=20):
    """
    批量下载股票数据（多线程并发）
    codes: 代码列表，None则用沪深300
    count: 每只股票下载的K线条数
    max_workers: 并发线程数
    """
    if codes is None:
        codes = load_hs300_codes()
    
    total = len(codes)
    if total == 0:
        print("无股票代码，请先确保 hs300_codes.txt 存在")
        return
    
    print(f"开始下载 {total} 只股票，每只 {count} 条K线，并发 {max_workers} 线程")
    start_time = time.time()
    
    results = []
    completed = 0
    success = 0
    fail = 0
    fail_list = []
    
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(download_one_stock, code, count): code for code in codes}
        
        for future in as_completed(future_map):
            completed += 1
            result = future.result()
            results.append(result)
            
            if result['status'] == 'ok':
                success += 1
            else:
                fail += 1
                fail_list.append(result)
            
            if completed % batch_report == 0 or completed == total:
                elapsed = time.time() - start_time
                speed = completed / elapsed if elapsed > 0 else 0
                eta = (total - completed) / speed if speed > 0 else 0
                print(f"进度: {completed}/{total} ({completed/total*100:.1f}%) | "
                      f"成功:{success} 失败:{fail} | "
                      f"速度:{speed:.1f}只/秒 | "
                      f"剩余:{eta/60:.1f}分钟")
    
    elapsed = time.time() - start_time
    print(f"\n下载完成！耗时 {elapsed/60:.1f} 分钟")
    print(f"成功: {success}, 失败: {fail}")
    
    if fail_list:
        print(f"失败股票(前10): {[r['code'] for r in fail_list[:10]]}")
    
    # 保存日志
    log = {
        'update_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        'total': total, 'success': success, 'fail': fail,
        'elapsed_minutes': round(elapsed / 60, 1),
        'fail_list': [{'code': r['code'], 'error': r.get('error', '')} for r in fail_list[:100]]
    }
    with open(LOG_FILE, 'w', encoding='utf-8') as f:
        json.dump(log, f, ensure_ascii=False, indent=2)
    print(f"日志已保存到 {LOG_FILE}")
    
    return log

def download_index(code="sh000001", count=2000):
    """下载指数数据。CLI（stock-data-skill）优先，失败回退腾讯多端点K线。
    （2026-09-01修复：CodeAct沙箱缺CLI API key时原逻辑直接失败，导致指数只有
    每日扫描persist的150条，月度重建时间线只剩91天历史）"""
    df = None
    try:
        df = dp_fetch_kline(code, count)
    except Exception as e:
        print(f"指数 {code} CLI下载失败: {e}")
        df = None
    if df is None or len(df) < 100:
        # 腾讯端点兜底（640条≈2.5年，足够覆盖walk-forward所需历史）
        try:
            df = _tencent_kline_multiep(code, count=min(count, 640), fq="qfq")
            if df is not None and len(df) < 100:
                df = None
        except Exception:
            df = None
    if df is None or len(df) == 0:
        print(f"指数 {code} 下载失败（所有通道不可用）")
        return None
    filepath = os.path.join(INDEX_DATA_DIR, f"{code}.parquet")
    df.to_parquet(filepath, index=False)
    print(f"指数 {code} 已保存，{len(df)} 条，{df['date'].min().date()}~{df['date'].max().date()}")
    return df

# ============ 查询接口 ============
def dp_get_kline(code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """
    从本地读取K线数据
    code: 纯代码格式，如 sh600396
    start_date/end_date: "YYYY-MM-DD" 格式，可选
    """
    filepath = os.path.join(STOCK_DATA_DIR, f"{code}.parquet")
    if not os.path.exists(filepath):
        return pd.DataFrame()
    
    df = pd.read_parquet(filepath)
    if start_date:
        df = df[df['date'] >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df['date'] <= pd.Timestamp(end_date)]
    return df.reset_index(drop=True)

def get_index(code="sh000001", start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """从本地读取指数数据"""
    filepath = os.path.join(INDEX_DATA_DIR, f"{code}.parquet")
    if not os.path.exists(filepath):
        return pd.DataFrame()
    
    df = pd.read_parquet(filepath)
    if start_date:
        df = df[df['date'] >= pd.Timestamp(start_date)]
    if end_date:
        df = df[df['date'] <= pd.Timestamp(end_date)]
    return df.reset_index(drop=True)

def get_all_local_codes() -> list:
    """获取本地已下载的所有股票代码"""
    files = Path(STOCK_DATA_DIR).glob("*.parquet")
    return [f.stem for f in files]

# ============ 数据统计 ============
def data_summary():
    """打印本地数据概况"""
    codes = get_all_local_codes()
    print(f"本地股票数据: {len(codes)} 只")
    
    total_rows = 0
    date_ranges = []
    for code in codes[:50]:  # 抽样50只
        df = dp_get_kline(code)
        if not df.empty:
            total_rows += len(df)
            date_ranges.append((df['date'].min(), df['date'].max()))
    
    if date_ranges:
        avg_rows = total_rows / min(50, len(codes))
        min_date = min(r[0] for r in date_ranges)
        max_date = max(r[1] for r in date_ranges)
        print(f"抽样平均: {avg_rows:.0f} 条/股")
        print(f"日期范围: {min_date.date()} ~ {max_date.date()}")
    
    # 指数
    idx_files = list(Path(INDEX_DATA_DIR).glob("*.parquet"))
    print(f"指数数据: {len(idx_files)} 个")
    for f in idx_files:
        df = pd.read_parquet(f)
        print(f"  {f.stem}: {len(df)} 条, {df['date'].min().date()}~{df['date'].max().date()}")

# ============ 主程序 ============

# ============ 新浪API批量下载 ============
def batch_download_sina(codes, max_workers=8, batch_report=100, datalen=5000):
    """使用新浪API批量下载（长历史+复权），8线程并发"""
    total = len(codes)
    print(f"新浪API批量下载: {total}只, {datalen}条/只, {max_workers}线程")
    start_time = time.time()
    results = []
    success = fail = adjusted = 0
    fail_list = []
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_map = {executor.submit(download_and_adjust, code, datalen): code for code in codes}
        completed = 0
        for future in as_completed(future_map):
            completed += 1
            result = future.result()
            results.append(result)
            if result['status'] == 'ok':
                success += 1
                if result.get('adjusted'):
                    adjusted += 1
            else:
                fail += 1
                fail_list.append(result)
            if completed % batch_report == 0 or completed == total:
                elapsed = time.time() - start_time
                speed = completed / elapsed if elapsed > 0 else 0
                eta = (total - completed) / speed / 60 if speed > 0 else 0
                print(f"进度: {completed}/{total} ({completed/total*100:.1f}%) | "
                      f"成功:{success}(复权{adjusted}) 失败:{fail} | "
                      f"{elapsed:.0f}s 剩余{eta:.1f}min")
            time.sleep(0.01)
    elapsed = time.time() - start_time
    print(f"\n下载完成! 成功:{success}(复权{adjusted}) 失败:{fail} 耗时:{elapsed/60:.1f}分钟")
    if fail_list:
        with open(os.path.join(DATA_DIR, "sina_download_fails.txt"), "w") as f:
            for r in fail_list:
                f.write(f"{r['code']},{r.get('error','')}\n")
    return {'total': total, 'success': success, 'fail': fail, 'adjusted': adjusted}


# ============================================================
# 参数优化引擎模块（原 param_optimizer.py）
# 参数优化引擎 - 自适应参数优化系统第三步
# 功能：
# 1. 定义v5.3.4.1核心参数搜索空间
# 2. 快速向量化回测函数（忠实v5.3.4.1交易逻辑）
# 3. Optuna贝叶斯优化 + walk-forward三段验证
# 4. 按市场状态分别优化
# 5. 防过拟合约束（最少交易笔数、参数范围限制）
# 6. 输出最优参数表供第四步使用
# 
# 优化目标：夏普比率（非纯收益率）
# 防过拟合：参数偏离基准≤30%、最少20笔交易、三段分离
# ============================================================
#!/usr/bin/env python3
"""
参数优化引擎 - 自适应参数优化系统第三步
功能：
1. 定义v5.3.4.1核心参数搜索空间
2. 快速向量化回测函数（忠实v5.3.4.1交易逻辑）
3. Optuna贝叶斯优化 + walk-forward三段验证
4. 按市场状态分别优化
5. 防过拟合约束（最少交易笔数、参数范围限制）
6. 输出最优参数表供第四步使用

优化目标：夏普比率（非纯收益率）
防过拟合：参数偏离基准≤30%、最少20笔交易、三段分离
"""

warnings.filterwarnings('ignore')

try:
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    _HAS_OPTUNA = True
except ImportError:
    _HAS_OPTUNA = False


# ============ 基准参数（v5.3.4.1默认值）============
BASELINE_PARAMS = {
    'vol_ratio_high': 1.3,      # >此值用25日通道
    'vol_ratio_low': 0.7,       # <此值用15日通道
    'trend_str_high': 0.15,     # >此值用12日退出线
    'trend_str_low': -0.05,     # <此值用8日退出线
    'min_entry_score': 1.7,     # 入场最低评分
    'atr_multiplier': 2.0,      # ATR止损倍数
    'channel_base': 20,         # 基准通道周期
    'exit_base': 10,            # 基准退出线周期
    'trail_stop_pct': 0.05,     # 追踪止盈基准触发线（上限=3×此值）
    'risk_value_ban': 80,       # 风险值超过此值禁止买入
    'score_threshold': 2.0,     # 评分低于此值半仓（低于此值+1.0八成仓）
    # 【扩展】7个新增自适应参数
    'base_risk_pct': 0.10,      # 单笔风险比例基准
    'max_concurrent_positions': 5,  # 最大同时持仓数
    'take_profit_base': 0.40,   # 止盈触发基准
    'stop_multiplier_base': 2.0,  # 止损倍数基准
    'add_threshold_base': 0.06,  # 加仓阈值基准
    'pre_filter_threshold': 40,  # 质量评分门槛
    'vol_pos_factor': 0.15,     # 波动率仓位缩减因子
}

# ============ 数据加载 ============
def load_stock_data(code):
    """从本地parquet加载股票数据，pyarrow不可用时回退到CSV缓存或API获取"""
    filepath = os.path.join(STOCK_DATA_DIR, f"{code}.parquet")
    if not os.path.exists(filepath):
        return None
    try:
        df = pd.read_parquet(filepath)
        return df
    except Exception:
        pass
    # 回退1: CSV缓存
    csv_path = os.path.join(STOCK_DATA_DIR, f"{code}.csv")
    if os.path.exists(csv_path):
        try:
            df = pd.read_csv(csv_path, parse_dates=["date"])
            for c in ["open", "close", "high", "low", "volume"]:
                if c in df.columns:
                    df[c] = pd.to_numeric(df[c], errors="coerce")
            return df.sort_values("date").reset_index(drop=True)
        except Exception:
            pass
    # 回退2: 腾讯API获取前复权日线(640条≈2.5年)
    try:
        import requests as _req
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,640,qfq"
        resp = _req.get(url, timeout=15)
        if resp.status_code == 200:
            raw = resp.json()
            if raw.get("code") == 0 and raw.get("data"):
                _d = raw["data"].get(code) or raw["data"].get(code.upper()) or {}
                bars = _d.get("qfqday") or _d.get("day") or []
                if bars:
                    rows = []
                    for b in bars:
                        rows.append({"date": b[0], "open": float(b[1]),
                                      "close": float(b[2]), "high": float(b[3]),
                                      "low": float(b[4]), "volume": float(b[5]) if len(b) > 5 else 0})
                    df = pd.DataFrame(rows)
                    df["date"] = pd.to_datetime(df["date"])
                    df = df.sort_values("date").reset_index(drop=True)
                    # 缓存为CSV供后续使用
                    try:
                        df.to_csv(csv_path, index=False)
                    except Exception:
                        pass
                    return df
    except Exception:
        pass
    return None

def load_market_states():
    """加载市场状态时间线"""
    with open(STATE_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def get_state_for_date(date_str, timeline):
    """获取某日的市场状态"""
    for entry in reversed(timeline):
        if entry['date'] <= date_str:
            return entry['state']
    return 'sideways'

# ============ 快速回测引擎 ============
def fast_backtest(df, params, initial_capital=500000, start_date=None, end_date=None):
    """
    快速向量化回测，忠实v5.3.4.1核心交易逻辑
    参数：
    - df: 股票K线数据 (date, open, high, low, close, volume)
    - params: 参数字典
    - start_date/end_date: 可选窗口，传入完整df（含窗口前预热历史），指标用全部数据计算，
      只统计入场日在窗口内的交易、权益曲线只含窗口内日期（2026-09-01修复：此前窗口回测
      会先截断df再计算指标，训练/验证段只有十几天K线，所有股票<60行被跳过→永远0交易）
    返回: dict(total_return, max_drawdown, sharpe, trades, win_rate, profit_factor)
    """
    if len(df) < 60:
        return None

    df = df.copy().reset_index(drop=True)
    n = len(df)
    # 窗口起止索引（指标计算用全部df；仅在窗口内开仓并统计窗口内权益）
    start_ts = pd.Timestamp(start_date) if start_date else None
    end_ts = pd.Timestamp(end_date) if end_date else None
    
    # === 1. 计算指标 ===
    vol_ratio_high = params['vol_ratio_high']
    vol_ratio_low = params['vol_ratio_low']
    trend_str_high = params['trend_str_high']
    trend_str_low = params['trend_str_low']
    ch_base = int(params['channel_base'])
    ex_base = int(params['exit_base'])
    min_score = params['min_entry_score']
    atr_mult = params['atr_multiplier']
    # 【自适应】新增3个参数（带默认值兼容旧params）
    trail_pct = params.get('trail_stop_pct', 0.05)
    risk_vban = params.get('risk_value_ban', 80)
    score_thresh = params.get('score_threshold', 2.0)
    # 【扩展】7个新增参数
    base_risk = params.get('base_risk_pct', 0.10)
    stop_mult_base = params.get('stop_multiplier_base', 2.0)
    tp_base = params.get('take_profit_base', 0.40)
    vol_pos_f = params.get('vol_pos_factor', 0.15)
    add_thresh_base = params.get('add_threshold_base', 0.06)
    
    # 波动率比率
    atr_pct = df["close"].diff().abs() / df["close"].shift(1)
    atr_pct_ma60 = atr_pct.rolling(60).mean()
    atr_pct_ma120 = atr_pct.rolling(120).mean()
    vol_ratio = atr_pct_ma60 / atr_pct_ma120
    
    # 动态Donchian通道
    ch_high_extra = max(ch_base + 5, 25)
    ch_low_extra = max(ch_base - 5, 15)
    
    df['dc_high_base'] = df['high'].rolling(ch_base).max().shift(1)
    df['dc_high_high'] = df['high'].rolling(ch_high_extra).max().shift(1)
    df['dc_high_low'] = df['high'].rolling(ch_low_extra).max().shift(1)
    
    df['dc_low_base'] = df['low'].rolling(ch_base).min().shift(1)
    df['dc_low_high'] = df['low'].rolling(ch_high_extra).min().shift(1)
    df['dc_low_low'] = df['low'].rolling(ch_low_extra).min().shift(1)
    
    # 动态选择通道
    high_vol = vol_ratio > vol_ratio_high
    low_vol = vol_ratio < vol_ratio_low
    normal_vol = ~high_vol & ~low_vol
    
    dc_high = pd.Series(np.nan, index=df.index)
    dc_low = pd.Series(np.nan, index=df.index)
    dc_high[high_vol] = df.loc[high_vol, 'dc_high_high']
    dc_low[high_vol] = df.loc[high_vol, 'dc_low_high']
    dc_high[low_vol] = df.loc[low_vol, 'dc_high_low']
    dc_low[low_vol] = df.loc[low_vol, 'dc_low_low']
    dc_high[normal_vol] = df.loc[normal_vol, 'dc_high_base']
    dc_low[normal_vol] = df.loc[normal_vol, 'dc_low_base']
    
    # 动态退出线
    ex_high = max(ex_base + 2, 12)
    ex_low = max(ex_base - 2, 8)
    
    df['exit_low_base'] = df['low'].rolling(ex_base).min().shift(1)
    df['exit_low_high'] = df['low'].rolling(ex_high).min().shift(1)
    df['exit_low_low'] = df['low'].rolling(ex_low).min().shift(1)
    
    trend_str = (df['close'] - df['close'].rolling(60).mean()) / df['close'].rolling(60).mean()
    strong_trend = trend_str > trend_str_high
    weak_trend = trend_str < trend_str_low
    normal_trend = ~strong_trend & ~weak_trend
    
    exit_low = pd.Series(np.nan, index=df.index)
    exit_low[strong_trend] = df.loc[strong_trend, 'exit_low_high']
    exit_low[weak_trend] = df.loc[weak_trend, 'exit_low_low']
    exit_low[normal_trend] = df.loc[normal_trend, 'exit_low_base']
    
    # ATR
    df['prev_close'] = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['prev_close']).abs(),
        (df['low'] - df['prev_close']).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(20).mean()
    
    # 辅助指标
    df['ma20'] = df['close'].rolling(20).mean()
    df['ma60'] = df['close'].rolling(60).mean()
    df['ma20_slope'] = (df['ma20'] - df['ma20'].shift(5)) / df['ma20'].shift(5)
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_hist = 2 * (dif - dea)
    df['vol_ma20'] = df['volume'].rolling(20).mean()
    df['vol_q75'] = df['volume'].rolling(20).quantile(0.75)
    
    # 【自适应】风险值（Williams %R变体，借鉴中和应泰强龙风控）
    llv_34 = df['low'].rolling(34).min()
    hhv_34 = df['high'].rolling(34).max()
    raw_risk = 100 * (df['close'] - llv_34) / (hhv_34 - llv_34 + 1e-10)
    df['risk_value'] = raw_risk.ewm(span=3, adjust=False).mean()
    
    # 【新增】vol_stack_ratio 计算（与calc_indicators_vec一致）
    _vs_r20 = df['volume'] / df['volume'].rolling(20).mean()
    _vs_r60 = df['volume'] / df['volume'].rolling(60).mean()
    vol_stack_ratio = np.maximum(_vs_r20.fillna(0), _vs_r60.fillna(0))
    
    # 【新增】按市值分档确定vol_stack阈值
    _mc_est = estimate_market_cap(df['close'].iloc[0])
    if _mc_est >= 300:
        _vs_threshold = params.get('vol_stack_threshold_large', 3.0)
    elif _mc_est >= 100:
        _vs_threshold = params.get('vol_stack_threshold_mid', 5.0)
    else:
        _vs_threshold = params.get('vol_stack_threshold_small', 8.0)
    _vs_penalty = _vs_threshold * 0.3
    
    # === 2. 信号生成 ===
    df['dc_high'] = dc_high
    df['dc_low'] = dc_low
    df['exit_low'] = exit_low
    df['atr'] = atr
    
    buy_signals = np.zeros(n, dtype=bool)
    sell_signals = np.zeros(n, dtype=bool)
    entry_scores = np.full(n, 3.0)  # 默认评分3.0（正常仓位）
    
    for i in range(60, n):
        # 买入信号：突破Donchian通道 + 评分
        breakout = (not pd.isna(dc_high.iloc[i])) and (df.loc[i, 'close'] > dc_high.iloc[i])
        vol_ok = df.loc[i, 'volume'] > df.loc[i, 'vol_q75']
        ma20_slope = df.loc[i, 'ma20_slope'] if not pd.isna(df.loc[i, 'ma20_slope']) else 0
        price_above_ma60 = df.loc[i, 'close'] > df.loc[i, 'ma60']
        trend_ok = (ma20_slope > 0.001) and price_above_ma60
        money_ok = (macd_hist.iloc[i] > 0) and (macd_hist.iloc[i] > macd_hist.iloc[i-3]) if i >= 3 else False
        
        score = 0
        if breakout: score += 1
        if vol_ok: score += 1
        if trend_ok: score += 0.5
        if money_ok: score += 0.5
        # 【新增】vol_stack评分（按市值分档阈值）
        _vs_val = vol_stack_ratio.iloc[i] if i < len(vol_stack_ratio) else 1.0
        if _vs_val > _vs_threshold: score += 1.0
        if _vs_val < _vs_penalty: score -= 0.5
        
        # 【自适应】风险值过滤
        risk_val = df.loc[i, 'risk_value'] if not pd.isna(df.loc[i, 'risk_value']) else 50
        if risk_val > risk_vban:
            trend_ok = False  # 风险值超限禁止买入
        
        entry_scores[i] = score
        
        if score >= min_score and trend_ok:
            buy_signals[i] = True
        
        # 卖出信号：跌破退出线
        if (not pd.isna(exit_low.iloc[i])) and (df.loc[i, 'close'] < exit_low.iloc[i]):
            sell_signals[i] = True
    
    # === 3. 模拟交易 ===
    position = 0  # 持有股数
    entry_price = 0
    stop_loss = 0
    entry_idx = 0  # 记录买入时的索引，用于追踪止盈
    capital = initial_capital
    trades = []
    equity_curve = []
    
    for i in range(n):
        price = df.loc[i, 'close']
        cur_date = df.loc[i, 'date']
        # 窗口判断：指标已用全部历史算好，这里只限制交易/统计窗口
        in_window = True
        if start_ts is not None and cur_date < start_ts:
            in_window = False
        if end_ts is not None and cur_date > end_ts:
            in_window = False

        # 【自适应】追踪止盈：浮盈后上移止损线（持仓中始终执行，含跨窗口持仓）
        if position > 0:
            pnl_check = (price - entry_price) / entry_price
            trail_high_pct = trail_pct * 3
            if pnl_check > trail_high_pct and i >= 3:
                recent_low = df.iloc[i-3:i]['low'].min()
                trail_stop = recent_low * 0.99
                if trail_stop > stop_loss:
                    stop_loss = trail_stop
            elif pnl_check > trail_pct and i >= 5:
                recent_low = df.iloc[i-5:i]['low'].min()
                trail_stop = recent_low * 0.98
                if trail_stop > stop_loss:
                    stop_loss = trail_stop

        # 止损检查
        if position > 0 and price < stop_loss:
            sell_price = stop_loss
            pnl = (sell_price - entry_price) * position
            capital += pnl
            trades.append({'entry': entry_price, 'exit': sell_price, 'pnl': pnl, 'type': 'stop',
                           'entry_date': df.loc[entry_idx, 'date'], 'exit_date': cur_date})
            position = 0

        # 卖出信号
        elif position > 0 and sell_signals[i]:
            sell_price = price
            pnl = (sell_price - entry_price) * position
            capital += pnl
            trades.append({'entry': entry_price, 'exit': sell_price, 'pnl': pnl, 'type': 'signal',
                           'entry_date': df.loc[entry_idx, 'date'], 'exit_date': cur_date})
            position = 0

        # 买入信号：仅在统计窗口内开仓（窗口外保留指标预热，不交易）
        elif position == 0 and buy_signals[i] and in_window:
            atr_val = atr.iloc[i] if not pd.isna(atr.iloc[i]) else price * 0.03
            # 【新增】波动率过滤：ATR占股价比过低跳过
            _min_atr_pct_fb = params.get('min_atr_pct', 0.015)
            if atr_val / price < _min_atr_pct_fb:
                continue
            risk_per_share = atr_val * atr_mult * (stop_mult_base / 2.0)  # 止损=ATR×倍数×状态基准
            if risk_per_share <= 0:
                continue
            # 【自适应】信号置信度仓位调整
            pos_factor = 1.0
            if entry_scores[i] < score_thresh:
                pos_factor = 0.5  # 边际信号半仓
            elif entry_scores[i] < score_thresh + 1.0:
                pos_factor = 0.8  # 中等信号八成仓
            # 【扩展】使用base_risk_pct + vol_pos_factor动态仓位
            vol_idx = atr_val / (atr.rolling(20).mean().iloc[i] if i >= 20 else atr_val)
            vol_reduce = 1.0 - vol_pos_f * min(1.0, max(0.0, (vol_idx - 0.8) / 0.7))
            max_shares = int(capital * base_risk * vol_reduce / risk_per_share)
            if max_shares < 100:
                max_shares = 100
            max_shares = min(max_shares, int(capital * 0.5 * pos_factor / price))  # 最大仓位×置信度
            if max_shares < 100:
                continue
            position = (max_shares // 100) * 100
            if position < 100:
                continue
            entry_price = price
            entry_idx = i
            stop_loss = price - risk_per_share
        
        # 记录权益：仅窗口内日期参与绩效统计
        if in_window:
            equity = capital + position * price
            equity_curve.append({'date': cur_date, 'equity': equity})

    # 只统计入场日落在窗口内的交易（预热期不开仓，这里是双保险）
    def _trade_in_window(t):
        ed = t.get('entry_date')
        if ed is None:
            return True
        if start_ts is not None and ed < start_ts:
            return False
        if end_ts is not None and ed > end_ts:
            return False
        return True
    trades = [t for t in trades if _trade_in_window(t)]

    # === 4. 计算指标 ===
    if not trades or not equity_curve:
        return {
            'total_return': 0, 'max_drawdown': 0, 'sharpe': 0,
            'trades': 0, 'win_rate': 0, 'profit_factor': 0
        }
    
    eq_df = pd.DataFrame(equity_curve)
    eq_df['daily_ret'] = eq_df['equity'].pct_change()
    
    total_return = (eq_df['equity'].iloc[-1] / initial_capital - 1) * 100
    
    # 最大回撤
    eq_df['peak'] = eq_df['equity'].cummax()
    eq_df['drawdown'] = (eq_df['equity'] / eq_df['peak'] - 1) * 100
    max_drawdown = eq_df['drawdown'].min()
    
    # 夏普比率（年化）
    daily_ret = eq_df['daily_ret'].dropna()
    if len(daily_ret) > 20 and daily_ret.std() > 0:
        sharpe = daily_ret.mean() / daily_ret.std() * np.sqrt(250)
    else:
        sharpe = 0
    
    # 胜率
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    
    # 盈亏比
    total_win = sum(t['pnl'] for t in wins)
    total_loss = abs(sum(t['pnl'] for t in losses))
    profit_factor = total_win / total_loss if total_loss > 0 else 999
    
    return {
        'total_return': round(total_return, 2),
        'max_drawdown': round(max_drawdown, 2),
        'sharpe': round(sharpe, 3),
        'trades': len(trades),
        'win_rate': round(win_rate, 1),
        'profit_factor': round(profit_factor, 2)
    }

# ============ Walk-Forward 验证 ============
def walk_forward_split(df, train_pct=0.6, val_pct=0.2):
    """将数据分为训练/验证/测试三段"""
    n = len(df)
    train_end = int(n * train_pct)
    val_end = int(n * (train_pct + val_pct))
    
    train = df.iloc[:train_end].copy()
    val = df.iloc[train_end:val_end].copy()
    test = df.iloc[val_end:].copy()
    
    return train, val, test

# ============ 多股回测 ===⣿⣿⣿========
def backtest_multi_stocks(codes, params, data_dict=None, start_date=None, end_date=None):
    """对多只股票回测，返回聚合指标"""
    all_returns = []
    all_trades = 0
    all_drawdowns = []
    weighted_wins = 0.0

    for code in codes:
        if data_dict and code in data_dict:
            df = data_dict[code].copy()
        else:
            df = load_stock_data(code)
            if df is None:
                continue

        # 注意：不预先按窗口截断df——指标（MA60/ATR120/通道等）需要窗口前的预热历史。
        # 把完整df和窗口起止传给fast_backtest，由回测引擎内部限制交易/统计窗口。
        # （2026-09-01修复：此前先截断df再算指标，训练/验证段只有十几天K线，全部<60行被跳过→0交易）
        if len(df) < 60:
            continue

        result = fast_backtest(df, params, start_date=start_date, end_date=end_date)
        if result is None or result['trades'] == 0:
            continue

        all_returns.append(result['total_return'])
        all_trades += result['trades']
        all_drawdowns.append(result['max_drawdown'])
        weighted_wins += result.get('win_rate', 0) * result['trades'] / 100.0
    
    if not all_returns:
        return {'sharpe': -10, 'total_return': 0, 'trades': 0, 'max_drawdown': 0, 'win_rate': 0}

    avg_return = np.mean(all_returns)
    avg_drawdown = np.mean(all_drawdowns)
    return_std = np.std(all_returns) if len(all_returns) > 1 else 1

    sharpe = avg_return / return_std if return_std > 0 else 0

    return {
        'sharpe': round(sharpe, 3),
        'total_return': round(avg_return, 2),
        'trades': all_trades,
        'max_drawdown': round(avg_drawdown, 2),
        'win_rate': round(weighted_wins / all_trades * 100, 1) if all_trades else 0,
        'n_stocks': len(all_returns)
    }

# ============ Optuna 优化 ============
def optimize_for_state(state_name, all_codes, data_dict, state_dates, n_trials=50):
    """
    对单个市场状态优化参数
    state_dates: 该状态所有日期列表 ['2023-01-03', ...]
    """
    print(f"\n{'='*60}")
    print(f"优化市场状态: {state_name} (共{len(state_dates)}天)")
    print(f"{'='*60}")

    # 使用数据驱动的状态专属基准（替代全局BASELINE_PARAMS）
    bp = STATE_BASELINES.get(state_name, BASELINE_PARAMS)

    # 按时间60/40分割为训练段和验证段
    mid_idx = int(len(state_dates) * 0.6)
    train_start = state_dates[0]
    train_end = state_dates[mid_idx - 1]
    val_start = state_dates[mid_idx]
    val_end = state_dates[-1]
    print(f"训练段: {train_start} ~ {train_end} ({mid_idx}天)")
    print(f"验证段: {val_start} ~ {val_end} ({len(state_dates)-mid_idx}天)")

    def objective(trial):
        params = {
            'vol_ratio_high': trial.suggest_float('vol_ratio_high',
                bp['vol_ratio_high'] * 0.7,
                bp['vol_ratio_high'] * 1.3),
            'vol_ratio_low': trial.suggest_float('vol_ratio_low',
                bp['vol_ratio_low'] * 0.7,
                bp['vol_ratio_low'] * 1.3),
            'trend_str_high': trial.suggest_float('trend_str_high',
                bp['trend_str_high'] * 0.7,
                bp['trend_str_high'] * 1.3),
            'trend_str_low': trial.suggest_float('trend_str_low',
                bp['trend_str_low'] * 1.3,
                bp['trend_str_low'] * 0.7),
            'min_entry_score': trial.suggest_float('min_entry_score',
                max(0.5, bp['min_entry_score'] * 0.7),
                bp['min_entry_score'] * 1.3),
            'atr_multiplier': trial.suggest_float('atr_multiplier',
                bp['atr_multiplier'] * 0.7,
                bp['atr_multiplier'] * 1.3),
            'channel_base': trial.suggest_int('channel_base',
                max(15, int(bp['channel_base'] * 0.85)),
                int(bp['channel_base'] * 1.15)),
            'exit_base': trial.suggest_int('exit_base',
                max(7, int(bp['exit_base'] * 0.85)),
                int(bp['exit_base'] * 1.15)),
            # 【自适应】新增3个可优化参数（±30%范围）
            'trail_stop_pct': trial.suggest_float('trail_stop_pct',
                bp['trail_stop_pct'] * 0.7,
                bp['trail_stop_pct'] * 1.3),
            'risk_value_ban': trial.suggest_int('risk_value_ban',
                max(60, int(bp['risk_value_ban'] * 0.85)),
                min(95, int(bp['risk_value_ban'] * 1.15))),
            'score_threshold': trial.suggest_float('score_threshold',
                bp['score_threshold'] * 0.7,
                bp['score_threshold'] * 1.3),
            # 【扩展】7个新增自适应参数（±30%范围）
            'base_risk_pct': trial.suggest_float('base_risk_pct',
                bp['base_risk_pct'] * 0.7,
                bp['base_risk_pct'] * 1.3),
            'max_concurrent_positions': trial.suggest_int('max_concurrent_positions',
                max(2, int(bp['max_concurrent_positions'] * 0.7)),
                int(bp['max_concurrent_positions'] * 1.3)),
            'take_profit_base': trial.suggest_float('take_profit_base',
                bp['take_profit_base'] * 0.7,
                bp['take_profit_base'] * 1.3),
            'stop_multiplier_base': trial.suggest_float('stop_multiplier_base',
                bp['stop_multiplier_base'] * 0.7,
                bp['stop_multiplier_base'] * 1.3),
            'add_threshold_base': trial.suggest_float('add_threshold_base',
                bp['add_threshold_base'] * 0.7,
                bp['add_threshold_base'] * 1.3),
            'pre_filter_threshold': trial.suggest_int('pre_filter_threshold',
                max(20, int(bp['pre_filter_threshold'] * 0.7)),
                int(bp['pre_filter_threshold'] * 1.3)),
            'vol_pos_factor': trial.suggest_float('vol_pos_factor',
                bp['vol_pos_factor'] * 0.7,
                bp['vol_pos_factor'] * 1.3),
            # 【新增】vol_stack按市值分档阈值（独立范围，不按基准±30%）
            'vol_stack_threshold_large': trial.suggest_float('vol_stack_threshold_large', 2.0, 5.0),
            'vol_stack_threshold_mid': trial.suggest_float('vol_stack_threshold_mid', 4.0, 8.0),
            'vol_stack_threshold_small': trial.suggest_float('vol_stack_threshold_small', 6.0, 12.0),
        }

        # 在训练时间段内回测
        train_result = backtest_multi_stocks(all_codes, params, data_dict,
                                              start_date=train_start, end_date=train_end)

        if train_result is None or train_result['trades'] < 10:
            return -10

        penalty = 0
        if train_result['max_drawdown'] < -25:
            penalty = -2

        return train_result['sharpe'] + penalty

    if not _HAS_OPTUNA:
        raise RuntimeError('optuna未安装，无法执行参数优化。请 pip install optuna')
    study = optuna.create_study(direction='maximize', sampler=optuna.samplers.TPESampler(seed=42))
    start_time = time.time()
    study.optimize(objective, n_trials=n_trials, show_progress_bar=False)
    elapsed = time.time() - start_time

    best_params = study.best_params
    best_train_sharpe = study.best_value

    print(f"训练完成 ({elapsed:.0f}秒, {n_trials}轮)")
    print(f"最佳训练夏普: {best_train_sharpe:.3f}")

    # 验证集检验（不同时间段）
    val_result = backtest_multi_stocks(all_codes, best_params, data_dict,
                                        start_date=val_start, end_date=val_end)
    if val_result is None:
        val_result = {'sharpe': 0, 'total_return': 0, 'trades': 0, 'max_drawdown': 0}
    print(f"验证集: 夏普={val_result['sharpe']}, 收益={val_result['total_return']}%, "
          f"交易={val_result['trades']}, 回撤={val_result['max_drawdown']}%")

    baseline_val = backtest_multi_stocks(all_codes, bp, data_dict,
                                          start_date=val_start, end_date=val_end)
    if baseline_val is None:
        baseline_val = {'sharpe': 0, 'total_return': 0, 'trades': 0, 'max_drawdown': 0}
    print(f"基准验证: 夏普={baseline_val['sharpe']}, 收益={baseline_val['total_return']}%, "
          f"交易={baseline_val['trades']}, 回撤={baseline_val['max_drawdown']}%")

    # 采纳判断：优化后夏普优于或接近基准
    if baseline_val['sharpe'] <= 0:
        # 基准为负，优化后为正则采纳，否则比较绝对值
        improvement = 1.5 if val_result['sharpe'] > 0 else 0.0
    else:
        improvement = val_result['sharpe'] / baseline_val['sharpe']

    if improvement >= 0.8:
        status = 'adopted'
        print(f"✅ 验证通过（夏普比值: {improvement:.2f}），采纳新参数")
    else:
        status = 'rejected'
        best_params = bp.copy()
        print(f"⚠️ 验证未通过（夏普比值: {improvement:.2f}），保留基准参数")

    return {
        'state': state_name,
        'params': best_params,
        'train_sharpe': round(best_train_sharpe, 3),
        'val_sharpe': val_result['sharpe'],
        'val_return': val_result['total_return'],
        'val_drawdown': val_result['max_drawdown'],
        'val_trades': val_result['trades'],
        'baseline_val_sharpe': baseline_val['sharpe'],
        'baseline_val_return': baseline_val['total_return'],
        'baseline_val_drawdown': baseline_val['max_drawdown'],
        'baseline_val_trades': baseline_val['trades'],
        'baseline_val_win_rate': baseline_val.get('win_rate', 0),
        'val_win_rate': val_result.get('win_rate', 0),
        'status': status,
        'train_period': f"{train_start}~{train_end}",
        'val_period': f"{val_start}~{val_end}",
        'optimization_time': round(elapsed, 1)
    }

# ============ 主流程 ============
def run_optimization(stock_codes=None, n_trials=50, test_mode=False, n_stocks=200,
                     output_path=None, partial_path=None, states=None, keep_partial=False):
    """
    完整优化流程
    1. 加载数据
    2. 按市场状态分别提取时间段
    3. Walk-forward训练/验证分离
    4. Optuna贝叶斯优化
    5. 保存最优参数
    n_stocks: 正式模式时从全部股票中采样的数量
    states: 只优化指定状态子集（如['bull','bear']），None=全部4状态；每日候选轮换用
    keep_partial: True时结束后保留断点文件，供跨天累积未完成的状态（由调用方在凑齐后清理）
    """
    print("=" * 60)
    print("龟缠安泰 v5.3.4.1 自适应参数优化引擎")
    print("=" * 60)

    # 加载股票代码
    if stock_codes is None:
        all_codes = get_all_local_codes()
        if test_mode:
            stock_codes = all_codes[:20]
        else:
            import random
            random.seed(42)
            if len(all_codes) > n_stocks:
                stock_codes = random.sample(all_codes, n_stocks)
            else:
                stock_codes = all_codes
            print(f"从{len(all_codes)}只股票中随机采样{len(stock_codes)}只")
    else:
        all_codes = stock_codes

    print(f"使用 {len(stock_codes)} 只股票进行优化")

    # 预加载所有数据（过滤退市/停牌股：最新数据必须在30天内）
    print("预加载数据...")
    from datetime import timedelta
    cutoff_date = pd.Timestamp.now() - pd.Timedelta(days=30)
    data_dict = {}
    skipped_stale = 0
    for code in stock_codes:
        df = load_stock_data(code)
        # 本地parquet可能只有150根（每日扫描persist截断），回测需要更长历史；
        # 在线补齐640根（≈2.5年），含walk-forward训练/验证所需的全部窗口（2026-09-01修复）
        if df is not None and len(df) < 300:
            try:
                online_df = _tencent_kline_multiep(code, count=640, fq="qfq")
                if online_df is not None and len(online_df) >= 300:
                    df = online_df
                    # 回填结果合并回本地parquet：后续每天的优化/扫描直接读本地，
                    # 不再为同一批股票重复在线拉取640根（2026-09-09修复每日优化超时的主要耗时来源）
                    _cache_backfilled_kline(code, online_df)
            except Exception:
                pass
        if df is not None and len(df) >= 120:
            if df['date'].max() < cutoff_date:
                skipped_stale += 1
                continue
            data_dict[code] = df
    print(f"成功加载 {len(data_dict)} 只股票数据" + (f" (跳过{skipped_stale}只过期数据)" if skipped_stale else ""))

    if len(data_dict) < 3:
        print("⚠️ 数据不足3只，无法优化")
        return None

    all_codes_list = list(data_dict.keys())

    # 确定股票数据的日期范围
    min_data_date = min(df['date'].min() for df in data_dict.values()).strftime('%Y-%m-%d')
    max_data_date = max(df['date'].max() for df in data_dict.values()).strftime('%Y-%m-%d')
    print(f"股票数据范围: {min_data_date} ~ {max_data_date}")

    # 加载市场状态
    timeline = load_market_states()

    # 按状态分别优化（states允许调用方只优化子集，如每日轮换只跑2个状态）
    all_states = ['bull', 'bear', 'sideways', 'transition']
    if states:
        states = [s for s in states if s in all_states] or all_states
        if len(states) < len(all_states):
            print(f"状态子集模式：本次只优化 {states}（其余状态由断点续跑/后续日期补齐）")
    else:
        states = all_states
    
    # 断点续跑：加载已有结果
    partial_file = partial_path or os.path.join(DATA_DIR, "optimization_partial.json")
    results = []
    completed_states = set()
    if os.path.exists(partial_file):
        try:
            with open(partial_file, 'r') as f:
                partial = json.load(f)
            results = partial.get('results', [])
            completed_states = {r['state'] for r in results}
            if completed_states:
                print(f"发现断点续跑数据，已完成状态: {completed_states}")
        except:
            pass

    for state in states:
        if state in completed_states:
            print(f"\n{state} 已完成，跳过")
            continue
            
        # 找到该状态对应的所有日期（只保留与股票数据重叠的日期）
        state_dates = [t['date'] for t in timeline
                       if t['state'] == state and t['date'] >= min_data_date and t['date'] <= max_data_date]
        if len(state_dates) < 30:
            print(f"\n{state} 状态仅{len(state_dates)}天，数据不足，跳过")
            continue

        result = optimize_for_state(
            state,
            all_codes_list,
            data_dict,
            state_dates,
            n_trials=n_trials if not test_mode else 10
        )
        results.append(result)
        
        # 每个状态完成后立即保存（断点续跑）
        partial_output = {
            'optimization_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'baseline_params': BASELINE_PARAMS,
            'results': results.copy(),
            'n_stocks': len(data_dict),
            'n_trials': n_trials
        }
        Path(partial_file).parent.mkdir(parents=True, exist_ok=True)
        with open(partial_file, 'w', encoding='utf-8') as f:
            json.dump(partial_output, f, ensure_ascii=False, indent=2)
        print(f"  → 已保存断点（{state}完成）")

    # 保存结果
    output = {
        'optimization_time': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'baseline_params': BASELINE_PARAMS,
        'results': results,
        'n_stocks': len(data_dict),
        'n_trials': n_trials
    }

    final_output_path = output_path or PARAMS_FILE
    os.makedirs(os.path.dirname(final_output_path) or '.', exist_ok=True)
    with open(final_output_path, 'w', encoding='utf-8') as f:
        json.dump(output, f, ensure_ascii=False, indent=2)
    
    # 清理断点文件（keep_partial=True时保留给调用方跨天累积，凑齐全状态后由调用方清理）
    if os.path.exists(partial_file):
        if keep_partial:
            done_states = sorted({r['state'] for r in results})
            print(f"断点已保留（keep_partial）：已完成状态 {done_states}，未凑齐前不清理")
        else:
            os.remove(partial_file)
            print(f"最优参数已保存到 {final_output_path}（断点文件已清理）")
    else:
        print(f"\n最优参数已保存到 {final_output_path}")

    # 打印汇总
    print(f"\n{'='*60}")
    print("优化汇总")
    print(f"{'='*60}")
    print(f"{'状态':12s} {'训练夏普':>8s} {'验证夏普':>8s} {'基准夏普':>8s} {'验证收益':>8s} {'验证回撤':>8s} {'采纳':6s}")
    for r in results:
        print(f"{r['state']:12s} {r['train_sharpe']:>8.3f} {r['val_sharpe']:>8.3f} "
              f"{r['baseline_val_sharpe']:>8.3f} {r['val_return']:>7.1f}% {r['val_drawdown']:>7.1f}% {r['status']:6s}")

    return output

# ============ 主程序 ============


# ============================================================
# 在线选择器模块（原 online_selector.py）
# 在线参数选择器 - 自适应参数优化系统第四步
# 功能：
# 1. 识别当前市场状态（调用market_state.py）
# 2. 从最优参数表中查找对应状态的参数
# 3. 用选中的参数运行v5.3.4.1回测逻辑
# 4. 生成买卖信号并输出
# 5. 升级每日扫描脚本，实现自适应参数
# 
# 每日收盘后运行：识别状态→选参数→出信号
# ============================================================
#!/usr/bin/env python3
"""
在线参数选择器 - 自适应参数优化系统第四步
功能：
1. 识别当前市场状态（调用market_state.py）
2. 从最优参数表中查找对应状态的参数
3. 用选中的参数运行v5.3.4.1回测逻辑
4. 生成买卖信号并输出
5. 升级每日扫描脚本，实现自适应参数

每日收盘后运行：识别状态→选参数→出信号
"""


# ============ 路径配置 ============
# 【修改】删除重复硬编码，复用文件开头从配置加载的全局变量
# BASE_DIR / DATA_DIR / PARAMS_FILE / STATE_FILE / STOCK_DATA_DIR / SIGNAL_LOG 已在文件开头从 config/settings.yaml 加载

# 添加路径以导入其他模块
sys.path.insert(0, BASE_DIR)

# ============ 参数选择 ============
def load_optimal_params():
    """加载优化后的参数表"""
    if not os.path.exists(PARAMS_FILE):
        print("⚠️ 未找到优化参数文件，使用基准参数")
        return {'results': [], 'baseline_params': BASELINE_PARAMS}
    
    with open(PARAMS_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)

def select_params(state=None):
    """
    根据市场状态选择最优参数
    state: None则自动检测当前状态
    返回: (params_dict, source_str)
    """
    data = load_optimal_params()
    
    if state is None:
        state_info = get_state()
        state = state_info['state']
        state_desc = state_info.get('description', '')
    else:
        state_info = get_state()
        state_desc = state_info.get('description', '')
    
    # 查找该状态的优化参数
    for result in data.get('results', []):
        if result['state'] == state and result['status'] == 'adopted':
            params = result['params']
            # 补全可能的缺失字段
            for k, v in BASELINE_PARAMS.items():
                if k not in params:
                    params[k] = v
            return params, f"优化参数({state}态, 训练夏普{result['train_sharpe']})"
    
    # 没有该状态的优化参数，用状态专属基准（pre_filter=60，符合统一60分门槛；
    # 旧代码回退v5默认BASELINE_PARAMS(pre_filter=40)会绕过门槛，2026-09-01修复）
    fallback = dict(BASELINE_PARAMS)
    fallback.update(STATE_BASELINES.get(state, {}))
    return fallback, f"状态基准参数({state}态无优化记录)"

# ============ 信号生成 ============
def generate_signals_for_stock(code, params, df=None):
    """
    为单只股票生成交易信号
    返回: dict(code, signal, price, params_used, details)
    """
    if df is None:
        df = load_stock_data(code)
    if df is None or len(df) < 60:
        return {'code': code, 'signal': 'NO_DATA', 'price': 0}
    
    df = df.copy().reset_index(drop=True)
    n = len(df)
    
    # 计算指标（与fast_backtest相同的逻辑）
    vol_ratio_high = params['vol_ratio_high']
    vol_ratio_low = params['vol_ratio_low']
    trend_str_high = params['trend_str_high']
    trend_str_low = params['trend_str_low']
    ch_base = int(params['channel_base'])
    ex_base = int(params['exit_base'])
    min_score = params['min_entry_score']
    atr_mult = params['atr_multiplier']
    
    # 波动率
    atr_pct = df["close"].diff().abs() / df["close"].shift(1)
    atr_pct_ma60 = atr_pct.rolling(60).mean()
    atr_pct_ma120 = atr_pct.rolling(120).mean()
    vol_ratio = atr_pct_ma60 / atr_pct_ma120
    
    # Donchian通道
    ch_high = max(ch_base + 5, 25)
    ch_low = max(ch_base - 5, 15)
    
    dc_high_base = df['high'].rolling(ch_base).max().shift(1)
    dc_high_high = df['high'].rolling(ch_high).max().shift(1)
    dc_high_low = df['high'].rolling(ch_low).max().shift(1)
    
    high_vol = vol_ratio > vol_ratio_high
    low_vol = vol_ratio < vol_ratio_low
    normal_vol = ~high_vol & ~low_vol
    
    dc_high = pd.Series(np.nan, index=df.index)
    dc_high[high_vol] = dc_high_high[high_vol]
    dc_high[low_vol] = dc_high_low[low_vol]
    dc_high[normal_vol] = dc_high_base[normal_vol]
    
    # 退出线
    ex_high = max(ex_base + 2, 12)
    ex_low = max(ex_base - 2, 8)
    
    exit_low_base = df['low'].rolling(ex_base).min().shift(1)
    exit_low_high = df['low'].rolling(ex_high).min().shift(1)
    exit_low_low = df['low'].rolling(ex_low).min().shift(1)
    
    trend_str = (df['close'] - df['close'].rolling(60).mean()) / df['close'].rolling(60).mean()
    strong_trend = trend_str > trend_str_high
    weak_trend = trend_str < trend_str_low
    normal_trend = ~strong_trend & ~weak_trend
    
    exit_low = pd.Series(np.nan, index=df.index)
    exit_low[strong_trend] = exit_low_high[strong_trend]
    exit_low[weak_trend] = exit_low_low[weak_trend]
    exit_low[normal_trend] = exit_low_base[normal_trend]
    
    # ATR
    df['prev_close'] = df['close'].shift(1)
    tr = pd.concat([
        df['high'] - df['low'],
        (df['high'] - df['prev_close']).abs(),
        (df['low'] - df['prev_close']).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(20).mean()
    
    # 辅助指标
    ma20 = df['close'].rolling(20).mean()
    ma60 = df['close'].rolling(60).mean()
    ma20_slope = (ma20 - ma20.shift(5)) / ma20.shift(5)
    ema12 = df['close'].ewm(span=12, adjust=False).mean()
    ema26 = df['close'].ewm(span=26, adjust=False).mean()
    dif = ema12 - ema26
    dea = dif.ewm(span=9, adjust=False).mean()
    macd_hist = 2 * (dif - dea)
    vol_q75 = df['volume'].rolling(20).quantile(0.75)
    
    # 最新一根K线
    i = n - 1
    price = df.loc[i, 'close']
    
    # 检查买入信号
    breakout = (not pd.isna(dc_high.iloc[i])) and (price > dc_high.iloc[i])
    vol_ok = df.loc[i, 'volume'] > vol_q75.iloc[i]
    slope = ma20_slope.iloc[i] if not pd.isna(ma20_slope.iloc[i]) else 0
    price_above_ma60 = price > ma60.iloc[i]
    trend_ok = (slope > 0.001) and price_above_ma60
    money_ok = (macd_hist.iloc[i] > 0) and (macd_hist.iloc[i] > macd_hist.iloc[i-3]) if i >= 3 else False
    
    score = 0
    if breakout: score += 1
    if vol_ok: score += 1
    if trend_ok: score += 0.5
    if money_ok: score += 0.5
    
    buy_signal = score >= min_score and trend_ok
    
    # 检查卖出信号
    sell_signal = (not pd.isna(exit_low.iloc[i])) and (price < exit_low.iloc[i])
    
    # 通道位
    dc_high_val = dc_high.iloc[i] if not pd.isna(dc_high.iloc[i]) else 0
    exit_low_val = exit_low.iloc[i] if not pd.isna(exit_low.iloc[i]) else 0
    atr_val = atr.iloc[i] if not pd.isna(atr.iloc[i]) else 0
    
    # 波动率状态
    vol_r = vol_ratio.iloc[i] if not pd.isna(vol_ratio.iloc[i]) else 1.0
    if vol_r > vol_ratio_high:
        vol_state = "高波动(25日通道)"
    elif vol_r < vol_ratio_low:
        vol_state = "低波动(15日通道)"
    else:
        vol_state = "正常(20日通道)"
    
    # 趋势状态
    ts = trend_str.iloc[i] if not pd.isna(trend_str.iloc[i]) else 0
    if ts > trend_str_high:
        trend_state = "强趋势(12日退出)"
    elif ts < trend_str_low:
        trend_state = "弱趋势(8日退出)"
    else:
        trend_state = "正常(10日退出)"
    
    # 信号判断
    if buy_signal:
        signal = 'BUY'
        stop_loss = price - atr_val * atr_mult if atr_val > 0 else price * 0.95
        result = {
            'code': code,
            'signal': signal,
            'price': round(float(price), 2),
            'score': score,
            'min_score': min_score,
            'dc_high': round(float(dc_high_val), 2),
            'exit_low': round(float(exit_low_val), 2),
            'atr': round(float(atr_val), 4),
            'stop_loss': round(float(stop_loss), 2),
            'vol_state': vol_state,
            'trend_state': trend_state,
            'breakout': breakout,
            'trend_ok': trend_ok,
            'money_ok': money_ok,
            'params_used': params,
        }
    elif sell_signal:
        signal = 'SELL'
        result = {
            'code': code,
            'signal': signal,
            'price': round(float(price), 2),
            'exit_low': round(float(exit_low_val), 2),
            'vol_state': vol_state,
            'trend_state': trend_state,
            'params_used': params,
        }
    else:
        signal = 'HOLD'
        result = {
            'code': code,
            'signal': signal,
            'price': round(float(price), 2),
            'dc_high': round(float(dc_high_val), 2),
            'exit_low': round(float(exit_low_val), 2),
            'score': score,
            'min_score': min_score,
            'vol_state': vol_state,
            'trend_state': trend_state,
            'distance_to_breakout': round(float((dc_high_val - price) / price * 100), 2) if dc_high_val > 0 else None,
            'params_used': params,
        }
    
    return result

def scan_market(codes=None, top_n=20):
    """
    扫描市场，生成信号报告
    codes: 股票代码列表，None则用全部本地数据
    top_n: 返回前N只信号
    """
    # 1. 识别当前市场状态
    state_info = get_state()
    state = state_info['state']
    print(f"当前市场状态: {state} - {state_info['description']}")
    print(f"  R²={state_info['r2_60']}, 偏离={state_info['deviation']}, 斜率={state_info['ma60_slope']}")
    
    # 2. 选择参数
    params, param_source = select_params(state)
    print(f"使用参数: {param_source}")
    print(f"  vol_ratio: {params['vol_ratio_high']}/{params['vol_ratio_low']}")
    print(f"  trend_str: {params['trend_str_high']}/{params['trend_str_low']}")
    print(f"  min_score: {params['min_entry_score']}")
    print(f"  atr_mult: {params['atr_multiplier']}")
    print(f"  channel: {params['channel_base']}, exit: {params['exit_base']}")
    
    # 3. 加载股票代码
    if codes is None:
        codes = get_all_local_codes()
    
    print(f"\n扫描 {len(codes)} 只股票...")
    
    # 4. 生成信号
    buy_signals = []
    sell_signals = []
    hold_signals = []
    
    for code in codes:
        try:
            result = generate_signals_for_stock(code, params)
            if result['signal'] == 'BUY':
                buy_signals.append(result)
            elif result['signal'] == 'SELL':
                sell_signals.append(result)
            else:
                hold_signals.append(result)
        except Exception as e:
            pass  # 静默跳过错误
    
    # 5. 排序
    buy_signals.sort(key=lambda x: x.get('score', 0), reverse=True)
    
    # 6. 输出报告
    print(f"\n{'='*60}")
    print(f"扫描完成: {len(codes)}只 → 买入{len(buy_signals)} | 卖出{len(sell_signals)} | 持有{len(hold_signals)}")
    print(f"{'='*60}")
    
    if buy_signals:
        print(f"\n📌 买入信号 (按评分排序):")
        for s in buy_signals[:top_n]:
            print(f"  {s['code']} | 价格:{s['price']} | 评分:{s['score']}/{s['min_score']} | "
                  f"突破线:{s['dc_high']} | 止损:{s['stop_loss']} | {s['vol_state']} | {s['trend_state']}")
    
    if sell_signals:
        print(f"\n🔴 卖出信号:")
        for s in sell_signals[:top_n]:
            print(f"  {s['code']} | 价格:{s['price']} | 退出线:{s['exit_low']} | {s['vol_state']}")
    
    # 即将突破的股票（距离突破线<3%）
    near_breakout = [h for h in hold_signals 
                     if h.get('distance_to_breakout') is not None and h['distance_to_breakout'] < 3]
    near_breakout.sort(key=lambda x: x.get('distance_to_breakout', 999))
    if near_breakout:
        print(f"\n⏳ 即将突破 (距突破线<3%):")
        for h in near_breakout[:10]:
            print(f"  {h['code']} | 价格:{h['price']} | 突破线:{h['dc_high']} | 距离:{h['distance_to_breakout']}%")
    
    # 7. 保存信号记录
    today = datetime.now().strftime('%Y-%m-%d')
    record = {
        'date': today,
        'state': state,
        'params_source': param_source,
        'params': {k: v for k, v in params.items()},
        'buy_count': len(buy_signals),
        'sell_count': len(sell_signals),
        'hold_count': len(hold_signals),
        'buy_signals': buy_signals[:10],
        'sell_signals': sell_signals[:10],
    }
    save_signal_record(record)
    
    return record

def make_jsonable(obj):
    """递归转换numpy类型为原生Python类型"""
    if isinstance(obj, dict):
        return {k: make_jsonable(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [make_jsonable(v) for v in obj]
    elif isinstance(obj, (np.integer,)):
        return int(obj)
    elif isinstance(obj, (np.floating,)):
        return float(obj)
    elif isinstance(obj, (np.bool_,)):
        return bool(obj)
    elif isinstance(obj, np.ndarray):
        return obj.tolist()
    return obj

def save_signal_record(record):
    """保存信号记录到历史"""
    history = []
    if os.path.exists(SIGNAL_LOG):
        with open(SIGNAL_LOG, 'r', encoding='utf-8') as f:
            history = json.load(f)
    history.append(make_jsonable(record))
    history = history[-90:]
    with open(SIGNAL_LOG, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# ============ 参数对比报告 ============
def params_comparison_report():
    """打印当前优化参数与基准参数的对比"""
    data = load_optimal_params()
    baseline = data.get('baseline_params', BASELINE_PARAMS)
    results = data.get('results', [])
    
    print(f"\n{'='*60}")
    print("参数对比报告")
    print(f"{'='*60}")
    print(f"优化时间: {data.get('optimization_time', 'N/A')}")
    print(f"股票数量: {data.get('n_stocks', 'N/A')}")
    print(f"优化轮数: {data.get('n_trials', 'N/A')}")
    
    print(f"\n{'参数':20s} {'基准值':>10s}", end='')
    for r in results:
        if r['status'] == 'adopted':
            print(f" {r['state']:>12s}", end='')
    print()
    
    for key in BASELINE_PARAMS:
        print(f"{key:20s} {baseline.get(key, ''):>10}", end='')
        for r in results:
            if r['status'] == 'adopted':
                val = r['params'].get(key, baseline.get(key))
                diff = ((val - baseline.get(key, val)) / baseline.get(key, 1) * 100) if isinstance(val, (int, float)) and isinstance(baseline.get(key), (int, float)) and baseline.get(key) != 0 else 0
                if abs(diff) < 0.01:
                    print(f" {val:>12}", end='')
                else:
                    print(f" {val:>10.3f}({diff:+.0f}%)", end='')
        print()

# ============ 主程序 ============


# ============================================================
# 反馈循环模块（原 feedback_loop.py）
# 反馈循环 + 生产化 - 自适应参数优化系统第五步
# 功能：
# 1. 月度自动触发重优化（由Calendar调用）
# 2. 新参数必须夏普比率提升≥10%才采纳
# 3. 参数变更日志记录
# 4. 实盘vs回测偏离超过15%报警
# 5. 数据增量更新
# 
# 每月1号收盘后自动运行：
#   更新数据 → 刷新市场状态 → 重跑优化 → 对比旧参数 → 采纳/拒绝 → 推送报告
# ============================================================
#!/usr/bin/env python3
"""
反馈循环 + 生产化 - 自适应参数优化系统第五步
功能：
1. 月度自动触发重优化（由Calendar调用）
2. 新参数必须夏普比率提升≥10%才采纳
3. 参数变更日志记录
4. 实盘vs回测偏离超过15%报警
5. 数据增量更新

每月1号收盘后自动运行：
  更新数据 → 刷新市场状态 → 重跑优化 → 对比旧参数 → 采纳/拒绝 → 推送报告
"""



# ============ 月度重优化 ============
def monthly_reoptimize():
    """
    月度自动重优化主流程
    1. 增量更新数据
    2. 刷新市场状态时间线
    3. 重跑参数优化
    4. 与旧参数对比
    5. 采纳/拒绝并记录
    """
    print("=" * 60)
    print(f"月度重优化开始 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)
    
    # Step 1: 数据更新
    # 注意：每日信号扫描(15:00)已通过腾讯API自动写回parquet，月度优化不需要全量下载。
    # 优化只需20只抽样股票，run_optimization内部会通过_tencent_kline_multiep在线补齐。
    # 新浪API在CodeAct沙箱中被封且4939只×5000条需15分钟+，会导致540s超时（2026-09-01修复）。
    print("\n[1/5] 刷新指数数据（股票K线由每日扫描自动更新）...")
    try:
        download_index("sh000001", count=2000)
        print("✅ 上证指数刷新完成")
    except Exception as e:
        print(f"⚠️ 指数刷新失败（不影响优化）: {e}")
    
    # Step 2: 刷新市场状态
    print("\n[2/5] 刷新市场状态时间线...")
    try:
        generate_timeline()
        print("✅ 市场状态更新完成")
    except Exception as e:
        print(f"⚠️ 市场状态更新失败: {e}")
    
    # Step 3: 重跑优化
    print("\n[3/5] 重跑参数优化...")
    # 关键：run_optimization会覆盖optimal_params.json，必须在调用前快照在用参数，
    # 否则后续熔断的"新旧对比"变成新vs新、拒绝的参数也无法回滚（2026-09-01修复）
    old_snapshot = load_old_params()
    try:
        # 月度重优化用20股×15轮（适配沙箱600s限制，完整优化200×50由主会话执行）
        new_result = run_optimization(n_trials=15, test_mode=False, n_stocks=20)
        if new_result is None:
            print("⚠️ 优化失败，保留旧参数")
            return
        print("✅ 优化完成")
    except Exception as e:
        print(f"⚠️ 优化失败: {e}")
        return
    
    # Step 4: 与旧参数对比（用Step3前的快照，而非已被覆盖的文件）
    print("\n[4/5] 对比新旧参数...")
    comparison = compare_params(old_snapshot, new_result)
    
    # Step 5: 采纳/拒绝并记录
    print("\n[5/5] 参数决策...")
    adopted_count = 0
    rejected_count = 0
    
    for cmp in comparison:
        if cmp['action'] == 'adopted':
            adopted_count += 1
            print(f"  ✅ {cmp['state']}: 采纳新参数 (夏普 {cmp['old_sharpe']:.3f} → {cmp['new_sharpe']:.3f})")
        else:
            rejected_count += 1
            print(f"  ⏭️ {cmp['state']}: 保留旧参数 ({cmp['reason']})")

    # 【关键修复】compare_params只在内存中标记决策，必须落盘：
    # run_optimization已把新参数写入optimal_params.json，若不回写，被熔断拒绝的
    # 参数仍以status=adopted留在文件里，select_params会直接加载——熔断形同虚设。
    try:
        with open(PARAMS_FILE, 'r', encoding='utf-8') as f:
            final_data = json.load(f)
        snap_results = {r['state']: r for r in (old_snapshot or {}).get('results', [])}
        for r in final_data.get('results', []):
            cmp = next((c for c in comparison if c['state'] == r['state']), None)
            if cmp is None:
                continue
            if cmp['action'] == 'rejected':
                old_r = snap_results.get(r['state'])
                if old_r is not None:
                    r['params'] = old_r.get('params', r['params'])
                    for _f in ('val_sharpe', 'val_return', 'val_drawdown',
                               'val_trades', 'train_sharpe'):
                        if _f in old_r:
                            r[_f] = old_r[_f]
                    # 保留旧条目的status：旧参数本就在用(adopted)则继续生效；
                    # 若把回滚后的在用条目也打成rejected，select_params会跳过它
                    # 回退状态基准，等于变相下线已采纳参数（2026-09-01修复）。
                    r['status'] = old_r.get('status', 'rejected')
                else:
                    r['params'] = dict(STATE_BASELINES.get(r['state'], BASELINE_PARAMS))
                    r['val_sharpe'] = None
                    r['status'] = 'rejected'
            else:
                r['status'] = 'adopted'
        final_data['optimization_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(PARAMS_FILE, 'w', encoding='utf-8') as f:
            json.dump(final_data, f, ensure_ascii=False, indent=4)
        print("  ✅ 参数决策已落盘（拒绝项已回滚为在用参数）")
    except Exception as e:
        print(f"  ⚠️ 参数决策落盘失败: {e}")

    # 保存参数历史
    save_params_history(old_snapshot, new_result, comparison)
    
    print(f"\n{'='*60}")
    print(f"月度重优化完成: 采纳{adopted_count}项, 拒绝{rejected_count}项")
    print(f"{'='*60}")
    
    # 生成摘要报告
    report = generate_report(new_result, comparison)
    return report

# ============ 每日优化用：腾讯K线多端点轮询（2026-08-26 修复WAF拦截导致有效数据不足）============
def _tencent_kline_multiep(code, count=640, fq="qfq"):
    """从腾讯多端点轮询拉取前复权日线，主端点被WAF拦截时自动切备用端点。
    返回 DataFrame(date,open,close,high,low,volume) 或 None。"""
    endpoints = [
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
        "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
    ]
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
    code_upper = code.upper()
    for ep in endpoints:
        try:
            url = f"{ep}?param={code},day,,,{count},{fq}"
            resp = requests.get(url, timeout=12, headers=headers)
            if resp.status_code != 200 or resp.text.lstrip().startswith("<"):
                continue
            raw = resp.json()
            if raw.get("code") not in (0, None):
                continue
            d = raw.get("data", {}).get(code) or raw.get("data", {}).get(code_upper) or {}
            bars = d.get("qfqday") or d.get("day") or []
            if not bars or len(bars) < 30:
                continue
            rows = []
            for b in bars:
                try:
                    rows.append({"date": b[0], "open": float(b[1]), "close": float(b[2]),
                                 "high": float(b[3]), "low": float(b[4]),
                                 "volume": float(b[5]) if len(b) > 5 else 0.0})
                except Exception:
                    continue
            if len(rows) < 30:
                continue
            df = pd.DataFrame(rows)
            df["date"] = pd.to_datetime(df["date"])
            for c in ["open", "close", "high", "low", "volume"]:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            df = df.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)
            if len(df) >= 30:
                return df
        except Exception:
            continue
    return None


def _cache_backfilled_kline(code, online_df):
    """把在线补齐的长历史K线合并回本地parquet（去重、按日期排序、上限2000根）。
    前复权数值随时间整体漂移，同日期以本次在线拉取为准（concat在后、keep='last'）。
    写入失败不影响本次优化，仅损失后续的缓存收益。"""
    try:
        path = os.path.join(STOCK_DATA_DIR, f"{code}.parquet")
        merged = online_df
        if os.path.exists(path):
            try:
                local_df = pd.read_parquet(path)
                merged = pd.concat([local_df, online_df], ignore_index=True)
            except Exception:
                merged = online_df
        merged = merged.copy()
        merged["date"] = pd.to_datetime(merged["date"])
        merged = (merged.drop_duplicates(subset="date", keep="last")
                       .sort_values("date")
                       .tail(2000)
                       .reset_index(drop=True))
        os.makedirs(STOCK_DATA_DIR, exist_ok=True)
        merged.to_parquet(path, index=False)
    except Exception:
        pass



def _is_state_in_feedback_cooldown(state, cooldown_days=7):
    """检查某市场状态是否处于反馈引擎冷却期（feedback_adjustments.json 中近N天有调整记录）。
    冷却期内，每日Optuna不得覆盖该状态参数，以免冲掉基于实战的防守/放开调整。"""
    try:
        adj_path = os.path.join(DATA_DIR, "feedback_adjustments.json")
        if not os.path.exists(adj_path):
            return False, None
        with open(adj_path, "r", encoding="utf-8") as f:
            history = json.load(f)
        cutoff = (datetime.now() - timedelta(days=cooldown_days)).strftime("%Y-%m-%d")
        recent = [a for a in history
                  if a.get("state") == state and a.get("date", "") >= cutoff]
        if recent:
            return True, recent[-1].get("date")
    except Exception:
        pass
    return False, None


def daily_optimize():
    """
    每日轻量优化（只优化当前市场状态，10股×10轮，~2分钟）
    1. 检测当前市场状态
    2. 随机选10只股票
    3. 用最近120天该状态的数据优化18个参数
    4. 熔断检查（±30%状态基准/±20%状态基准/夏普≥-5%）
    5. 更新optimal_params.json
    """
    print("=" * 60)
    print(f"每日优化开始 - {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 60)

    # Step 1: 检测当前市场状态
    print("\n[1/4] 检测当前市场状态...")
    timeline = load_market_states()
    if not timeline:
        print("⚠️ 无市场状态数据，跳过")
        return "每日优化失败：无市场状态数据"

    # 取最近30天的状态
    recent_states = timeline[-30:]
    current_state = recent_states[-1]['state'] if recent_states else 'sideways'
    state_count = sum(1 for s in recent_states if s['state'] == current_state)
    print(f"当前状态: {current_state} (最近30天中{state_count}天)")

    # Step 2: 加载数据（10只随机股票）
    print("\n[2/4] 加载股票数据...")
    all_codes = get_all_local_codes()
    if len(all_codes) < 10:
        print(f"⚠️ 股票数不足10只({len(all_codes)})，跳过")
        return "每日优化失败：股票数不足"

    import random
    random.seed(int(datetime.now().strftime('%Y%m%d')))  # 每天不同种子
    # 多抽一些股票，因为部分数据源可能失败，取前10只有效的
    _candidate_codes = random.sample(all_codes, min(50, len(all_codes)))

    from datetime import timedelta
    cutoff_date = pd.Timestamp.now() - pd.Timedelta(days=30)
    data_dict = {}
    _LOCAL_STOCK_DIR = os.path.join(DATA_DIR, "stocks")
    _KLINE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
    for code in _candidate_codes:
        if len(data_dict) >= 10:
            break
        df = None
        # 1. 读本地parquet + 在线补齐最新K线（复用信号扫描逻辑）
        try:
            _pq_path = os.path.join(_LOCAL_STOCK_DIR, f"{code}.parquet")
            if os.path.exists(_pq_path):
                local_df = pd.read_parquet(_pq_path)
                if "date" not in local_df.columns:
                    local_df = local_df.reset_index()
                local_df["date"] = pd.to_datetime(local_df["date"])
                for c in ["open", "close", "high", "low", "volume"]:
                    if c in local_df.columns:
                        local_df[c] = pd.to_numeric(local_df[c], errors="coerce")
                local_df = local_df.sort_values("date").reset_index(drop=True)
                # 在线拉最新5根K线补齐（多端点轮询，2026-08-26 修复WAF拦截）
                try:
                    _latest = _tencent_kline_multiep(code, count=5, fq="qfq")
                    if _latest is not None and len(_latest) > 0:
                        _common = [c for c in ["date","open","close","high","low","volume"]
                                   if c in local_df.columns and c in _latest.columns]
                        for _, _r in _latest.iterrows():
                            _mask = local_df["date"] == _r["date"]
                            if _mask.any():
                                for c in ["open","close","high","low","volume"]:
                                    if c in local_df.columns and c in _latest.columns:
                                        local_df.loc[_mask, c] = _r[c]
                        _new = _latest[_latest["date"] > local_df["date"].iloc[-1]]
                        if len(_new) > 0:
                            local_df = pd.concat([local_df[_common], _new[_common]], ignore_index=True)
                            local_df = local_df.drop_duplicates(subset=["date"], keep="last")
                            local_df = local_df.sort_values("date").reset_index(drop=True)
                except Exception:
                    pass
                df = local_df
        except Exception:
            df = None
        # 2. CLI获取
        if df is None or len(df) < 120:
            try:
                df = dp_fetch_kline(code, count=640)
            except Exception:
                pass
        # 3. 腾讯API全量获取（多端点轮询，2026-08-26 修复WAF拦截）
        if df is None or len(df) < 120:
            try:
                df = _tencent_kline_multiep(code, count=640, fq="qfq")
            except Exception:
                pass
        if df is not None and len(df) >= 120:
            if df['date'].max() >= cutoff_date:
                data_dict[code] = df
    print(f"成功加载 {len(data_dict)} 只股票")

    if len(data_dict) < 3:
        print("⚠️ 有效数据不足3只，跳过")
        return "每日优化失败：有效数据不足"

    # Step 3: 优化当前状态参数（10轮）
    print(f"\n[3/4] 优化 {current_state} 状态参数(10轮)...")
    all_codes_list = list(data_dict.keys())
    min_data_date = min(df['date'].min() for df in data_dict.values()).strftime('%Y-%m-%d')
    max_data_date = max(df['date'].max() for df in data_dict.values()).strftime('%Y-%m-%d')

    state_dates = [t['date'] for t in timeline
                   if t['state'] == current_state and t['date'] >= min_data_date and t['date'] <= max_data_date]

    if len(state_dates) < 30:
        print(f"⚠️ {current_state}状态仅{len(state_dates)}天，数据不足")
        return f"每日优化跳过：{current_state}状态数据不足({len(state_dates)}天)"

    try:
        new_result = optimize_for_state(current_state, all_codes_list, data_dict, state_dates, n_trials=10)
    except Exception as e:
        print(f"⚠️ 优化失败: {e}")
        return f"每日优化失败：{e}"

    if new_result is None:
        return "每日优化失败：无结果"

    # Step 4: 熔断检查 + 更新
    print("\n[4/4] 熔断检查与参数更新...")
    if new_result['status'] != 'adopted':
        print(f"⏭️ {current_state} 优化未达标，保留旧参数")
        return f"每日优化完成：{current_state}未达标，保留旧参数"

    # 熔断0：反馈引擎冷却期保护——冷却期内Optuna不得覆盖该状态参数，
    # 防止10股×10轮小样本的历史回测参数冲掉基于实战交易结果的防守/放开调整。
    in_cooldown, last_adj_date = _is_state_in_feedback_cooldown(current_state)
    if in_cooldown:
        print(f"🔒 {current_state} 处于反馈冷却期（最近调整 {last_adj_date}），"
              f"每日Optuna不覆盖实战调整参数，保留当前参数")
        return (f"每日优化完成 ✅（回测完成，冷却期保护生效）\n"
                f"状态: {current_state}\n"
                f"回测夏普: {new_result['val_sharpe']:.3f}（基准 {new_result.get('baseline_val_sharpe',0):.3f}）\n"
                f"冷却期: 至 {last_adj_date} 后7天，期间保留实战反馈参数")

    # 加载现有参数
    with open(PARAMS_FILE, 'r', encoding='utf-8') as f:
        opt_data = json.load(f)

    # 找到对应状态的旧参数
    old_state_result = None
    for r in opt_data.get('results', []):
        if r['state'] == current_state:
            old_state_result = r
            break

    if old_state_result:
        old_params = old_state_result.get('params', {})
        new_params = new_result['params']

        # 熔断1+2: vs状态专属基准（硬限制±30%/软限制±20%允许5个超标）
        state_bp = STATE_BASELINES.get(current_state, BASELINE_PARAMS)
        hard_violations = []
        soft_violations = []
        for k, v in new_params.items():
            # vol_stack分档阈值使用独立优化范围，不受±30%偏差限制
            if k.startswith('vol_stack_threshold_'):
                continue
            bp_v = state_bp.get(k)
            if bp_v is not None and bp_v != 0:
                dev = abs(v - bp_v) / abs(bp_v)
                if dev > 0.30:
                    hard_violations.append(f"{k}偏离基准{dev:.0%}")
                elif dev > 0.20:
                    soft_violations.append(f"{k}偏离基准{dev:.0%}")

        if hard_violations:
            print(f"⚠️ 熔断1触发(超±30%): {', '.join(hard_violations)}")
            print("保留旧参数，参数偏离硬限制")
            return f"每日优化完成：{current_state}熔断1触发，保留旧参数"

        if len(soft_violations) > 5:
            print(f"⚠️ 熔断2触发(超±20%共{len(soft_violations)}个): {', '.join(soft_violations)}")
            print("保留旧参数，软限制超标参数过多")
            return f"每日优化完成：{current_state}熔断2触发，保留旧参数"
        elif soft_violations:
            print(f"ℹ️ 熔断2提示(超±20%共{len(soft_violations)}个，允许): {', '.join(soft_violations)}")

        # 熔断3: 新参数在同一样本验证集上不显著差于基准参数
        # （用optimize_for_state内部同口径回测的baseline_val_sharpe，
        #  而非历史月度大样本的old_sharpe——后者股票池/时间段不同，不可比）
        baseline_sharpe = new_result.get('baseline_val_sharpe', 0)
        new_sharpe = new_result.get('val_sharpe', 0)
        if baseline_sharpe > 0 and new_sharpe < baseline_sharpe * 0.95:
            print(f"⚠️ 夏普低于基准5%以上: 基准{baseline_sharpe:.3f}→新{new_sharpe:.3f}")
            return f"每日优化完成：{current_state}夏普低于基准，保留旧参数"

        # 熔断4: 验证集最大回撤硬上限。10股×10轮小样本容易挑出
        # 高Sharpe但回撤不可接受的参数（如-31%），必须拦住。
        DAILY_MAX_DRAWDOWN = 0.22
        new_dd = abs(new_result.get('val_drawdown', 0) or 0) / 100.0
        if new_dd > DAILY_MAX_DRAWDOWN:
            print(f"⚠️ 熔断4触发(回撤{new_dd:.1%}>{DAILY_MAX_DRAWDOWN:.0%})，保留旧参数")
            return f"每日优化完成：{current_state}回撤过大({new_dd:.1%})，保留旧参数"

    # 更新参数
    updated = False
    for r in opt_data.get('results', []):
        if r['state'] == current_state:
            r['params'] = new_result['params']
            r['val_sharpe'] = new_result['val_sharpe']
            r['val_return'] = new_result['val_return']
            r['val_drawdown'] = new_result['val_drawdown']
            r['val_trades'] = new_result['val_trades']
            r['train_sharpe'] = new_result['train_sharpe']
            r['optimization_time'] = new_result.get('optimization_time', 0)
            r['status'] = 'adopted'
            r['train_period'] = new_result.get('train_period', '')
            r['val_period'] = new_result.get('val_period', '')
            updated = True
            break

    if updated:
        opt_data['optimization_time'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(PARAMS_FILE, 'w', encoding='utf-8') as f:
            json.dump(opt_data, f, ensure_ascii=False, indent=4)
        print(f"✅ {current_state} 参数已更新")

        report = (f"每日优化完成 ✅\n"
                  f"状态: {current_state}\n"
                  f"验证夏普: {new_result['val_sharpe']:.3f}\n"
                  f"验证收益: {new_result['val_return']:.1f}%\n"
                  f"验证回撤: {new_result['val_drawdown']:.1f}%\n"
                  f"交易笔数: {new_result['val_trades']}\n"
                  f"优化耗时: {new_result.get('optimization_time', 0):.0f}秒")
    else:
        report = f"每日优化完成：未找到{current_state}状态记录"

    # ---- 实盘反馈自动调参（闭环） ----
    # 不管 Optuna 是否采纳新参数，都根据模拟盘真实交易结果做一次反馈调整
    try:
        from monitor.performance_feedback import apply_feedback, generate_feedback_report
        print("\n[反馈闭环] 检查实盘表现...")
        fb_result = apply_feedback(dry_run=False)
        if fb_result.get("success") and fb_result.get("changes"):
            print(f"[反馈闭环] {fb_result.get('reason', '')}")
            for c in fb_result["changes"]:
                print(f"  {c['param']}: {c['old']} → {c['new']}")
            report += "\n\n" + generate_feedback_report(fb_result)
        elif fb_result.get("action") in ("cooldown", "hold", "insufficient_data", "no_effective_change"):
            print(f"[反馈闭环] {fb_result.get('message', fb_result.get('reason', '无需调整'))}")
        else:
            print(f"[反馈闭环] {fb_result.get('message', '完成')}")
    except Exception as e:
        print(f"[反馈闭环] 异常（不影响主流程）: {e}")

    return report

def load_old_params():
    """加载当前使用的参数"""
    if os.path.exists(PARAMS_FILE):
        with open(PARAMS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return None

def compare_params(old_data, new_data):
    """对比新旧参数，决定是否采纳（月度4重熔断，2026-09-01修复）。
    修复要点：
    1. 熔断1基准改为STATE_BASELINES状态专属基准（旧代码误用v5默认BASELINE_PARAMS，
       pre_filter基准40导致60/68等合法值被误判偏离70%）；
    2. 熔断3改用optimize_for_state同口径baseline_val_sharpe（旧代码old_sharpe因
       快照时机错误恒等于new_sharpe，夏普比较完全失效）；
    3. walk-forward验证未通过的状态(run_optimization内status!=adopted)直接拒绝；
    4. 拒绝时回滚params/status，调用方负责落盘。"""
    comparison = []

    old_results = {r['state']: r for r in (old_data or {}).get('results', [])}

    for new_r in new_data.get('results', []):
        state = new_r['state']
        old_r = old_results.get(state)
        new_params = new_r.get('params', {})
        new_sharpe = new_r.get('val_sharpe', 0) or 0
        old_sharpe = (old_r.get('val_sharpe') if old_r else None)
        old_params = old_r.get('params', {}) if old_r else None
        state_bp = STATE_BASELINES.get(state, BASELINE_PARAMS)

        def _reject(reason):
            new_r['params'] = old_params if old_params else dict(state_bp)
            new_r['status'] = 'rejected'
            comparison.append({
                'state': state, 'action': 'rejected',
                'old_sharpe': old_sharpe or 0, 'new_sharpe': new_sharpe,
                'improvement': 0, 'reason': reason
            })

        # 0) walk-forward样本外验证未通过（run_optimization内新夏普<基准）→直接拒绝
        if new_r.get('status') != 'adopted':
            _reject(f"walk-forward验证未通过(新夏普{new_sharpe:.3f} vs 同口径基准"
                    f"{new_r.get('baseline_val_sharpe', 0) or 0:.3f})，保留旧参数")
            continue

        if old_r is None:
            comparison.append({
                'state': state, 'action': 'adopted',
                'old_sharpe': 0, 'new_sharpe': new_sharpe,
                'improvement': 100, 'reason': '新状态首次优化且通过walk-forward验证'
            })
            continue

        # 【熔断1】vs状态专属基准偏离≤30%硬限制（vol_stack分档阈值豁免）
        hard_violations = []
        for k, v in new_params.items():
            if k.startswith('vol_stack_threshold_'):
                continue
            bp_v = state_bp.get(k)
            if bp_v is not None and bp_v != 0:
                dev = abs(v - bp_v) / abs(bp_v)
                if dev > 0.30:
                    hard_violations.append(f"{k}偏离{dev:.0%}")
        if hard_violations:
            _reject(f"熔断1:参数超状态基准30%({'; '.join(hard_violations)})")
            continue

        # 【熔断2】vs状态专属基准偏离≤20%软限制（允许最多5个超标，防突变）
        soft_violations = []
        for k, v in new_params.items():
            if k.startswith('vol_stack_threshold_'):
                continue
            bp_v = state_bp.get(k)
            if bp_v is not None and bp_v != 0:
                dev = abs(v - bp_v) / abs(bp_v)
                if 0.20 < dev <= 0.30:
                    soft_violations.append(f"{k}变化{dev:.0%}")
        if len(soft_violations) > 5:
            _reject(f"熔断2:参数偏离状态基准>20%共{len(soft_violations)}个"
                    f"({'; '.join(soft_violations)})")
            continue

        # 【熔断3】同口径回测：新夏普不低于基准×0.95（防退化）
        baseline_sharpe = new_r.get('baseline_val_sharpe', 0) or 0
        if baseline_sharpe > 0 and new_sharpe < baseline_sharpe * 0.95:
            _reject(f"熔断3:夏普{new_sharpe:.3f}低于同口径基准{baseline_sharpe:.3f}的95%")
            continue

        # 【采纳门槛】相对当前在用参数夏普提升≥10%（旧参数为哨兵值/缺失时视为新状态）
        if old_sharpe and old_sharpe > 0:
            improvement = (new_sharpe - old_sharpe) / abs(old_sharpe)
        else:
            improvement = 1.0
        if improvement < 0.10:
            _reject(f"夏普提升仅{improvement*100:.1f}% (<10%门槛)")
            continue

        comparison.append({
            'state': state, 'action': 'adopted',
            'old_sharpe': old_sharpe or 0, 'new_sharpe': new_sharpe,
            'improvement': round(improvement * 100, 1),
            'reason': f"通过全部熔断，夏普提升{improvement*100:.1f}%"
        })

    return comparison


def save_params_history(old_data, new_data, comparison):
    """保存参数变更历史"""
    history = []
    if os.path.exists(PARAMS_HISTORY):
        with open(PARAMS_HISTORY, 'r', encoding='utf-8') as f:
            history = json.load(f)
    
    entry = {
        'date': datetime.now().strftime('%Y-%m-%d %H:%M'),
        'old_optimization_time': old_data.get('optimization_time') if old_data else None,
        'new_optimization_time': new_data.get('optimization_time'),
        'n_stocks': new_data.get('n_stocks'),
        'n_trials': new_data.get('n_trials'),
        'changes': comparison
    }
    history.append(entry)
    history = history[-24:]  # 保留最近24个月
    
    with open(PARAMS_HISTORY, 'w', encoding='utf-8') as f:
        json.dump(history, f, ensure_ascii=False, indent=2)

# ============ 偏离监控 ============
def check_deviation(actual_returns, backtest_returns):
    """
    检查实盘收益与回测收益的偏离
    actual_returns: 实际持仓收益率列表
    backtest_returns: 同期回测收益率列表
    """
    if not actual_returns or not backtest_returns:
        return None
    
    deviations = []
    for actual, bt in zip(actual_returns, backtest_returns):
        dev = abs(actual - bt) / abs(bt) * 100 if bt != 0 else 0
        deviations.append(dev)
    
    avg_deviation = np.mean(deviations)
    max_deviation = np.max(deviations)
    
    alert = None
    if avg_deviation > 15:
        alert = {
            'date': datetime.now().strftime('%Y-%m-%d'),
            'type': 'high_deviation',
            'avg_deviation': round(avg_deviation, 1),
            'max_deviation': round(max_deviation, 1),
            'message': f'实盘与回测平均偏离{avg_deviation:.1f}%（超15%阈值），请检查策略有效性'
        }
        
        # 保存报警
        alerts = []
        if os.path.exists(DEVIATION_LOG):
            with open(DEVIATION_LOG, 'r', encoding='utf-8') as f:
                alerts = json.load(f)
        alerts.append(alert)
        alerts = alerts[-50:]
        with open(DEVIATION_LOG, 'w', encoding='utf-8') as f:
            json.dump(alerts, f, ensure_ascii=False, indent=2)
    
    return {
        'avg_deviation': round(avg_deviation, 1),
        'max_deviation': round(max_deviation, 1),
        'alert': alert
    }

# ============ 报告生成 ============
def generate_report(opt_result, comparison):
    """生成月度优化报告"""
    report = f"""
📊 龟缠安泰v5.3.4.1 月度参数优化报告
{'='*50}
优化时间: {opt_result.get('optimization_time', 'N/A')}
股票数量: {opt_result.get('n_stocks', 'N/A')}
优化轮数: {opt_result.get('n_trials', 'N/A')}

参数变更:
"""
    for cmp in comparison:
        action_emoji = "✅" if cmp['action'] == 'adopted' else "⏭️"
        report += f"  {action_emoji} {cmp['state']:12s} 夏普: {cmp['old_sharpe']:.3f} → {cmp['new_sharpe']:.3f} ({cmp['reason']})\n"
    
    adopted = sum(1 for c in comparison if c['action'] == 'adopted')
    report += f"\n采纳: {adopted}/{len(comparison)}\n"
    
    return report

# ============ 主程序 ============

# ===================== 入口 =====================
def main():
    parser = argparse.ArgumentParser(description="龟缠安泰v5.3.4.1 攻守兼备升级版")
    parser.add_argument("--mode", default="backtest", choices=["backtest", "scan"])
    parser.add_argument("--code", default="300750,603986,600887,002353,688111",
                        help="股票代码，多只用逗号分隔")
    parser.add_argument("--scan-date", default=str(datetime.now().date()))
    parser.add_argument("--start", default="2023-01-01")
    parser.add_argument("--end", default="2026-08-05")
    parser.add_argument("--initial-capital", type=float, default=None, help="初始本金，单位：元")
    parser.add_argument("--save-name", default="龟缠安泰v5.3.4.1_攻守兼备", help="输出文件名")
    parser.add_argument("--adaptive", nargs='?', const='auto', default='auto',
                        choices=['auto', 'bull', 'bear', 'sideways', 'transition', 'off'],
                        help="自适应参数模式，默认auto(自动判断市场状态)。off=用v5.3.4.1固定参数")
    args = parser.parse_args()

    cfg = DEFAULT_CONFIG
    
    # ========== 自适应参数加载（默认开启）==========
    adaptive_source = "v5.3.4.1默认参数"
    if args.adaptive and args.adaptive != 'off':
        # 【修改】从配置读取参数文件路径，不再硬编码
        OPTIMAL_PARAMS_FILE = PARAMS_FILE
        if os.path.exists(OPTIMAL_PARAMS_FILE):
            with open(OPTIMAL_PARAMS_FILE, 'r', encoding='utf-8') as f:
                opt_data = json.load(f)
            
            target_state = args.adaptive
            if target_state == 'auto':
                # 自动判断：用回测结束日的市场状态
                try:
                    # get_state 已合并到本文件（市场状态识别模块）
                    state_info = get_state(args.end)
                    target_state = state_info.get('state', 'sideways')
                    print(f"📊 自动判断市场状态: {target_state} ({state_info.get('description','')})")
                except Exception as e:
                    print(f"⚠️ 自动判断市场状态失败({e})，使用sideways")
                    target_state = 'sideways'
            
            # 查找对应状态的优化参数
            found_params = None
            for r in opt_data.get('results', []):
                if r['state'] == target_state and r.get('status') == 'adopted':
                    found_params = r['params']
                    break
            
            if found_params:
                cfg["strategy"]["min_entry_score"] = found_params["min_entry_score"]
                cfg["strategy"]["base_atr_multiplier"] = found_params["atr_multiplier"]
                cfg["strategy"]["dc_period"] = found_params["channel_base"]
                cfg["strategy"]["exit_period"] = found_params["exit_base"]
                cfg["strategy"]["vol_ratio_high"] = found_params["vol_ratio_high"]
                cfg["strategy"]["vol_ratio_low"] = found_params["vol_ratio_low"]
                cfg["strategy"]["trend_str_high"] = found_params["trend_str_high"]
                cfg["strategy"]["trend_str_low"] = found_params["trend_str_low"]
                # 【自适应】新增3个参数注入
                cfg["strategy"]["trail_stop_pct"] = found_params.get("trail_stop_pct", 0.05)
                cfg["strategy"]["risk_value_ban"] = found_params.get("risk_value_ban", 80)
                cfg["strategy"]["score_threshold"] = found_params.get("score_threshold", 2.0)
                adaptive_source = f"自适应优化参数({target_state})"
                print(f"📊 已加载自适应参数[{target_state}]: entry={found_params['min_entry_score']:.3f} "
                      f"atr={found_params['atr_multiplier']:.3f} ch={found_params['channel_base']} "
                      f"exit={found_params['exit_base']} vr=[{found_params['vol_ratio_low']:.3f},{found_params['vol_ratio_high']:.3f}]")
            else:
                print(f"⚠️ 未找到{target_state}状态的优化参数，使用默认参数")
        else:
            print(f"⚠️ 优化参数文件不存在，使用默认参数")
    
    if args.initial_capital is not None:
        cfg["strategy"]["initial_capital"] = args.initial_capital
        print(f"📊 使用指定本金: {args.initial_capital:,.0f} 元")
    else:
        print(f"📊 使用默认本金: {cfg['strategy']['initial_capital']:,.0f} 元")
    
    Path(cfg["cache_dir"]).mkdir(exist_ok=True)
    Path(cfg["output_dir"]).mkdir(exist_ok=True)
    code_list = [c.strip() for c in args.code.split(",")]

    print(f"🐢 龟缠安泰v5.3.4.1 攻守兼备升级版 启动")
    print(f"股票: {code_list} | {args.start} ~ {args.end}")
    print(f"初始资金: {cfg['strategy']['initial_capital']:,.2f}")
    print("【V5.3.4核心升级】")
    print("  【1】min_entry_score 从 2.0 → 1.7（增加候选池，解决集中度问题）")
    print("  【2】估值极端低豁免（价格分位<10%时额外+15分，仓位上限30%）")
    print("  【3】熊市末期抄底信号（在1664点级别底部可小仓位试错）")
    print("  【保留】L1宏观硬截断（上证跌破MA60超3%时空仓）")
    print("  【保留】L2行业R²过滤 + L3跨股波动率ATR调整 + L4动态止盈")
    print("-" * 60)

    trades, eq_df, final_cap, bt_df_dict, max_dd, max_dd_date = run_multi_backtest(
        code_list, cfg, args.start, args.end
    )

    init_cap = cfg["strategy"]["initial_capital"]
    total_ret = (final_cap - init_cap) / init_cap * 100
    win_trades = [t for t in trades if t.pnl > 0]
    loss_trades = [t for t in trades if t.pnl <= 0]

    print("\n" + "=" * 70)
    print("📊 龟缠安泰v5.3.4.1 回测报告")
    print("=" * 70)
    print(f"回测区间: {args.start} ~ {args.end}")
    print(f"参数来源: {adaptive_source}")
    print(f"初始资金: {init_cap:,.2f}")
    print(f"期末资金: {final_cap:,.2f}")
    print(f"总收益: {final_cap - init_cap:,.2f} ({total_ret:+.2f}%)")
    print(f"最大回撤: {max_dd*100:.2f}% (发生日期: {max_dd_date.strftime('%Y-%m-%d') if max_dd_date else 'N/A'})")
    print(f"总交易: {len(trades)} 笔")
    if trades:
        print(f"胜率: {len(win_trades)/len(trades)*100:.1f}% ({len(win_trades)}胜 / {len(loss_trades)}负)")
        avg_win = np.mean([t.pnl_pct for t in win_trades]) if win_trades else 0
        avg_loss = np.mean([t.pnl_pct for t in loss_trades]) if loss_trades else 0
        print(f"平均盈利: {avg_win:+.1f}% | 平均亏损: {avg_loss:+.1f}%")
        print(f"盈亏比: {avg_win/abs(avg_loss):.2f}" if avg_loss != 0 else "盈亏比: N/A")
        print("\n交易明细:")
        print("-" * 70)
        for i, t in enumerate(trades):
            code_info = f"[{t.code}] " if hasattr(t, 'code') else ""
            print(f" #{i+1} {code_info}买{t.buy_date.strftime('%m-%d')}@{t.buy_price:.2f} "
                  f"卖{t.sell_date.strftime('%m-%d') if t.sell_date else '-'}@{t.sell_price:.2f} "
                  f"盈亏{t.pnl_pct:+.1f}% [{t.sell_reason}]")
    print("=" * 70)

    if cfg["plot_enable"] and bt_df_dict:
        for code in code_list:
            if code in bt_df_dict:
                plot_backtest_report(
                    code, trades, eq_df, final_cap, bt_df_dict[code],
                    init_cap, max_dd, cfg["output_dir"], f"{args.save_name}_{code}"
                )

    print(f"\n✅ 回测完成！图表保存在: {cfg['output_dir']}/")

# ============ 数据驱动的状态基准参数 ============
# 基于历史数据统计特征(趋势斜率/回撤深度/日均收益)自动校准
# 替代手动设定的per-state初始值，Optuna在此基准±30%范围内搜索

STATE_BASELINES = {
    'bull': {
        'vol_ratio_high': 1.3, 'vol_ratio_low': 0.7,
        'trend_str_high': 0.15, 'trend_str_low': -0.05,
        'min_entry_score': 1.7, 'atr_multiplier': 2.0,
        'channel_base': 20, 'exit_base': 10,
        'trail_stop_pct': 0.05, 'risk_value_ban': 80, 'score_threshold': 2.0,
        'base_risk_pct': 0.117, 'max_concurrent_positions': 7,
        'take_profit_base': 0.50, 'stop_multiplier_base': 2.2,
        'add_threshold_base': 0.045, 'pre_filter_threshold': 60,
        'vol_pos_factor': 0.13,
        'vol_stack_threshold_large': 3.0, 'vol_stack_threshold_mid': 5.0, 'vol_stack_threshold_small': 8.0,
    },
    'bear': {
        'vol_ratio_high': 1.3, 'vol_ratio_low': 0.7,
        'trend_str_high': 0.15, 'trend_str_low': -0.05,
        'min_entry_score': 1.7, 'atr_multiplier': 2.0,
        'channel_base': 20, 'exit_base': 10,
        'trail_stop_pct': 0.05, 'risk_value_ban': 80, 'score_threshold': 2.0,
        'base_risk_pct': 0.05, 'max_concurrent_positions': 2,
        'take_profit_base': 0.30, 'stop_multiplier_base': 2.6,
        'add_threshold_base': 0.09, 'pre_filter_threshold': 60,
        'vol_pos_factor': 0.22,
        'vol_stack_threshold_large': 3.0, 'vol_stack_threshold_mid': 5.0, 'vol_stack_threshold_small': 8.0,
    },
    'sideways': {
        'vol_ratio_high': 1.3, 'vol_ratio_low': 0.7,
        'trend_str_high': 0.15, 'trend_str_low': -0.05,
        'min_entry_score': 1.7, 'atr_multiplier': 2.0,
        'channel_base': 20, 'exit_base': 10,
        'trail_stop_pct': 0.05, 'risk_value_ban': 80, 'score_threshold': 2.0,
        'base_risk_pct': 0.086, 'max_concurrent_positions': 5,
        'take_profit_base': 0.40, 'stop_multiplier_base': 2.5,
        'add_threshold_base': 0.065, 'pre_filter_threshold': 60,
        'vol_pos_factor': 0.19,
        'vol_stack_threshold_large': 3.0, 'vol_stack_threshold_mid': 5.0, 'vol_stack_threshold_small': 8.0,
    },
    'transition': {
        'vol_ratio_high': 1.3, 'vol_ratio_low': 0.7,
        'trend_str_high': 0.15, 'trend_str_low': -0.05,
        'min_entry_score': 1.7, 'atr_multiplier': 2.0,
        'channel_base': 20, 'exit_base': 10,
        'trail_stop_pct': 0.05, 'risk_value_ban': 80, 'score_threshold': 2.0,
        'base_risk_pct': 0.070, 'max_concurrent_positions': 4,
        'take_profit_base': 0.39, 'stop_multiplier_base': 2.7,
        'add_threshold_base': 0.079, 'pre_filter_threshold': 60,
        'vol_pos_factor': 0.20,
        'vol_stack_threshold_large': 3.0, 'vol_stack_threshold_mid': 5.0, 'vol_stack_threshold_small': 8.0,
    },
}

def calibrate_state_baselines(data_dict=None, timeline=None, n_stocks=80):
    """
    基于历史数据统计特征，为每个市场状态计算数据驱动的基准参数。
    用趋势斜率(方向)+回撤深度(风险)+日均收益(方向)三维度构建状态分数，
    再通过物理关系映射到7个per-state参数。
    可定期调用以更新STATE_BASELINES（如月度校准）。
    """
    if timeline is None:
        timeline = load_market_states()
    if data_dict is None:
        all_codes = get_all_local_codes()
        import random
        random.seed(42)
        sample_codes = random.sample(all_codes, min(n_stocks, len(all_codes)))
        data_dict = {}
        for code in sample_codes:
            df = load_stock_data(code)
            if df is not None and len(df) >= 250:
                data_dict[code] = df

    state_date_sets = {}
    for s in ['bull', 'bear', 'sideways', 'transition']:
        state_date_sets[s] = set(t['date'] for t in timeline if t['state'] == s)

    state_stats = {}
    for state in ['bull', 'bear', 'sideways', 'transition']:
        daily_returns, trend_slopes, max_dds, atr_pcts = [], [], [], []
        for code, df in data_dict.items():
            df = df.copy()
            df['date_str'] = df['date'].dt.strftime('%Y-%m-%d')
            mask = df['date_str'].isin(state_date_sets[state])
            sub = df[mask]
            if len(sub) < 20:
                continue
            returns = sub['close'].pct_change().dropna()
            if len(returns) > 0:
                daily_returns.append(returns.mean())
            ma20 = sub['close'].rolling(20).mean()
            slope = ma20.pct_change(5).dropna()
            if len(slope) > 0:
                trend_slopes.append(slope.median())
            rmax = sub['close'].rolling(60, min_periods=20).max()
            dd = (sub['close'] - rmax) / rmax
            dd = dd.dropna()
            if len(dd) > 0:
                max_dds.append(dd.min())
            hl = sub['high'] - sub['low']
            atr = hl.rolling(14).mean()
            atr_pct = (atr / sub['close']).dropna()
            if len(atr_pct) > 0:
                atr_pcts.append(atr_pct.median())

        if not daily_returns:
            continue
        state_stats[state] = {
            'avg_return': np.median(daily_returns),
            'avg_slope': np.median(trend_slopes) if trend_slopes else 0,
            'avg_dd': np.median(max_dds) if max_dds else -0.4,
            'atr_pct': np.median(atr_pcts) if atr_pcts else 0.038,
        }

    if len(state_stats) < 2:
        return STATE_BASELINES  # 数据不足，返回现有值

    # 归一化到[0,1]
    all_slopes = [s['avg_slope'] for s in state_stats.values()]
    all_returns = [s['avg_return'] for s in state_stats.values()]
    all_dds = [s['avg_dd'] for s in state_stats.values()]
    all_atrs = [s['atr_pct'] for s in state_stats.values()]

    slope_rng = max(all_slopes) - min(all_slopes) + 1e-8
    return_rng = max(all_returns) - min(all_returns) + 1e-8
    dd_rng = max(all_dds) - min(all_dds) + 1e-8
    atr_rng = max(all_atrs) - min(all_atrs) + 1e-8
    abs_dds = [abs(d) for d in all_dds]
    abs_dd_rng = max(abs_dds) - min(abs_dds) + 1e-8

    calibrated = {}
    for state, s in state_stats.items():
        slope_norm = (s['avg_slope'] - min(all_slopes)) / slope_rng
        return_norm = (s['avg_return'] - min(all_returns)) / return_rng
        dd_norm = (s['avg_dd'] - min(all_dds)) / dd_rng
        state_score = 0.4 * slope_norm + 0.3 * return_norm + 0.3 * dd_norm

        atr_norm = (s['atr_pct'] - min(all_atrs)) / atr_rng
        dd_depth_norm = (abs(s['avg_dd']) - min(abs_dds)) / abs_dd_rng

        # 11个原参数保持不变
        base = BASELINE_PARAMS.copy()
        # 7个per-state参数数据驱动
        base['base_risk_pct'] = round(max(0.04, min(0.15,
            0.05 + 0.09 * state_score * (1.2 - 0.4 * dd_depth_norm))), 4)
        base['max_concurrent_positions'] = max(2, min(8, round(2 + 5 * state_score)))
        base['take_profit_base'] = round(max(0.25, min(0.55, 0.30 + 0.25 * slope_norm)), 3)
        base['stop_multiplier_base'] = round(max(1.5, min(3.0,
            1.8 + 0.8 * dd_depth_norm + 0.4 * atr_norm)), 2)
        base['add_threshold_base'] = round(max(0.03, min(0.12, 0.04 + 0.06 * (1 - state_score))), 4)
        base['pre_filter_threshold'] = max(25, min(55, round(30 + 20 * (1 - state_score))))
        base['vol_pos_factor'] = round(max(0.08, min(0.25, 0.10 + 0.12 * dd_depth_norm)), 4)
        calibrated[state] = base

    return calibrated

if __name__ == "__main__":
    main()
