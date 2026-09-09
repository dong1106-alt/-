# -*- coding: utf-8 -*-
"""
本地每日巡检模块(替代云端未导出的 daily_pipeline)。
只做只读巡检: 读取模拟盘 portfolio.json, 汇总净值/回撤/持仓并写 reports/每日巡检_日期.txt。
不改动任何交易/风控数据与逻辑。
"""
import datetime
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
REPORTS_DIR = ROOT / "reports"
PORTFOLIO_FILE = DATA_DIR / "sim_trades" / "portfolio.json"


def run_daily_pipeline() -> str:
    """巡检主入口: 返回报告文本(str)。"""
    now = datetime.datetime.now()
    today = now.strftime("%Y-%m-%d")
    lines = []
    lines.append("=" * 60)
    lines.append(f"每日巡检 {today} {now.strftime('%H:%M:%S')}")
    lines.append("=" * 60)
    failed = False
    try:
        if not PORTFOLIO_FILE.exists():
            raise FileNotFoundError(f"未找到 {PORTFOLIO_FILE}（模拟盘尚未建立或已清空）")
        with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
            p = json.load(f)
        lines.append(f"初始资金: {p.get('initial_capital', 'N/A')}")
        lines.append(f"当前总资产: {p.get('total_value', 'N/A')}")
        lines.append(f"累计盈亏: {p.get('total_pnl_pct', 'N/A')}%")
        lines.append(f"峰值权益: {p.get('max_value', p.get('peak_equity', 'N/A'))}")
        lines.append(f"最大回撤: {p.get('max_drawdown', 'N/A')}%")
        lines.append(f"最后处理日期: {p.get('last_processed_date', p.get('current_date', 'N/A'))}")
        positions = p.get("positions") or p.get("holdings") or []
        lines.append(f"当前持仓数: {len(positions) if isinstance(positions, list) else 'N/A'}")
        cash = p.get("cash")
        if cash is not None:
            lines.append(f"可用现金: {cash}")
        dd = p.get("max_drawdown")
        if isinstance(dd, (int, float)) and dd < -4.0:
            lines.append("提示: 当前最大回撤超过 4%，注意观察。")
        lines.append("巡检完成。")
    except Exception as e:
        failed = True
        lines.append(f"巡检异常: {type(e).__name__}: {e}")
    report = "\n".join(lines)
    try:
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)
        (REPORTS_DIR / f"每日巡检_{today}.txt").write_text(report, encoding="utf-8")
    except Exception:
        pass
    if failed:
        raise RuntimeError(report)
    return report


if __name__ == "__main__":
    print(run_daily_pipeline())
