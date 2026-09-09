#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
超级智能体 · 个股量化分析 HTTP 接口（本地服务）

用途：把「龟缠量化v6」的个股信号分析包成可被外部按次调用的接口，
      策略代码留在本机、不对外泄露，只暴露「输入股票代码 → 输出分析结果」。

输入（POST /analyze，JSON）：
  {"codes": ["sh600000"], "analysis_date": "2026-09-05"}
  或 {"code": "sh600000"}

输出（JSON）：
  {"ok": true, "market_state": "...", "analysis_date": "...",
   "results": [ {code, name, conclusion, signal_type, entry_score, close, change_pct, report, detail} ],
   "disclaimer": "仅供研究参考，不构成任何投资建议；据此操作风险自负。"}

免责声明：本接口只做研究/数据分析，不构成任何投资建议。
"""

import json
import os
import sys
import threading
import datetime
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import io
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                      # 超级智能体 目录
SCRIPTS = os.path.join(ROOT, "scripts")
sys.path.insert(0, SCRIPTS)

import guichan_v6_stock_signal_analysis as g       # 导入分析模块（不会执行 main）

HOST = os.environ.get("GUICHAN_API_HOST", "127.0.0.1")
PORT = int(os.environ.get("GUICHAN_API_PORT", "8000"))
API_KEY = os.environ.get("GUICHAN_API_KEY", "").strip()
MAX_BODY_BYTES = int(os.environ.get("GUICHAN_MAX_BODY_BYTES", "16384"))
MAX_CODES = int(os.environ.get("GUICHAN_MAX_CODES", "10"))
RATE_LIMIT_PER_MIN = int(os.environ.get("GUICHAN_RATE_LIMIT_PER_MIN", "30"))
DISCLAIMER = "仅供研究参考，不构成任何投资建议；据此操作风险自负。"

# 策略主模块缓存（210KB 大文件，只在首次调用时导入一次）
_STRAT = None
_STRAT_LOCK = threading.Lock()
_RATE_LOCK = threading.Lock()
_RATE_BUCKETS = {}
_CODE_RE = re.compile(r"^(?:sh|sz|bj)?[0-9A-Za-z._-]{2,20}$", re.I)


def _allow_request(client_ip):
    """Small in-process per-IP limiter; Caddy should provide the outer limiter."""
    now = time.time()
    with _RATE_LOCK:
        bucket = [t for t in _RATE_BUCKETS.get(client_ip, []) if now - t < 60]
        if len(bucket) >= RATE_LIMIT_PER_MIN:
            _RATE_BUCKETS[client_ip] = bucket
            return False
        bucket.append(now)
        _RATE_BUCKETS[client_ip] = bucket
        if len(_RATE_BUCKETS) > 2000:
            for ip, ts in list(_RATE_BUCKETS.items()):
                if not ts or now - ts[-1] > 120:
                    _RATE_BUCKETS.pop(ip, None)
        return True


def _get_strategy():
    global _STRAT
    if _STRAT is None:
        with _STRAT_LOCK:
            if _STRAT is None:
                _STRAT = g.import_strategy()
    return _STRAT


def _jsonable(obj):
    """把 numpy/pandas 标量转成标准 JSON 类型。"""
    import numpy as np
    import pandas as pd
    if obj is None:
        return None
    if isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return float(obj)
    if isinstance(obj, np.generic):
        return _jsonable(obj.item())
    if isinstance(obj, (pd.Timestamp, datetime.datetime, datetime.date)):
        return str(obj)
    if isinstance(obj, (list, tuple)):
        return [_jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    return str(obj)


def _analyze(codes, analysis_date):
    mod = _get_strategy()
    cfg = mod.DEFAULT_CONFIG.copy()
    cfg["strategy"] = dict(cfg["strategy"])
    cfg["plot_enable"] = False

    market_state = g.load_state_on(analysis_date)
    state_name = market_state.get("state", "sideways")
    opt_params = g.load_optimal_params(state_name)
    quotes = g.fetch_quotes(codes)
    index_df = g.fetch_kline("sh000001", "day", 640)

    import pandas as pd
    index_df = index_df[index_df["date"] <= pd.to_datetime(analysis_date)].copy()

    results, errors = [], []
    for code in codes:
        try:
            r = g.analyze_one(code, analysis_date, mod, cfg, opt_params, market_state, index_df, quotes)
            results.append(r)
        except Exception as e:
            errors.append({"code": code, "error": str(e)})

    if not results and errors:
        raise RuntimeError("; ".join([f"{x['code']}: {x['error']}" for x in errors]))

    out = []
    for r in results:
        out.append({
            "code": r.get("code"),
            "name": r.get("name"),
            "conclusion": (r.get("signal") or {}).get("conclusion"),
            "signal_type": (r.get("signal") or {}).get("signal_type"),
            "entry_score": _jsonable((r.get("signal") or {}).get("entry_score")),
            "close": _jsonable((r.get("indicators") or {}).get("close")),
            "change_pct": _jsonable((r.get("indicators") or {}).get("change_pct")),
            "report": g.render_report([r], [], analysis_date),
            "detail": _jsonable(r),
        })

    return {
        "market_state": state_name,
        "analysis_date": analysis_date,
        "results": out,
        "failed_items": errors,
        "disclaimer": DISCLAIMER,
    }


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path in ("/", "/health"):
            self._send(200, {"ok": True, "service": "guichan-v6-stock-analysis", "disclaimer": DISCLAIMER})
        else:
            self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        if self.path != "/analyze":
            self._send(404, {"ok": False, "error": "not found"})
            return
        if API_KEY and self.headers.get("X-API-Key", "") != API_KEY:
            self._send(401, {"ok": False, "error": "未授权"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length > MAX_BODY_BYTES:
                self._send(413, {"ok": False, "error": "请求体过大"})
                return
            if not _allow_request(self.client_address[0]):
                self._send(429, {"ok": False, "error": "请求过于频繁，请稍后重试"})
                return
            raw = self.rfile.read(length) if length else b"{}"
            body = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._send(400, {"ok": False, "error": f"invalid json: {e}"})
            return

        codes = body.get("codes") or ([body["code"]] if body.get("code") else None)
        if not codes or not isinstance(codes, list):
            self._send(400, {"ok": False, "error": "缺少 codes（数组）或 code"})
            return
        codes = [str(c).strip() for c in codes if str(c).strip()]
        if not codes or len(codes) > MAX_CODES:
            self._send(400, {"ok": False, "error": f"股票代码数量必须为 1-{MAX_CODES}"})
            return
        invalid = [c for c in codes if not _CODE_RE.fullmatch(c)]
        if invalid:
            self._send(400, {"ok": False, "error": "股票代码格式无效", "invalid_codes": invalid})
            return

        analysis_date = str(body.get("analysis_date") or datetime.date.today().isoformat())

        try:
            result = _analyze(codes, analysis_date)
            self._send(200, {"ok": True, **result})
        except Exception as e:
            self._send(500, {"ok": False, "error": str(e)})

    def log_message(self, fmt, *args):
        pass


def main():
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"guichan-v6 API 已启动：http://{HOST}:{PORT}/analyze", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()


