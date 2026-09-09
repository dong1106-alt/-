#!/usr/bin/env python3
"""
估值数据模块 — 龟缠安泰 v5.3.4.1
通过腾讯批量行情API获取PE/PB/股票名称，通过东方财富API获取PE/PB历史分位。
本地缓存每月更新一次。
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===

import os
import sys
import json
import time
import requests
from datetime import datetime, timedelta

# 路径配置
_BASE_DIR = f"{_ROOT}"
_CACHE_DIR = os.path.join(_BASE_DIR, "data", "valuation_cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "valuation_cache.json")

# API
TENCENT_QUOTE_API = "https://qt.gtimg.cn/q="
EASTMONEY_VALUATION_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"

# 批量请求大小
BATCH_SIZE = 50

# 缓存有效期（天）
CACHE_TTL_DAYS = 30


def _code_to_tencent(code):
    """sh600396 → sh600396（腾讯格式与我们的格式一致）"""
    return code


def _code_to_eastmoney(code):
    """sh600396 → 600396, sz002484 → 002484"""
    return code[2:]


def _code_to_eastmoney_secid(code):
    """sh600396 → 1.600396, sz002484 → 0.002484"""
    market = "1" if code.startswith("sh") else "0"
    return f"{market}.{code[2:]}"


def fetch_batch_quotes(codes):
    """
    批量获取腾讯实时行情，返回 PE/PB/名称/ST标记。
    
    Args:
        codes: ['sh600396', 'sz002484', ...]
    
    Returns:
        {code: {'name': str, 'price': float, 'pe': float, 'pb': float, 'is_st': bool}}
    """
    result = {}
    for i in range(0, len(codes), BATCH_SIZE):
        batch = codes[i:i + BATCH_SIZE]
        query = ",".join(batch)
        try:
            r = requests.get(f"{TENCENT_QUOTE_API}{query}", timeout=10)
            lines = r.text.strip().split(";")
            for line in lines:
                if not line.strip():
                    continue
                try:
                    fields = line.split("~")
                    if len(fields) < 47:
                        continue
                    name = fields[1]
                    code_raw = fields[2]
                    market_prefix = "sh" if fields[0] == "1" else "sz"
                    code = f"{market_prefix}{code_raw}"
                    
                    price = float(fields[3]) if fields[3] else 0
                    pe_str = fields[39]
                    pb_str = fields[46]
                    pe = float(pe_str) if pe_str and pe_str != "" else None
                    pb = float(pb_str) if pb_str and pb_str != "" else None
                    
                    # ST检测：名称包含ST/*ST
                    is_st = "ST" in name.upper() or "*ST" in name.upper()
                    
                    result[code] = {
                        'name': name,
                        'price': price,
                        'pe': pe,
                        'pb': pb,
                        'is_st': is_st,
                    }
                except (ValueError, IndexError):
                    continue
        except Exception as e:
            print(f"  [估值] 批量行情请求失败({i//BATCH_SIZE+1}): {e}")
            continue
        time.sleep(0.1)  # 请求间隔
    
    return result


def fetch_valuation_percentile(code):
    """
    从东方财富API获取PE/PB历史分位。
    
    Returns:
        {'pe_percentile': float(0-100), 'pb_percentile': float(0-100)} or None
    """
    em_code = _code_to_eastmoney(code)
    try:
        params = {
            'sortColumns': 'TRADE_DATE',
            'sortTypes': '-1',
            'pageSize': '10',
            'columns': 'ALL',
            'reportName': 'RPT_VALUATIONSTATUS',
            'filter': f'(SECURITY_CODE="{em_code}")',
        }
        r = requests.get(EASTMONEY_VALUATION_API, params=params, timeout=10)
        data = r.json()
        if not data.get('success') or not data.get('result'):
            return None
        
        rows = data['result'].get('data', [])
        pe_pct = None
        pb_pct = None
        for row in rows:
            if row['INDICATOR_TYPE'] == '1':  # PE TTM
                pe_pct = row['INDEX_PERCENTILE'] / 100.0  # 转为0-1
            elif row['INDICATOR_TYPE'] == '2':  # PB
                pb_pct = row['INDEX_PERCENTILE'] / 100.0
        
        return {'pe_percentile': pe_pct, 'pb_percentile': pb_pct}
    except Exception as e:
        return None


def update_cache(codes=None, force=False):
    """
    更新估值缓存。每月自动更新一次，或force=True强制更新。
    
    Args:
        codes: 股票代码列表。None则从all_main_board_codes.txt加载
        force: 是否强制更新
    """
    # 检查缓存是否过期
    if not force and os.path.exists(_CACHE_FILE):
        mtime = os.path.getmtime(_CACHE_FILE)
        age_days = (time.time() - mtime) / 86400
        if age_days < CACHE_TTL_DAYS:
            print(f"  [估值] 缓存{age_days:.1f}天前更新，跳过（有效期{CACHE_TTL_DAYS}天）")
            return load_cache()
    
    os.makedirs(_CACHE_DIR, exist_ok=True)
    
    # 加载股票代码
    if codes is None:
        codes_file = os.path.join(_BASE_DIR, "data", "all_main_board_codes.txt")
        if os.path.exists(codes_file):
            codes = []
            with open(codes_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line:
                        parts = line.split(",", 1)
                        codes.append(parts[0])
        else:
            print("  [估值] 未找到股票代码文件")
            return {}
    
    print(f"  [估值] 开始更新{len(codes)}只股票的估值缓存...")
    
    # Step 1: 批量获取腾讯行情（PE/PB/名称/ST）
    quotes = fetch_batch_quotes(codes)
    print(f"  [估值] 腾讯行情: {len(quotes)}/{len(codes)} 成功")
    
    # Step 2: 获取东方财富PE/PB分位（逐只，但只获取有PE/PB数据的）
    cache = {}
    success_pct = 0
    for code in codes:
        q = quotes.get(code)
        if q is None:
            continue
        
        entry = {
            'name': q['name'],
            'pe': q['pe'],
            'pb': q['pb'],
            'is_st': q['is_st'],
            'pe_percentile': None,
            'pb_percentile': None,
            'update_date': datetime.now().strftime('%Y-%m-%d'),
        }
        
        # 获取PE/PB历史分位（仅非ST股票）
        if not q['is_st'] and q['pe'] is not None:
            pct = fetch_valuation_percentile(code)
            if pct:
                entry['pe_percentile'] = pct['pe_percentile']
                entry['pb_percentile'] = pct['pb_percentile']
                success_pct += 1
        
        cache[code] = entry
    
    print(f"  [估值] 东方财富分位: {success_pct} 成功")
    
    # 保存缓存
    with open(_CACHE_FILE, 'w', encoding='utf-8') as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    
    print(f"  [估值] 缓存已保存: {_CACHE_FILE} ({len(cache)}只)")
    return cache


def load_cache():
    """加载本地估值缓存。"""
    if not os.path.exists(_CACHE_FILE):
        return {}
    try:
        with open(_CACHE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def get_valuation(code):
    """
    获取单只股票的估值数据（从缓存）。
    
    Returns:
        {'name', 'pe', 'pb', 'is_st', 'pe_percentile', 'pb_percentile'} or None
    """
    cache = load_cache()
    return cache.get(code)


def is_st_stock(code):
    """检查股票是否为ST/*ST/退市股。"""
    v = get_valuation(code)
    if v is not None:
        return v.get('is_st', False)
    # 缓存中没有，检查股票代码文件中的名称
    codes_file = os.path.join(_BASE_DIR, "data", "all_main_board_codes.txt")
    if os.path.exists(codes_file):
        with open(codes_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line:
                    parts = line.split(",", 1)
                    if parts[0] == code:
                        name = parts[1] if len(parts) > 1 else ""
                        return "ST" in name.upper() or "退" in name
    return False


def get_valuation_percentile(code, date=None):
    """
    获取指定股票在指定日期的估值分位。
    
    对于实时数据（date=None或今天），从缓存读取东方财富分位。
    对于历史日期，缓存中无历史数据，返回None（调用方应回退到价格分位）。
    
    Returns:
        {'pe_percentile': float(0-1), 'pb_percentile': float(0-1)} or None
    """
    v = get_valuation(code)
    if v is None:
        return None
    
    pe_pct = v.get('pe_percentile')
    pb_pct = v.get('pb_percentile')
    
    if pe_pct is None and pb_pct is None:
        return None
    
    return {
        'pe_percentile': pe_pct,
        'pb_percentile': pb_pct,
    }


def get_valuation_series(code, df, lookback=250):
    """
    为回测生成估值分位序列。
    
    由于沙箱无法获取历史PE/PB数据，此函数采用混合策略：
    1. 如果缓存中有当前PE/PB值，用当前EPS/BPS反推历史PE/PB
    2. 否则返回None，调用方回退到价格分位
    
    Args:
        code: 股票代码
        df: 含close列的DataFrame
        lookback: 回看天数
    
    Returns:
        pd.Series (pe_percentile) or None
    """
    v = get_valuation(code)
    if v is None or v.get('pe') is None or v.get('pe', 0) <= 0:
        return None
    
    import pandas as pd
    import numpy as np
    
    # 当前PE和价格
    current_pe = v['pe']
    current_pb = v.get('pb')
    
    # 用当前PE和当前价格计算EPS，然后反推历史PE
    # EPS = Price / PE（假设EPS在回测期内不变，近似）
    # 历史PE = 历史Price / EPS
    current_price = v.get('price', df['close'].iloc[-1])
    if current_price <= 0:
        return None
    
    eps = current_price / current_pe
    historical_pe = df['close'] / eps
    
    # 计算PE的历史分位（rolling）
    pe_pct = historical_pe.rolling(lookback, min_periods=lookback).rank(pct=True)
    
    # 如果有PB，也计算
    if current_pb and current_pb > 0:
        bps = current_price / current_pb
        historical_pb = df['close'] / bps
        pb_pct = historical_pb.rolling(lookback, min_periods=lookback).rank(pct=True)
        # 综合分位 = (PE分位 + PB分位) / 2
        combined = (pe_pct + pb_pct) / 2
        return combined
    else:
        return pe_pct


if __name__ == '__main__':
    # 测试
    print("=== 估值模块测试 ===")
    cache = update_cache(['sh600396', 'sh601398', 'sz000001', 'sh600519', 'sz002484'], force=True)
    for code, v in cache.items():
        print(f"  {code} {v['name']:8s} PE={v['pe']:8s} PB={v['pb']:8s} "
              f"PE分位={v['pe_percentile']} PB分位={v['pb_percentile']} ST={v['is_st']}")