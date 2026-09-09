#!/usr/bin/env python3
"""
回测回归测试脚本 - 第三道安全保障
固定10只股票+固定时间段，每次代码更新后跑一遍，对比结果变化。
收益变化>30%触发警报（可能引入了未来函数或过拟合）。

用法：python regression_test.py
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===

import sys
import os
import json
import time
import importlib.util

WORK_DIR = f"{_ROOT}"
STRATEGY_FILE = os.path.join(WORK_DIR, "龟缠安泰v5.3.4.1.py")
BASELINE_FILE = os.path.join(WORK_DIR, "data", "regression_baseline.json")
RESULT_FILE = os.path.join(WORK_DIR, "data", "regression_latest.json")

# 固定测试集：10只股票，覆盖不同行业和市值
TEST_STOCKS = [
    "sh600519",  # 贵州茅台 - 白酒龙头
    "sh601318",  # 中国平安 - 金融
    "sz000858",  # 五粮液 - 消费
    "sz000651",  # 格力电器 - 家电
    "sh600036",  # 招商银行 - 银行
    "sz002475",  # 立讯精密 - 电子
    "sh600276",  # 恒瑞医药 - 医药
    "sz300750",  # 宁德时代 - 新能源
    "sh601899",  # 紫金矿业 - 矿业
    "sz002484",  # 江海股份 - 电子元件
]

TEST_START = "2023-01-01"
TEST_END = "2026-08-06"
INITIAL_CAPITAL = 500000
THRESHOLD_PCT = 30  # 收益变化超过30%触发警报


def load_strategy():
    """加载策略文件"""
    spec = importlib.util.spec_from_file_location("v5341", STRATEGY_FILE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_regression(strategy_mod):
    """运行回归测试"""
    results = {}
    for code in TEST_STOCKS:
        print(f"  回测 {code}...", end=" ")
        try:
            # 捕获输出
            old_stdout = sys.stdout
            sys.stdout = type('Dummy', (), {'write': lambda self, x: None, 'flush': lambda self: None})()
            
            import io
            buf = io.StringIO()
            sys.stdout = buf
            
            strategy_mod.run_backtest(
                code=code,
                start_date=TEST_START,
                end_date=TEST_END,
                initial_capital=INITIAL_CAPITAL,
                adaptive="auto"
            )
            
            sys.stdout = old_stdout
            output = buf.getvalue()
            
            # 解析结果
            total_return = 0.0
            max_drawdown = 0.0
            win_rate = 0.0
            trades = 0
            
            for line in output.split('\n'):
                if '总收益' in line and '%' in line:
                    import re
                    m = re.search(r'([+-]?\d+\.?\d*)%', line)
                    if m:
                        total_return = float(m.group(1))
                elif '最大回撤' in line and '%' in line:
                    import re
                    m = re.search(r'(-?\d+\.?\d*)%', line)
                    if m:
                        max_drawdown = float(m.group(1))
                elif '胜率' in line and '%' in line:
                    import re
                    m = re.search(r'(\d+\.?\d*)%', line)
                    if m:
                        win_rate = float(m.group(1))
            
            results[code] = {
                'total_return_pct': round(total_return, 2),
                'max_drawdown_pct': round(max_drawdown, 2),
                'win_rate_pct': round(win_rate, 2),
            }
            print(f"收益={total_return:.2f}% 回撤={max_drawdown:.2f}%")
            
        except Exception as e:
            sys.stdout = old_stdout if 'old_stdout' in dir() else sys.__stdout__
            print(f"失败: {e}")
            results[code] = {
                'total_return_pct': 0.0,
                'max_drawdown_pct': 0.0,
                'win_rate_pct': 0.0,
                'error': str(e)
            }
    
    return results


def compare_with_baseline(current):
    """与基准对比"""
    if not os.path.exists(BASELINE_FILE):
        print("\n⚠️ 基准文件不存在，本次结果将保存为新基准。")
        with open(BASELINE_FILE, 'w', encoding='utf-8') as f:
            json.dump(current, f, ensure_ascii=False, indent=2)
        print(f"基准已保存: {BASELINE_FILE}")
        return True, []
    
    with open(BASELINE_FILE, 'r', encoding='utf-8') as f:
        baseline = json.load(f)
    
    alerts = []
    all_ok = True
    
    print("\n📊 回归测试对比:")
    print(f"{'股票':<12} {'基准收益':>10} {'当前收益':>10} {'变化':>10} {'状态':>6}")
    print("-" * 55)
    
    for code in TEST_STOCKS:
        base_ret = baseline.get(code, {}).get('total_return_pct', 0)
        curr_ret = current.get(code, {}).get('total_return_pct', 0)
        
        if base_ret == 0:
            change_pct = 100 if curr_ret != 0 else 0
        else:
            change_pct = abs((curr_ret - base_ret) / abs(base_ret) * 100) if base_ret != 0 else 100
        
        if change_pct > THRESHOLD_PCT:
            status = "🚫警报"
            alerts.append({
                'code': code,
                'baseline': base_ret,
                'current': curr_ret,
                'change_pct': round(change_pct, 1),
                'reason': f"收益变化{change_pct:.1f}%超过{THRESHOLD_PCT}%阈值"
            })
            all_ok = False
        else:
            status = "✅"
        
        print(f"{code:<12} {base_ret:>9.2f}% {curr_ret:>9.2f}% {change_pct:>9.1f}% {status:>6}")
    
    return all_ok, alerts


def main():
    print("=" * 60)
    print("🔧 回测回归测试 - 第三道安全保障")
    print(f"   策略文件: 龟缠安泰v5.3.4.1.py")
    print(f"   测试股票: {len(TEST_STOCKS)}只")
    print(f"   回测区间: {TEST_START} ~ {TEST_END}")
    print(f"   本金: {INITIAL_CAPITAL}")
    print(f"   警报阈值: 收益变化>{THRESHOLD_PCT}%")
    print("=" * 60)
    print()
    
    # 加载策略
    print("📦 加载策略文件...")
    try:
        mod = load_strategy()
        print("✅ 策略加载成功")
    except Exception as e:
        print(f"❌ 策略加载失败: {e}")
        sys.exit(1)
    
    # 运行回归测试
    print("\n🔄 开始回测...")
    results = run_regression(mod)
    
    # 保存结果
    with open(RESULT_FILE, 'w', encoding='utf-8') as f:
        json.dump({
            'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
            'results': results
        }, f, ensure_ascii=False, indent=2)
    
    # 与基准对比
    all_ok, alerts = compare_with_baseline(results)
    
    print()
    if all_ok:
        print("✅ 回归测试通过，代码安全。")
        sys.exit(0)
    else:
        print(f"🚫 回归测试未通过！{len(alerts)}只股票收益变化超过阈值。")
        print("   可能原因：引入了未来函数、过度拟合、或策略逻辑变更。")
        print("   请人工审查代码变更后再决定是否更新。")
        for a in alerts:
            print(f"   - {a['code']}: 基准{a['baseline']}% → 当前{a['current']}% (变化{a['change_pct']}%)")
        sys.exit(1)


if __name__ == '__main__':
    main()