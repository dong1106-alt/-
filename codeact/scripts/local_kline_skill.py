#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地"扣子 skill"取数适配层(shim):
主策略以 subprocess 调用 `python <本文件> call kline --param code=X ...`,
原样复刻扣子 stock-data-skill 的 CLI 协议, 但实际从腾讯行情 API 拉前复权日K,
并在本地 parquet 存在时优先复用(离线可用)。
"""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STOCK_DIR = ROOT / "data" / "stocks"
INDEX_DIR = ROOT / "data" / "index"

ENDPOINTS = [
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
    "https://web.ifzq.gtimg.cn/appstock/app/kline/kline",
]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def norm_code(code: str) -> str:
    code = code.strip().lower()
    if code.startswith(("sh", "sz")):
        return code
    return ("sh" if code.startswith("6") or code.startswith("9") else "sz") + code


def fetch_tencent(code: str, count: int, fq: str = "qfq"):
    import requests
    code_upper = code.upper()
    for ep in ENDPOINTS:
        try:
            url = f"{ep}?param={code},day,,,{count},{fq}"
            resp = requests.get(url, timeout=12, headers=HEADERS)
            if resp.status_code != 200 or resp.text.lstrip().startswith("<"):
                continue
            raw = resp.json()
            if raw.get("code") not in (0, None):
                continue
            d = raw.get("data", {}).get(code) or raw.get("data", {}).get(code_upper) or {}
            bars = d.get("qfqday") or d.get("day") or []
            rows = []
            for b in bars:
                try:
                    rows.append({"date": b[0], "open": float(b[1]), "close": float(b[2]),
                                 "high": float(b[3]), "low": float(b[4]),
                                 "volume": float(b[5]) if len(b) > 5 else 0.0})
                except Exception:
                    continue
            if len(rows) >= 5:
                return rows
        except Exception:
            continue
    return None


def load_local(code: str):
    for d in (STOCK_DIR, INDEX_DIR):
        p = d / f"{code}.parquet"
        if p.exists():
            try:
                import pandas as pd
                df = pd.read_parquet(p)
                df["date"] = df["date"].astype(str)
                return df.to_dict("records")
            except Exception:
                continue
    return None


def main():
    ap = argparse.ArgumentParser(description="local kline skill shim")
    ap.add_argument("cmd", nargs="?", default="call")
    ap.add_argument("sub", nargs="?", default="kline")
    ap.add_argument("--param", action="append", default=[], help="key=value")
    args = ap.parse_args()
    if args.cmd != "call" or args.sub != "kline":
        print(json.dumps({"error": "unsupported"}, ensure_ascii=False))
        return 1
    params = {}
    for kv in args.param:
        if "=" in kv:
            k, v = kv.split("=", 1)
            params[k.strip()] = v.strip()
    code = norm_code(params.get("code", ""))
    try:
        count = int(params.get("count", 640))
    except Exception:
        count = 640
    fq = params.get("fq", "qfq")
    if not code:
        print(json.dumps({"error": "empty code"}, ensure_ascii=False))
        return 1
    local = load_local(code)
    if local and len(local) >= max(60, int(count * 0.8)):
        rows = local
    else:
        rows = fetch_tencent(code, count, fq)
        if rows is None:
            rows = local
    if not rows:
        sys.stderr.write(f"kline fetch failed: {code}\n")
        print(json.dumps({"data": []}, ensure_ascii=False))
        return 1
    # 截断到最近 count 条
    rows = rows[-count:]
    print(json.dumps({"data": rows}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
