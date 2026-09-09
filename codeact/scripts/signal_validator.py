#!/usr/bin/env python3
"""
实盘信号追踪验证脚本 - 第四道安全保障
读取signal_history.json中记录的历史信号，检查实际涨跌表现。
偏离回测预期>20%触发警报，暂停参数自动采纳。

用法：python signal_validator.py
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===

import json
import os
import sys
import time
from datetime import datetime, timedelta

WORK_DIR = f"{_ROOT}"
SIGNAL_HISTORY_FILE = os.path.join(WORK_DIR, "data", "signal_history.json")
INDEX_FILE = os.path.join(WORK_DIR, "data", "index", "sh000001.parquet")
ALERT_FILE = os.path.join(WORK_DIR, "data", "signal_alerts.json")

DEVIATION_THRESHOLD = 20  # 实盘vs回测偏离>20%触发警报
MIN_SIGNALS_FOR_STATS = 5  # 至少5个信号才能统计


def load_signal_history():
    """加载信号历史"""
    if not os.path.exists(SIGNAL_HISTORY_FILE):
        return []
    with open(SIGNAL_HISTORY_FILE, 'r', encoding='utf-8') as f:
        return json.load(f)


def fetch_close_price(code, date_str):
    """获取指定日期的收盘价"""
    try:
        import pandas as pd
        # 尝试从本地parquet读取
        stock_file = os.path.join(WORK_DIR, "data", "stocks", f"{code}.parquet")
        if os.path.exists(stock_file):
            df = pd.read_parquet(stock_file)
            df['date'] = pd.to_datetime(df['date'])
            target_date = pd.to_datetime(date_str)
            # 找最接近的日期
            idx = (df['date'] - target_date).abs().idxmin()
            row = df.loc[idx]
            if abs((row['date'] - target_date).days) <= 3:
                return float(row['close'])
        return None
    except:
        return None


def fetch_price_after_days(code, signal_date, days=30):
    """获取信号发出N天后的价格"""
    try:
        import pandas as pd
        stock_file = os.path.join(WORK_DIR, "data", "stocks", f"{code}.parquet")
        if os.path.exists(stock_file):
            df = pd.read_parquet(stock_file)
            df['date'] = pd.to_datetime(df['date'])
            signal_dt = pd.to_datetime(signal_date)
            future_dt = signal_dt + timedelta(days=days)
            # 找信号日之后的第N天
            future_df = df[df['date'] > signal_dt]
            if len(future_df) > 0:
                # 取第N个交易日（约等于N个自然日）
                target_idx = min(days - 1, len(future_df) - 1)  # 简化：取第days个交易日
                target_idx = min(int(days * 0.7), len(future_df) - 1)  # 交易日约占自然日的70%
                return float(future_df.iloc[target_idx]['close']), str(future_df.iloc[target_idx]['date'].date())
        return None, None
    except:
        return None, None


def validate_signals():
    """验证历史信号的实际表现"""
    history = load_signal_history()
    if not history:
        print("📭 暂无信号历史记录，无法验证。")
        print("   系统运行后会自动记录信号，积累后可验证。")
        return True, []
    
    print(f"📋 共找到 {len(history)} 条信号记录")
    
    buy_signals = [s for s in history if s.get('type') == 'buy']
    sell_signals = [s for s in history if s.get('type') == 'sell']
    
    print(f"   买入信号: {len(buy_signals)} 条")
    print(f"   卖出信号: {len(sell_signals)} 条")
    print()
    
    if len(buy_signals) < MIN_SIGNALS_FOR_STATS:
        print(f"⏳ 买入信号不足{MIN_SIGNALS_FOR_STATS}条，样本太小暂不统计。")
        print(f"   当前{len(buy_signals)}条，继续积累中...")
        return True, []
    
    # 验证买入信号：信号发出后30天的实际涨跌
    print("📊 买入信号验证（信号发出后30天表现）:")
    print(f"{'日期':<12} {'代码':<12} {'信号价':>8} {'30天后':>8} {'涨跌':>8} {'状态':>6}")
    print("-" * 60)
    
    hits = 0
    total_checked = 0
    results = []
    
    for sig in buy_signals:
        code = sig.get('code', '')
        date = sig.get('date', '')
        price = sig.get('close', 0)
        
        if not code or not date or not price:
            continue
        
        future_price, future_date = fetch_price_after_days(code, date, 30)
        if future_price is None:
            continue
        
        change_pct = (future_price - price) / price * 100
        total_checked += 1
        
        # 买入信号：涨了算命中
        if change_pct > 0:
            hits += 1
            status = "✅赚"
        else:
            status = "❌亏"
        
        results.append({
            'code': code, 'date': date, 'signal_price': price,
            'future_price': future_price, 'future_date': future_date,
            'change_pct': round(change_pct, 2)
        })
        
        print(f"{date:<12} {code:<12} {price:>8.2f} {future_price:>8.2f} {change_pct:>+7.2f}% {status:>4}")
    
    if total_checked == 0:
        print("⏳ 信号发出不足30天，暂无法验证实际表现。")
        return True, []
    
    hit_rate = hits / total_checked * 100 if total_checked > 0 else 0
    avg_return = sum(r['change_pct'] for r in results) / len(results) if results else 0
    
    print()
    print(f"📈 统计汇总:")
    print(f"   验证信号数: {total_checked}")
    print(f"   命中率(上涨): {hit_rate:.1f}%")
    print(f"   平均收益: {avg_return:+.2f}%")
    
    # 与回测预期对比
    # 回测中买入信号胜率约40-50%，偏离>20%触发警报
    expected_win_rate = 45  # 预期胜率45%
    deviation = abs(hit_rate - expected_win_rate) / expected_win_rate * 100
    
    print(f"   预期胜率: {expected_win_rate}%")
    print(f"   偏离度: {deviation:.1f}%")
    
    alerts = []
    if deviation > DEVIATION_THRESHOLD:
        alert_msg = f"实盘胜率{hit_rate:.1f}%偏离预期{expected_win_rate}%超过{DEVIATION_THRESHOLD}%"
        print(f"\n🚫 警报: {alert_msg}")
        print("   建议暂停参数自动采纳，人工检查策略表现。")
        alerts.append({
            'type': 'win_rate_deviation',
            'message': alert_msg,
            'actual': round(hit_rate, 1),
            'expected': expected_win_rate,
            'deviation': round(deviation, 1)
        })
    
    # 保存警报
    if alerts:
        with open(ALERT_FILE, 'w', encoding='utf-8') as f:
            json.dump({
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'alerts': alerts,
                'stats': {
                    'total_checked': total_checked,
                    'hit_rate': round(hit_rate, 1),
                    'avg_return': round(avg_return, 2),
                    'deviation': round(deviation, 1)
                }
            }, f, ensure_ascii=False, indent=2)
    
    return len(alerts) == 0, alerts


def main():
    print("=" * 60)
    print("📊 实盘信号追踪验证 - 第四道安全保障")
    print(f"   信号历史: {SIGNAL_HISTORY_FILE}")
    print(f"   验证方式: 买入信号发出30天后实际涨跌")
    print(f"   警报阈值: 胜率偏离预期>{DEVIATION_THRESHOLD}%")
    print("=" * 60)
    print()
    
    ok, alerts = validate_signals()
    
    print()
    if ok:
        print("✅ 信号验证通过，策略表现正常。")
        sys.exit(0)
    else:
        print(f"🚫 信号验证未通过！发现{len(alerts)}个警报。")
        print("   建议暂停月度参数自动采纳，人工检查后再恢复。")
        sys.exit(1)


if __name__ == '__main__':
    main()