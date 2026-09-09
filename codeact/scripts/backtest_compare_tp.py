#!/usr/bin/env python3
"""
回测对比：固定止盈全部卖出 vs 分段止盈
在v6_optimized主策略框架下，仅替换卖出止盈逻辑，比较Sharpe/收益/回撤/胜率。
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===
import sys, os, json, math, copy
sys.path.insert(0, f"{_ROOT}")

# 直接复用主策略
import importlib.util
spec = importlib.util.spec_from_file_location(
    "v6", f"{_ROOT}/龟缠量化v6_optimized.py"
)
v6 = importlib.util.module_from_spec(spec)

# 捕获参数加载
import yaml
import numpy as np
import pandas as pd
from datetime import datetime

CFG_PATH = f"{_ROOT}/config/settings.yaml"
STRATEGY_PATH = f"{_ROOT}/龟缠量化v6_optimized.py"

TEST_STOCKS = [
    "sh600519", "sh601318", "sz000858", "sz000651", "sh600036",
    "sz002475",
]
TEST_START = "2023-06-01"
TEST_END = "2026-08-15"
INIT_CAP = 500000


def load_cfg():
    with open(CFG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def run_one_mode(mode):
    """
    mode='fixed': 用主策略原生逻辑（其实原生就有分段止盈）
    mode='segmented': 原生分段止盈（主策略默认）

    由于主策略本身已经是分段止盈，我们通过monkey-patch的方式：
    - fixed: 强制 take_profit_pct=1.0 (全部卖出) 并屏蔽阶梯/保本上移
    - segmented: 原生逻辑
    """
    # 每次重新加载模块，避免状态污染
    spec = importlib.util.spec_from_file_location(f"v6_{mode}", STRATEGY_PATH)
    mod = importlib.util.module_from_spec(spec)

    # 拦截exec以patch
    if mode == "fixed":
        # 读取源码并替换关键逻辑
        with open(STRATEGY_PATH, "r", encoding="utf-8") as f:
            src = f.read()
        # 替换1: 阶梯止盈触发线设为999（永不触发）
        src = src.replace(
            "if _sell_ts > 0.15:\n                _tier_trigger = 0.20",
            "if _sell_ts > 0.15:\n                _tier_trigger = 9.99"
        )
        src = src.replace(
            "elif _sell_ts < -0.05:\n                _tier_trigger = 0.10",
            "elif _sell_ts < -0.05:\n                _tier_trigger = 9.99"
        )
        src = src.replace(
            "else:\n                _tier_trigger = 0.15",
            "else:\n                _tier_trigger = 9.99"
        )
        # 替换2: 保本止损阈值设为999（永不上移）
        src = src.replace(
            "elif pnl > 0.05:\n                # 浮盈5%-触发线：止损上移到成本价（保本）",
            "elif False and pnl > 0.05:\n                # 浮盈5%-触发线：止损上移到成本价（保本）"
        )
        # 替换3: 部分止盈比例改为1.0（全部卖出）
        src = src.replace(
            "if pnl_pct > dp['take_profit_threshold']:\n                    sell_shares = int(pos.current_shares * dp['take_profit_pct'])",
            "if pnl_pct > dp['take_profit_threshold']:\n                    sell_shares = pos.current_shares  # FIXED: 全部止盈"
        )
        # 编译并执行
        code_obj = compile(src, STRATEGY_PATH, "exec")
        exec(code_obj, mod.__dict__)
    else:
        spec.loader.exec_module(mod)

    raw_cfg = load_cfg()
    # 构建模块期望的扁平cfg结构（与DEFAULT_CONFIG一致）
    _p = raw_cfg['paths']
    cfg = {
        "data_source": raw_cfg.get('data_source', 'coze'),
        "coze_skill_path": _p['skill_path_dp'],
        "cache_dir": _p['cache_dir'],
        "output_dir": _p['output_dir'],
        "strategy": raw_cfg['strategy'],
        "trade_cost": raw_cfg['trade_cost'],
        "filter": raw_cfg['filter'],
        "risk": raw_cfg['risk'],
        "plot_enable": False,
        "market_trend": raw_cfg.get('market_trend', {}),
    }
    # 多股回测
    trades, eq_df, final_cap, bt_df_dict, max_dd, max_dd_date = mod.run_multi_backtest(
        TEST_STOCKS, cfg, TEST_START, TEST_END
    )

    # 计算Sharpe（日收益率）
    if eq_df is not None and len(eq_df) > 2:
        eq = eq_df["equity"] if "equity" in eq_df.columns else eq_df.iloc[:, -1]
        daily_ret = eq.pct_change().dropna()
        if len(daily_ret) > 1 and daily_ret.std() > 0:
            sharpe = (daily_ret.mean() / daily_ret.std()) * math.sqrt(252)
        else:
            sharpe = 0.0
    else:
        sharpe = 0.0

    win = [t for t in trades if t.pnl > 0]
    loss = [t for t in trades if t.pnl <= 0]
    total_ret = (final_cap - INIT_CAP) / INIT_CAP * 100

    return {
        "mode": mode,
        "final_cap": final_cap,
        "total_ret_pct": total_ret,
        "max_dd_pct": max_dd * 100 if max_dd else 0,
        "sharpe": sharpe,
        "trades": len(trades),
        "win_rate": len(win) / len(trades) * 100 if trades else 0,
        "wins": len(win),
        "losses": len(loss),
        "avg_win_pct": np.mean([t.pnl_pct for t in win]) if win else 0,
        "avg_loss_pct": np.mean([t.pnl_pct for t in loss]) if loss else 0,
    }


def main():
    print("=" * 70)
    print("分段止盈 vs 固定止盈 回测对比")
    print(f"股票: {len(TEST_STOCKS)}只  区间: {TEST_START}~{TEST_END}")
    print("=" * 70)

    results = {}
    for mode in ["fixed", "segmented"]:
        print(f"\n>>> 运行模式: {mode} ...")
        try:
            r = run_one_mode(mode)
            results[mode] = r
            print(f"    完成: 收益{r['total_ret_pct']:+.2f}% 回撤{r['max_dd_pct']:.2f}% "
                  f"Sharpe{r['sharpe']:.3f} 交易{r['trades']}笔 胜率{r['win_rate']:.1f}%")
        except Exception as e:
            import traceback
            print(f"    ❌ 失败: {e}")
            traceback.print_exc()
            results[mode] = None

    print("\n" + "=" * 70)
    print("📊 对比结果")
    print("=" * 70)
    if results.get("fixed") and results.get("segmented"):
        f = results["fixed"]
        s = results["segmented"]
        print(f"{'指标':<20} {'固定止盈(全部卖出)':>20} {'分段止盈':>20} {'变化':>15}")
        print("-" * 80)
        rows = [
            ("总收益%", f["total_ret_pct"], s["total_ret_pct"], "%.2f"),
            ("最大回撤%", f["max_dd_pct"], s["max_dd_pct"], "%.2f"),
            ("Sharpe", f["sharpe"], s["sharpe"], "%.3f"),
            ("交易笔数", f["trades"], s["trades"], "%d"),
            ("胜率%", f["win_rate"], s["win_rate"], "%.1f"),
            ("平均盈利%", f["avg_win_pct"], s["avg_win_pct"], "%.2f"),
            ("平均亏损%", f["avg_loss_pct"], s["avg_loss_pct"], "%.2f"),
        ]
        for name, fv, sv, fmt in rows:
            delta = sv - fv
            sign = "+" if delta >= 0 else ""
            print(f"{name:<20} {fmt % fv:>20} {fmt % sv:>20} {sign}{fmt % delta:>14}")

        sharpe_change = (s["sharpe"] - f["sharpe"]) / abs(f["sharpe"]) * 100 if f["sharpe"] != 0 else 0
        print("\n" + "-" * 80)
        print(f"Sharpe变化: {sharpe_change:+.2f}%  (阈值: 下降不超过5%)")
        if sharpe_change >= -5:
            print("✅ 通过：分段止盈Sharpe下降未超过5%，可采纳")
        else:
            print("❌ 不通过：Sharpe下降超过5%，应回滚")

    # 保存结果
    out_path = f"{_ROOT}/data/tp_comparison_result.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n结果已保存: {out_path}")


if __name__ == "__main__":
    main()