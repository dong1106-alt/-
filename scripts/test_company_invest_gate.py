#!/usr/bin/env python3
"""公司投资门禁夹具测试：不访问网络，不写主模拟盘。"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from data.company_quality import card_for_signal, evaluate_snapshot  # noqa: E402

MAIN_PORTFOLIO = ROOT / "data" / "sim_trades" / "portfolio.json"


def years_ok():
    return [
        {"report_date": "2024-12-31", "revenue": 100, "profit": 20, "ocf": 22, "roe": 0.15, "debt": 0.40, "revenue_yoy": 0.08, "profit_yoy": 0.06},
        {"report_date": "2023-12-31", "revenue": 92, "profit": 19, "ocf": 20, "roe": 0.14, "debt": 0.41, "revenue_yoy": 0.07, "profit_yoy": 0.05},
        {"report_date": "2022-12-31", "revenue": 86, "profit": 18, "ocf": 18, "roe": 0.13, "debt": 0.42, "revenue_yoy": 0.06, "profit_yoy": 0.04},
    ]


def snap(**kwargs):
    base = {
        "code": "sh600000",
        "name": "测试银行" if kwargs.get("is_financial") else "测试公司",
        "pe": 8.0,
        "is_st": False,
        "data_ok": True,
        "years": years_ok(),
        "roe": 0.15,
        "debt": 0.40,
        "profit": 20,
        "revenue": 100,
        "ocf": 22,
        "is_financial": False,
    }
    base.update(kwargs)
    return base


good = evaluate_snapshot(snap())
assert good["decision"] == "PASS", good
assert good["expected_band"]["earnings_yield"] == 0.125
assert "股价保证" in good["note"]

bank = evaluate_snapshot(snap(name="工商银行", is_financial=True, debt=0.92, years=[
    {**years_ok()[0], "debt": 0.92}, *years_ok()[1:]
]))
assert bank["decision"] == "PASS", bank

loss = evaluate_snapshot(snap(profit=-1, years=[{**years_ok()[0], "profit": -1}, *years_ok()[1:]]))
assert loss["decision"] == "FAIL" and any("亏损" in r for r in loss["reasons"]), loss

st = evaluate_snapshot(snap(name="*ST测试", is_st=True))
assert st["decision"] == "FAIL" and any("ST" in r for r in st["reasons"]), st

missing = evaluate_snapshot(snap(data_ok=False, years=[]))
assert missing["decision"] == "GRAY", missing

expensive = evaluate_snapshot(snap(pe=50.0, years=[
    {**y, "profit_yoy": 0.0, "revenue_yoy": 0.0} for y in years_ok()
]))
assert expensive["decision"] == "FAIL", expensive
assert any("门槛" in r for r in expensive["reasons"]), expensive

weak_cash = evaluate_snapshot(snap(
    ocf=1,
    years=[
        {**years_ok()[0], "ocf": 1, "profit": 20},
        {**years_ok()[1], "ocf": 1, "profit": 19},
        years_ok()[2],
    ],
))
assert weak_cash["decision"] == "FAIL" and any("现金流" in r for r in weak_cash["reasons"]), weak_cash

before = MAIN_PORTFOLIO.read_bytes() if MAIN_PORTFOLIO.exists() else None
buys = [
    {"code": "fixture_pass", "name": "好公司", "suggested_position_pct": 10},
    {"code": "fixture_skip", "name": "差公司", "suggested_position_pct": 10},
]

# apply_gate 会联网；夹具只测 evaluate_snapshot + 手工挂 card，避免扫主盘。
from data.company_quality import card_for_signal
buys[0]["company_gate"] = card_for_signal(good)
buys[1]["company_gate"] = card_for_signal(loss)
kept = [b for b in buys if b["company_gate"]["decision"] == "PASS"]
skipped = [b for b in buys if b["company_gate"]["decision"] != "PASS"]
assert [b["code"] for b in kept] == ["fixture_pass"]
assert [b["code"] for b in skipped] == ["fixture_skip"]

after = MAIN_PORTFOLIO.read_bytes() if MAIN_PORTFOLIO.exists() else None
assert before == after, "门禁测试不得改写主模拟盘"

sys.path.insert(0, str(ROOT / "scripts"))
from daily_wechat_summary import company_gate_brief  # noqa: E402

assert company_gate_brief(ROOT / "no-such.json") is None
tmp = ROOT / "data" / "company_cache" / "_test_signals.json"
tmp.parent.mkdir(parents=True, exist_ok=True)
tmp.write_text(json.dumps({
    "buy_signals": [{"code": "sh600000", "name": "好公司", "company_gate": {"decision": "PASS", "expected_band": {"mid": 0.12}}}],
    "company_gate_skips": [{"code": "sz000001", "name": "差公司", "company_gate": {"decision": "FAIL", "reasons": ["最新年报亏损"]}}],
}, ensure_ascii=False), encoding="utf-8")
brief = company_gate_brief(tmp)
assert brief and "PASS 1" in brief and "拦截 1" in brief, brief
tmp.unlink()

print("company_invest_gate_ok")
