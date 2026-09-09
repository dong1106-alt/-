#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""评估 v6 影子候选；样本不足时只记录观察，不覆盖主参数。"""
import datetime as dt
import json
import os
from pathlib import Path

from candidate_engine import (
    MAX_DRAWDOWN_PCT,
    MIN_SHADOW_CLOSED_TRADES,
    closed_shadow_trades,
    write_json,
)

ROOT = Path(__file__).resolve().parent.parent
SHADOW = Path(os.environ.get("SHADOW_ROOT", str(ROOT / "data" / "shadow")))


def main():
    ds = dt.date.today().isoformat()
    portfolio_path = SHADOW / "sim_trades" / "portfolio.json"
    meta_path = SHADOW / "shadow_meta.json"
    if not portfolio_path.exists():
        print(f"[shadow_evaluate] 缺少组合文件：{portfolio_path}")
        return 1
    portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
    meta = json.loads(meta_path.read_text(encoding="utf-8")) if meta_path.exists() else {}
    baseline = int(meta.get("baseline_trade_count", 0))
    closed_trades = closed_shadow_trades(portfolio, baseline)
    candidate_id = meta.get("candidate_id", "v6-risk-baseline")
    initial_total = float(meta.get("baseline_main_total_value") or portfolio.get("initial_capital", 0))
    current_total = float(portfolio.get("total_value", 0))
    shadow_delta = round(current_total - initial_total, 2)
    drawdown = abs(float(portfolio.get("max_drawdown", 0) or 0))
    static_ready = candidate_id == "v6-risk-baseline"
    if candidate_id != "v6-risk-baseline":
        active = ROOT / "data" / "candidates" / "active_shadow.json"
        try:
            static_ready = json.loads(active.read_text(encoding="utf-8")).get("candidate_meta", {}).get("candidate_id") == candidate_id
        except (OSError, ValueError, json.JSONDecodeError):
            static_ready = False
    enough = closed_trades >= MIN_SHADOW_CLOSED_TRADES
    if not static_ready:
        decision = "拒绝：候选未通过样本外预筛"
    elif not enough:
        decision = f"待观察：影子平仓{closed_trades}笔，少于{MIN_SHADOW_CLOSED_TRADES}笔"
    elif drawdown > MAX_DRAWDOWN_PCT:
        decision = f"拒绝：影子回撤{drawdown:.2f}%超过{MAX_DRAWDOWN_PCT:.0f}%"
    else:
        decision = "达到影子门槛：生成切换建议，仍需人工审核、Git提交后部署"
    result = {
        "date": ds,
        "strategy": "v6-shadow",
        "candidate_id": candidate_id,
        "baseline_trade_count": baseline,
        "shadow_closed_trades": closed_trades,
        "shadow_pnl": portfolio.get("total_pnl", 0),
        "shadow_drawdown": -drawdown,
        "shadow_delta_since_baseline": shadow_delta,
        "decision": decision,
        "main_params_unchanged": True,
    }
    out_json = SHADOW / "evaluation" / f"candidate_{ds}.json"
    out_txt = SHADOW / "reports" / f"候选评估_{ds}.txt"
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_txt.parent.mkdir(parents=True, exist_ok=True)
    write_json(out_json, result)
    out_txt.write_text(
        f"v6候选参数评估 {ds}\n"
        f"候选：{candidate_id}（单票上限25%，新开仓最大止损距离12%，持仓超时20天，趋势失效退出）\n"
        f"影子平仓：{closed_trades}笔；影子阶段盈亏：{shadow_delta}；最大回撤：-{drawdown}%\n"
        f"结论：{decision}\n"
        "说明：本评估不覆盖主模拟盘或 data/optimal_params.json。\n",
        encoding="utf-8",
    )
    if decision.startswith("达到影子门槛"):
        write_json(ROOT / "data" / "candidates" / "promotion_recommendation.json", result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
