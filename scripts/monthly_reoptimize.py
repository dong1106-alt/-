#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""月度候选优化入口：增加样本和轮数，但不覆盖主策略。"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def main() -> int:
    cmd = [str(PYTHON), "-u", str(ROOT / "scripts" / "candidate_optimize.py"), "--monthly"]
    env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1")
    try:
        result = subprocess.run(cmd, cwd=str(ROOT), env=env, timeout=3500, check=False)
    except subprocess.TimeoutExpired:
        # 子进程已被杀，已完成的市场状态保存在月度断点文件中，顺延日继续
        print("[月度候选优化] 超时（3500s）：已完成状态已存断点，下次继续；主参数保持不变")
        return 124
    print("[月度候选优化] 主参数保持不变")
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
