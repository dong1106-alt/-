#!/usr/bin/env python3
"""公司投资门禁：财务快照 + PASS/FAIL/GRAY + 盈利收益率区间。

只服务当日扫描实盘信号，不进入历史回测。数字必须来自快照字段，不编造。
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent

import json
import os
import time
from datetime import datetime, timedelta

import requests

_CACHE_DIR = os.path.join(_ROOT, "data", "company_cache")
CACHE_TTL_DAYS = 7
EASTMONEY_DATA = "https://datacenter-web.eastmoney.com/api/data/v1/get"
ZYZB_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/ZYZBAjaxNew"
TENCENT_QUOTE_API = "https://qt.gtimg.cn/q="
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

MIN_EXPECTED_MID = float(os.environ.get("COMPANY_MIN_EXPECTED_YIELD", "0.06"))
GROWTH_FLOOR = -0.20
GROWTH_CEIL = 0.25
OCF_TO_PROFIT_MIN = 0.30
DEBT_FAIL = 0.85
FINANCIAL_NAME_MARKERS = ("银行", "保险", "证券", "信托")
BAND_PAD = 0.03


def _to_float(v):
    if v is None or v == "" or v == "-":
        return None
    try:
        x = float(str(v).replace("%", "").replace(",", "").strip())
        if x != x or x in (float("inf"), float("-inf")):
            return None
        return x
    except (TypeError, ValueError):
        return None


def _as_ratio(v):
    """百分数 12.3 / 12.3% / 0.123 → 小数。无法判断时原样当小数。"""
    x = _to_float(v)
    if x is None:
        return None
    if abs(x) > 1.5:
        return x / 100.0
    return x


def _code_em(code):
    return code[2:] if len(code) > 2 else code


def _code_em_pref(code):
    raw = code.lower()
    if raw.startswith("sh"):
        return "SH" + raw[2:]
    if raw.startswith("sz"):
        return "SZ" + raw[2:]
    return code.upper()


def _pick(row, *keys):
    for k in keys:
        if k in row and row[k] not in (None, "", "-"):
            return row[k]
    return None


def _is_financial(name):
    name = name or ""
    return any(m in name for m in FINANCIAL_NAME_MARKERS)


def _annual_rows(rows):
    annual = []
    for row in rows:
        date = str(_pick(row, "REPORT_DATE", "REPORTDATE", "date", "REPORT_DATE_NAME") or "")
        rtype = str(_pick(row, "REPORT_TYPE", "REPORTTYPENAME", "STD_REPORT_DATE") or "")
        if "一季" in rtype or "三季" in rtype or "中报" in rtype or "季报" in rtype:
            continue
        if date[5:10] in ("12-31", "12/31") or "年报" in rtype or date.endswith("1231"):
            annual.append(row)
        elif not rtype and date[5:10] == "12-31":
            annual.append(row)
    if not annual:
        return rows[:4]
    return annual[:4]


def _normalize_row(row):
    revenue = _to_float(_pick(
        row, "TOTAL_OPERATE_INCOME", "TOTALOPERATEREVE", "yyzsr", "OPERATE_INCOME",
    ))
    profit = _to_float(_pick(
        row, "PARENT_NETPROFIT", "PARENTNETPROFIT", "gsjlr", "NETPROFIT", "jlr",
    ))
    ocf = _to_float(_pick(
        row, "NETCASH_OPERATE", "NETCASHFLOWOPERATE", "jyxjl", "jyhdcsdxjllje",
        "NETCASHFLOW_OPERATE",
    ))
    roe = _as_ratio(_pick(row, "ROE_WEIGHT", "ROE", "jzcsyl", "ROEJQ", "WEIGHTAVG_ROE"))
    debt = _as_ratio(_pick(row, "DEBT_ASSET_RATIO", "zcfzl", "ASSETLIAB_RATIO", "ZCFZL"))
    rev_yoy = _as_ratio(_pick(
        row, "TOTAL_OPERATE_INCOME_YOY", "yyzsrtb", "OPERATE_INCOME_YOY", "yyzsr_yoy",
    ))
    profit_yoy = _as_ratio(_pick(
        row, "PARENT_NETPROFIT_YOY", "gsjlrtb", "NETPROFIT_YOY", "gsjlr_yoy",
    ))
    report_date = str(_pick(row, "REPORT_DATE", "date", "REPORTDATE") or "")[:10]
    return {
        "report_date": report_date,
        "revenue": revenue,
        "profit": profit,
        "ocf": ocf,
        "roe": roe,
        "debt": debt,
        "revenue_yoy": rev_yoy,
        "profit_yoy": profit_yoy,
    }


def fetch_finance_rows(code, timeout=12):
    em_code = _code_em(code)
    params = {
        "sortColumns": "REPORT_DATE",
        "sortTypes": "-1",
        "pageSize": "12",
        "pageNumber": "1",
        "reportName": "RPT_F10_FINANCE_MAINFINADATA",
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{em_code}")',
        "source": "HSF10",
        "client": "APP",
    }
    r = requests.get(EASTMONEY_DATA, params=params, timeout=timeout, headers=HTTP_HEADERS)
    r.raise_for_status()
    payload = r.json()
    rows = ((payload.get("result") or {}).get("data")) or []
    if rows:
        return rows
    z = requests.get(
        ZYZB_URL,
        params={"companyType": "4", "reportDateType": "0", "code": _code_em_pref(code)},
        timeout=timeout,
        headers=HTTP_HEADERS,
    )
    z.raise_for_status()
    zdata = z.json()
    data = zdata.get("data") if isinstance(zdata, dict) else zdata
    if not data:
        return []
    return data if isinstance(data, list) else []


def fetch_pe_and_name(code, timeout=8):
    name, pe, is_st = None, None, False
    try:
        from data.valuation import get_valuation, is_st_stock
        v = get_valuation(code)
        if v:
            name = v.get("name") or name
            pe = _to_float(v.get("pe"))
            is_st = bool(v.get("is_st"))
        if not is_st:
            is_st = bool(is_st_stock(code))
    except Exception:
        pass
    if pe is None or not name:
        try:
            resp = requests.get(f"{TENCENT_QUOTE_API}{code}", timeout=timeout, headers=HTTP_HEADERS)
            resp.encoding = "gbk"
            text = resp.text.strip()
            if "=" in text:
                payload = text.split("=", 1)[1].strip(' ";\n')
                fields = payload.split("~")
                if len(fields) > 46:
                    name = name or fields[1]
                    pe = pe if pe is not None else (_to_float(fields[39]) if fields[39] else None)
                    nm = name or ""
                    is_st = is_st or ("ST" in nm.upper())
        except Exception:
            pass
    return name, pe, is_st


def build_snapshot(code, finance_rows, name=None, pe=None, is_st=False):
    annual = [_normalize_row(r) for r in _annual_rows(finance_rows or [])]
    latest = annual[0] if annual else {}
    return {
        "code": code,
        "name": name or "",
        "pe": pe,
        "is_st": bool(is_st),
        "data_ok": bool(annual),
        "years": annual[:3],
        "roe": latest.get("roe"),
        "debt": latest.get("debt"),
        "profit": latest.get("profit"),
        "revenue": latest.get("revenue"),
        "ocf": latest.get("ocf"),
        "is_financial": _is_financial(name),
    }


def _growth_cap(years):
    yoys = [y.get("profit_yoy") for y in years if y.get("profit_yoy") is not None]
    if len(yoys) < 2:
        rev = [y.get("revenue_yoy") for y in years if y.get("revenue_yoy") is not None]
        yoys = rev
    if not yoys:
        return 0.0, False
    avg = sum(yoys) / len(yoys)
    return max(GROWTH_FLOOR, min(GROWTH_CEIL, avg)), True


def expected_band(pe, years):
    if pe is None or pe <= 0:
        return None
    ey = 1.0 / pe
    g, _ = _growth_cap(years)
    mid = ey + g
    return {
        "earnings_yield": round(ey, 4),
        "growth_cap": round(g, 4),
        "low": round(mid - BAND_PAD, 4),
        "mid": round(mid, 4),
        "high": round(mid + BAND_PAD, 4),
        "note": "公司盈利口径，不是股价保证",
    }


def evaluate_snapshot(snap, min_expected_mid=None):
    """纯规则。snap 由 build_snapshot 或测试夹具提供。"""
    floor = MIN_EXPECTED_MID if min_expected_mid is None else min_expected_mid
    reasons = []
    decision = "PASS"
    years = snap.get("years") or []
    name = snap.get("name") or ""
    pe = _to_float(snap.get("pe"))
    is_st = bool(snap.get("is_st")) or ("ST" in name.upper())
    financial = bool(snap.get("is_financial")) or _is_financial(name)

    if is_st:
        return _pack("FAIL", ["ST/*ST"], snap, expected_band(pe, years) if pe and pe > 0 else None)
    if not snap.get("data_ok") or not years:
        return _pack("GRAY", ["财务数据缺失"], snap, None)
    latest = years[0]
    profit = _to_float(snap.get("profit"))
    if profit is None:
        profit = _to_float(latest.get("profit"))
    roe = _to_float(snap.get("roe"))
    if roe is None:
        roe = _to_float(latest.get("roe"))
    debt = _to_float(snap.get("debt"))
    if debt is None:
        debt = _to_float(latest.get("debt"))
    ocf = _to_float(snap.get("ocf"))
    if ocf is None:
        ocf = _to_float(latest.get("ocf"))

    missing = []
    if pe is None:
        missing.append("PE")
    if profit is None:
        missing.append("净利润")
    if roe is None:
        missing.append("ROE")
    if missing:
        return _pack("GRAY", [f"关键字段缺失：{'/'.join(missing)}"], snap, None)
    if pe <= 0:
        decision = "FAIL"
        reasons.append("PE<=0")
    if profit < 0:
        decision = "FAIL"
        reasons.append("最新年报亏损")

    if not financial and debt is not None and debt > DEBT_FAIL:
        decision = "FAIL"
        reasons.append(f"资产负债率{debt:.0%}>阈值")

    if profit and profit > 0 and ocf is not None and ocf < profit * OCF_TO_PROFIT_MIN:
        weak_years = 0
        for y in years[:2]:
            yp, yo = y.get("profit"), y.get("ocf")
            if yp and yp > 0 and yo is not None and yo < yp * OCF_TO_PROFIT_MIN:
                weak_years += 1
        if weak_years >= 2 or (len(years) < 2 and ocf < profit * OCF_TO_PROFIT_MIN):
            decision = "FAIL"
            reasons.append("经营现金流长期弱于净利润")

    if len(years) >= 3:
        rev_down = all(
            (years[i].get("revenue") is not None and years[i + 1].get("revenue") is not None
             and years[i]["revenue"] < years[i + 1]["revenue"])
            for i in range(2)
        )
        pft_down = all(
            (years[i].get("profit") is not None and years[i + 1].get("profit") is not None
             and years[i]["profit"] < years[i + 1]["profit"])
            for i in range(2)
        )
        yoy_neg = all(
            (y.get("revenue_yoy") is not None and y["revenue_yoy"] < 0
             and y.get("profit_yoy") is not None and y["profit_yoy"] < 0)
            for y in years[:3]
        )
        if (rev_down and pft_down) or yoy_neg:
            decision = "FAIL"
            reasons.append("营收与利润连续恶化")

    band = expected_band(pe, years)
    if band is None:
        return _pack("GRAY", reasons + ["无法计算盈利收益率"], snap, None)
    if band["mid"] < floor:
        decision = "FAIL"
        reasons.append(f"预期中值{band['mid']:.1%}低于{floor:.0%}门槛")

    if decision == "PASS" and not reasons:
        reasons.append("财务与收益门槛通过")
    return _pack(decision, reasons, snap, band)


def _pack(decision, reasons, snap, band):
    return {
        "decision": decision,
        "reasons": reasons,
        "code": snap.get("code"),
        "name": snap.get("name") or "",
        "pe": snap.get("pe"),
        "roe": snap.get("roe"),
        "expected_band": band,
        "note": "公司盈利口径，不是股价保证",
    }


def _cache_path(code):
    os.makedirs(_CACHE_DIR, exist_ok=True)
    return os.path.join(_CACHE_DIR, f"{code}.json")


def _load_cache(code):
    path = _cache_path(code)
    if not os.path.exists(path):
        return None
    try:
        payload = json.loads(open(path, encoding="utf-8").read())
        ts = datetime.fromisoformat(payload["fetched_at"])
        if datetime.now() - ts > timedelta(days=CACHE_TTL_DAYS):
            return None
        return payload
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _save_cache(code, snapshot, evaluation):
    path = _cache_path(code)
    payload = {
        "fetched_at": datetime.now().isoformat(timespec="seconds"),
        "snapshot": snapshot,
        "evaluation": evaluation,
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def evaluate_code(code, name=""):
    cached = _load_cache(code)
    if cached and cached.get("evaluation"):
        ev = cached["evaluation"]
        if name and not ev.get("name"):
            ev["name"] = name
        return ev
    try:
        rows = fetch_finance_rows(code)
        fetched_name, pe, is_st = fetch_pe_and_name(code)
        snap = build_snapshot(
            code, rows, name=name or fetched_name, pe=pe, is_st=is_st,
        )
        ev = evaluate_snapshot(snap)
        _save_cache(code, snap, ev)
        time.sleep(0.15)
        return ev
    except Exception as exc:
        ev = _pack("GRAY", [f"拉取失败：{type(exc).__name__}"], {"code": code, "name": name}, None)
        return ev


def card_for_signal(evaluation):
    band = evaluation.get("expected_band") or {}
    return {
        "decision": evaluation.get("decision"),
        "reasons": evaluation.get("reasons") or [],
        "earnings_yield": band.get("earnings_yield"),
        "growth_cap": band.get("growth_cap"),
        "expected_band": band,
        "note": evaluation.get("note") or "公司盈利口径，不是股价保证",
    }


def apply_gate(buy_signals):
    """对已有买入信号做门禁。只拉这些代码的财务，不全市场扫描。"""
    kept, skipped = [], []
    for item in buy_signals:
        code = item.get("code")
        ev = evaluate_code(code, name=item.get("name") or "")
        item["company_gate"] = card_for_signal(ev)
        if ev.get("decision") == "PASS":
            kept.append(item)
        else:
            skipped.append(item)
    return kept, skipped
