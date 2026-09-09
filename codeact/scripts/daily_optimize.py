#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日候选优化入口：只生成候选，绝不覆盖主策略参数。"""
import asyncio
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


async def main():
    cmd = [str(PYTHON), "-u", str(ROOT / "scripts" / "candidate_optimize.py")]
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    loop = asyncio.get_running_loop()
    result = await loop.run_in_executor(
        None, lambda: subprocess.run(cmd, cwd=str(ROOT), env=env, timeout=1700, check=False)
    )
    if result.returncode:
        print(f"[每日候选优化] 失败，退出码={result.returncode}")
        return result.returncode
    print("[每日候选优化] 完成；主参数保持不变")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
