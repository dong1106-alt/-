#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""只读生成 v6 影子组合每日巡检报告。"""
import datetime as dt
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SHADOW = Path(os.environ.get("SHADOW_ROOT", str(ROOT / "data" / "shadow")))


def main():
    ds = dt.date.today().isoformat()
    portfolio_path = SHADOW / "sim_trades" / "portfolio.json"
    if not portfolio_path.exists():
        print(f"[shadow_patrol] 缺少组合文件：{portfolio_path}")
        return 1
    portfolio = json.loads(portfolio_path.read_text(encoding="utf-8"))
    positions = portfolio.get("positions", [])
    total = float(portfolio.get("total_value", portfolio.get("initial_capital", 0)))
    cash = float(portfolio.get("cash", 0))
    pnl = float(portfolio.get("total_pnl", 0))
    pnl_pct = float(portfolio.get("total_pnl_pct", 0))
    drawdown = float(portfolio.get("max_drawdown", 0))
    position_value = sum(float(p.get("current_value", 0)) for p in positions)
    trades = [t for t in portfolio.get("trade_history", []) if t.get("date") == ds]
    lines = [
        f"v6影子组合每日巡检 {ds}",
        "策略：稳健风控候选（仅影子，不切换主模拟盘）",
        f"组合净值：¥{total:,.2f}；累计盈亏：¥{pnl:,.2f}（{pnl_pct:.2f}%）",
        f"现金：¥{cash:,.2f}；持仓市值：¥{position_value:,.2f}；持仓数：{len(positions)}",
        f"最大回撤：{drawdown:.2f}%；今日成交：{len(trades)}笔",
        "持仓：",
    ]
    if positions:
        for p in positions:
            lines.append(
                f"- {p.get('name') or p.get('code')}({p.get('code')}) "
                f"现价{p.get('current_price', 0)}，盈亏{p.get('pnl', 0)} "
                f"({p.get('pnl_pct', 0)}%)，止损{p.get('stop_loss', 0)}"
            )
    else:
        lines.append("- 无持仓")
    if trades:
        lines.append("今日成交明细：")
        for t in trades:
            lines.append(f"- {t.get('action')} {t.get('code')} {t.get('shares')}股 @{t.get('price')}：{t.get('reason', '')}")
    out = SHADOW / "reports" / f"每日巡检_{ds}.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    print(f"[shadow_patrol] 已写入：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
