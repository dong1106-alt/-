#!/usr/bin/env python3
"""
未来函数自动检测脚本 - 第一道安全保障
扫描目标代码文件，检测常见的未来函数模式：
1. 预评分/质量评分使用回测期间数据（非回测前数据）
2. ATR/波动率计算使用回测期间数据
3. .shift(-n) 前视模式
4. 阈值用全量数据计算（如 max/min/quantile on full df）
5. 回测函数内直接引用回测结束日之后的数据

用法：python ff_detector.py <目标文件.py>
"""

import re
import sys
import os

def detect_future_functions(filepath):
    """检测文件中的未来函数模式"""
    issues = []
    
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        
        # 跳过注释和空行
        if stripped.startswith('#') or not stripped:
            continue
        
        # 检测1: .shift(-n) 前视模式
        if re.search(r'\.shift\s*\(\s*-\s*\d+', line):
            issues.append(('CRITICAL', i, f'前视shift: {stripped}'))
        
        # 检测2: 预评分用全量数据（calc_stock_quality_score(df, 而非 pre_filter_df）
        if 'calc_stock_quality_score' in line and 'pre_filter' not in line and 'def ' not in line:
            if '(' in line and 'df' in line and 'pre_filter' not in line:
                issues.append(('WARNING', i, f'预评分可能用全量数据: {stripped}'))
        
        # 检测3: ATR%用回测期间数据（valid_atr = bt_df 而非 pre_atr_df）
        if 'valid_atr' in line and 'bt_df' in line and 'pre_atr' not in line and '=' in line:
            issues.append(('WARNING', i, f'ATR%可能用回测数据: {stripped}'))
        
        # 检测4: 在回测函数内用 .max() / .min() / .quantile() 对全量df
        # 仅检测回测相关函数内的全量统计
        if re.search(r'df\[.*\]\.(max|min|quantile|mean|std)\(\)', line) and 'rolling' not in line and 'shift' not in line:
            # 排除预评分和正常指标计算
            if 'pre_filter' not in line and 'bt_start' not in line:
                context_before = ''.join(lines[max(0,i-5):i])
                if '回测' in context_before or 'backtest' in context_before.lower() or 'bt_df' in context_before:
                    issues.append(('INFO', i, f'回测区内全量统计: {stripped}'))
        
        # 检测5: 直接引用回测结束日之后的数据
        if 'bt_end' in line and ('>' in line or '<' in line) and 'date' in line:
            if 'filter' in line.lower() or 'mask' in line.lower():
                pass  # 正常的日期过滤
            elif re.search(r'df\[.*bt_end.*\]', line):
                issues.append(('WARNING', i, f'可能引用回测后数据: {stripped}'))
    
    return issues

def main():
    if len(sys.argv) < 2:
        print("用法: python ff_detector.py <目标文件.py>")
        sys.exit(1)
    
    filepath = sys.argv[1]
    if not os.path.exists(filepath):
        print(f"❌ 文件不存在: {filepath}")
        sys.exit(1)
    
    print(f"🔍 未来函数检测: {filepath}")
    print(f"   文件大小: {os.path.getsize(filepath)} bytes")
    print()
    
    issues = detect_future_functions(filepath)
    
    if not issues:
        print("✅ 未检测到未来函数模式，代码安全。")
        sys.exit(0)
    
    # 按严重程度分组
    critical = [i for i in issues if i[0] == 'CRITICAL']
    warnings = [i for i in issues if i[0] == 'WARNING']
    infos = [i for i in issues if i[0] == 'INFO']
    
    if critical:
        print(f"🚫 CRITICAL ({len(critical)}): 必须修复")
        for _, line_no, desc in critical:
            print(f"   Line {line_no}: {desc}")
        print()
    
    if warnings:
        print(f"⚠️ WARNING ({len(warnings)}): 需要人工确认")
        for _, line_no, desc in warnings:
            print(f"   Line {line_no}: {desc}")
        print()
    
    if infos:
        print(f"ℹ️ INFO ({len(infos)}): 仅供参考")
        for _, line_no, desc in infos:
            print(f"   Line {line_no}: {desc}")
        print()
    
    if critical:
        print("🚫 检测到严重未来函数，拒绝更新！")
        sys.exit(1)
    elif warnings:
        print("⚠️ 有疑似未来函数，请人工确认后再更新。")
        sys.exit(2)
    else:
        print("✅ 仅有信息级提示，代码安全。")
        sys.exit(0)

if __name__ == '__main__':
    main()
