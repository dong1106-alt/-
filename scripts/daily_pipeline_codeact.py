#!/usr/bin/env python3
"""
每日巡检主入口 (CodeAct)

每日定时执行策略巡检：
  a. 刷新市场状态（调用 get_state() / generate_timeline()）
  b. 评估策略表现（轻量回测 + performance_monitor）
  c. 跳过优化步骤（已由 daily_optimize.py 在 16:30 独立执行）
  d. 生成每日信号简报（调用 scan_market()）

调用 daily_pipeline.run_daily_pipeline()，该函数返回巡检报告字符串。
- result_mode=display_only: 将巡检报告直接展示给用户
- result_mode=notify:       将报告交给主 Agent 处理
- result_mode=auto:         成功时展示报告(display_only)，失败时通知(notify)

依赖模块（位于 /app/data/所有对话/主对话）:
  daily_pipeline.py  → run_daily_pipeline()

注意：不修改 daily_pipeline.py 原有逻辑，仅做 CodeAct 调度层封装。
      daily_pipeline 内部通过 __file__ 自动解析 _BASE_DIR，路径引用不受影响。
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===

import asyncio
import os
import subprocess
import sys
from datetime import datetime

# 不在定时任务中安装/升级依赖。缺少 parquet 引擎时由巡检正常报错，
# 由人工审核后在部署环境补齐，避免自动改变云端运行环境。

# ---- 导入主目录模块 ----
WORK_DIR = f"{_ROOT}"
sys.path.insert(0, WORK_DIR)
sys.path.insert(0, os.path.join(WORK_DIR, "python_libs"))

from codeact_sdk import CodeActSDK

# ---- 巡检超时上限（秒）；沙箱总超时 600s ----
PIPELINE_TIMEOUT = 540


async def main():
    result_mode = sys.argv[1] if len(sys.argv) > 1 else "display_only"
    print(f"[参数] result_mode={result_mode}")
    print(f"[时间] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

    sdk = CodeActSDK()

    # 交易日判断：读标记文件（与 daily_pipeline 内部逻辑一致，提前拦截避免无谓加载）
    _today_str = datetime.now().strftime('%Y-%m-%d')
    _flag_file = os.path.join(WORK_DIR, f"data/not_trading_day_{_today_str}.txt")
    if os.path.exists(_flag_file):
        print(f"[跳过] 今天 {_today_str} 非交易日，每日巡检跳过")
        await sdk.submit_result(
            result_mode="no_reply",
            status="success",
            message=f"[每日巡检] {_today_str} 非交易日，跳过。",
        )
        return 3

    actual_mode = result_mode if result_mode != "auto" else "display_only"

    try:
        # 导入 daily_pipeline 模块
        # 模块级 _BASE_DIR = os.path.dirname(os.path.abspath(__file__)) 会正确解析为 WORK_DIR
        # 因此 config.loader / monitor / optimizer 等路径引用不受影响
        print("[步骤] 导入 daily_pipeline 模块...")
        import daily_pipeline

        print(f"[步骤] 开始执行 run_daily_pipeline()（超时上限 {PIPELINE_TIMEOUT}s）...")
        print("-" * 60)

        loop = asyncio.get_running_loop()
        try:
            report = await asyncio.wait_for(
                loop.run_in_executor(None, daily_pipeline.run_daily_pipeline),
                timeout=PIPELINE_TIMEOUT,
            )
        except asyncio.TimeoutError:
            print(f"[超时] run_daily_pipeline() 在 {PIPELINE_TIMEOUT}s 内未完成")
            await sdk.submit_result(
                result_mode="notify",
                status="error",
                message=(
                    f"[主人](at://owner) 每日巡检超时（{PIPELINE_TIMEOUT}s）。\n"
                    "可能是数据量过大或网络请求过多。\n"
                    "信号扫描和参数优化仍独立运行。"
                ),
            )
            return 1
        if report is None:
            report = "每日巡检完成，无返回报告"

        print("-" * 60)
        print(f"[完成] {report}")

        # 保留原有巡检任务，同时在同一计划任务内启动独立 v6 影子链路，
        # 避免新增 Windows 计划任务；影子结果全部写入 data/shadow/。
        shadow_failed = False
        if os.environ.get("DISABLE_SHADOW_PIPELINE", "0") != "1":
            shadow_python = _ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
            shadow_cmd = [str(shadow_python), "-u",
                          str(_ROOT / "scripts" / "shadow_pipeline.py")]
            try:
                shadow_result = subprocess.run(
                    shadow_cmd, cwd=str(_ROOT), env=dict(os.environ),
                    timeout=660, check=False,
                )
                print(f"[影子链路] 返回码={shadow_result.returncode}")
                shadow_failed = shadow_result.returncode != 0
            except subprocess.TimeoutExpired:
                print("[影子链路] 超时；详见 data/shadow/logs/")
                shadow_failed = True
            except Exception as shadow_exc:
                print(f"[影子链路] 启动失败：{shadow_exc}")
                shadow_failed = True

        if shadow_failed:
            await sdk.submit_result(
                result_mode="notify", status="error",
                message="[主人](at://owner) 影子链路失败，详见 data/shadow/logs/。",
            )
            return 1
        await sdk.submit_result(
            result_mode=actual_mode,
            status="success",
            message=report,
        )

    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"[错误] {error_detail}")
        await sdk.submit_result(
            result_mode="notify",
            status="error",
            message=f"[主人](at://owner) 每日巡检失败：{e}\n信号扫描和参数优化仍独立运行。",
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
