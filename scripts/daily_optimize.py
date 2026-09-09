#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日候选优化入口：只生成候选，绝不覆盖主策略参数。"""
import asyncio
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
# 必须小于 scheduler.py 中 daily_optimize 的 timeout(3600s)，保证超时时由本包装器
# 打出可读信息并保留断点，而不是被调度器直接击杀。
INTERNAL_TIMEOUT = 3500


async def main():
    cmd = [str(PYTHON), "-u", str(ROOT / "scripts" / "candidate_optimize.py")]
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(
            None,
            lambda: subprocess.run(cmd, cwd=str(ROOT), env=env, timeout=INTERNAL_TIMEOUT, check=False),
        )
    except subprocess.TimeoutExpired:
        # 子进程已被杀，但已完成的状态保存在断点文件中，明日跨天续跑。
        print(f"[每日候选优化] 超时（{INTERNAL_TIMEOUT}s）：已完成状态已存断点，明日续跑；主参数保持不变")
        return 124
    except FileNotFoundError:
        print(f"[每日候选优化] 找不到Python解释器：{PYTHON}")
        return 1
    if result.returncode:
        print(f"[每日候选优化] 失败，退出码={result.returncode}；主参数保持不变")
        return result.returncode
    print("[每日候选优化] 完成；主参数保持不变")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
