#!/usr/bin/env python3
"""
分段止盈烟雾测试 - 验证calc_segmented_tp_state和相关逻辑
不修改portfolio.json，只读取数据并打印分析结果
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===
import sys
sys.path.insert(0, f"{_ROOT}/codeact/scripts")
import json
import requests

KLINE_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

# 导入sim_trade_tracker中的函数
import importlib.util
spec = importlib.util.spec_from_file_location(
    "sim_trade_tracker",
    f"{_ROOT}/codeact/scripts/sim_trade_tracker.py"
)
sim = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sim)

PORTFOLIO_FILE = f"{_ROOT}/data/sim_trades/portfolio.json"

def main():
    with open(PORTFOLIO_FILE, "r", encoding="utf-8") as f:
        portfolio = json.load(f)

    print("=" * 80)
    print("分段止盈烟雾测试 - 分析当前10只持仓")
    print("=" * 80)

    for pos in portfolio["positions"]:
        code = pos["code"]
        name = pos.get("name", "")
        entry = pos["entry_price"]
        shares = pos["shares"]
        sl = pos["stop_loss"]
        tp = pos.get("take_profit", 0)

        # 获取当日OHLC
        ohlc = sim.fetch_latest_ohlc(code)
        if ohlc is None:
            print(f"\n{code} {name}: 无法获取OHLC，跳过")
            continue

        close = ohlc["close"]
        high = ohlc["high"]
        low = ohlc["low"]
        pnl_pct = (close - entry) / entry * 100

        # 计算分段止盈状态
        tp_state = sim.calc_segmented_tp_state(code, close, high, low)

        print(f"\n{code} {name}")
        print(f"  买入价: {entry}  收盘: {close}  最高: {high}  最低: {low}")
        print(f"  持仓: {shares}股  浮盈: {pnl_pct:+.1f}%  原止损: {sl}  原止盈: {tp}")
        print(f"  MA20: {tp_state['ma20']}  MA60: {tp_state['ma60']}  "
              f"MA20斜率: {tp_state['ma20_slope']:.4f}  趋势强度: {tp_state['trend_strength']:.4f}")
        print(f"  R²20: {tp_state['r2_20']:.4f}  阶梯触发: {tp_state['tier_trigger']*100:.0f}%  "
              f"部分止盈比例: {tp_state['tp_sell_pct']*100:.0f}%  趋势锁仓: {tp_state['trend_lock']}")

        # 模拟止损线调整
        pnl_ratio = (close - entry) / entry
        new_sl = sl
        if pnl_ratio > tp_state['tier_trigger']:
            tier_stop = round(tp_state['ma20'] * sim.MA20_TIER_FACTOR, 2)
            if tier_stop > new_sl:
                new_sl = tier_stop
                action = f"止损上移到MA20×0.98={tier_stop}"
            else:
                action = f"阶梯止损{tier_stop}不高于当前{new_sl}，保持"
        elif pnl_ratio > sim.BREAKEVEN_TRIGGER:
            if entry > new_sl:
                new_sl = round(entry, 2)
                action = f"止损上移到保本价{entry}"
            else:
                action = f"保本价{entry}不高于当前止损{new_sl}，保持"
        else:
            action = f"浮盈{pnl_ratio*100:.1f}%≤{sim.BREAKEVEN_TRIGGER*100:.0f}%，保持原止损"

        print(f"  止损调整: {action}")

        # 判断是否触发卖出
        if low <= new_sl and new_sl != sl:
            print(f"  ⚠️  注意：新止损{new_sl}在当日low={low}之下，但今日刚上移，次日生效")
        elif low <= sl:
            print(f"  🚨 原止损{sl}被当日low={low}触及，将触发全部卖出")
        elif pnl_ratio > sim.TP_THRESHOLD and not pos.get("partial_sold", False):
            partial_shares = int(shares * tp_state['tp_sell_pct'])
            partial_shares = (partial_shares // 100) * 100
            if partial_shares >= 100:
                remaining = shares - partial_shares
                if remaining < 100:
                    print(f"  🚨 部分止盈触发：将卖出全部{shares}股（部分{partial_shares}+零头{remaining}）")
                else:
                    print(f"  📤 部分止盈触发：卖出{partial_shares}股（{tp_state['tp_sell_pct']*100:.0f}%），余{remaining}股")
            else:
                print(f"  部分止盈：计算{partial_shares}股<100，不触发")
        else:
            print(f"  ✅ 无卖出触发（持有）")

    print("\n" + "=" * 80)
    print("烟雾测试完成。以上为分析结果，未修改任何持仓数据。")
    print("=" * 80)

if __name__ == "__main__":
    main()