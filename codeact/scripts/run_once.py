#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""单次任务执行器 + 开机补跑（配合 Windows 计划任务使用，替代常驻 scheduler.py）。
用法:
  python run_once.py <name> <script> [timeout]
  python run_once.py --catchup
当天 logs/<name>_<date>.log 已有 RUN_OK 则跳过；否则运行并把结果写入该日志。"""
import datetime
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = ROOT / ".venv" / "Scripts" / "python.exe"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

TASKS = [
    {"name": "daily_signal_scan", "time": "15:00", "script": "scripts/daily_signal_scan.py", "timeout": 3600, "monthly_only": False},
    {"name": "sim_trade_tracker", "time": "15:20", "script": "scripts/sim_trade_tracker.py", "timeout": 1800, "monthly_only": False},
    {"name": "daily_pipeline_codeact", "time": "15:30", "script": "scripts/daily_pipeline_codeact.py", "timeout": 1200, "monthly_only": False},
    {"name": "daily_optimize", "time": "16:30", "script": "scripts/daily_optimize.py", "timeout": 1800, "monthly_only": False},
    {"name": "monthly_reoptimize", "time": "15:30", "script": "scripts/monthly_reoptimize.py", "timeout": 3600, "monthly_only": True},
    {"name": "wechat_daily_summary", "time": "17:45", "script": "scripts/daily_wechat_summary.py", "timeout": 300, "monthly_only": False},
]


def _log_path(name, date_str):
    return LOG_DIR / f"{name}_{date_str}.log"


def task_done_today(name, date_str):
    p = _log_path(name, date_str)
    if not p.exists():
        return False
    return any("RUN_OK" in ln for ln in p.read_text(encoding="utf-8", errors="replace").splitlines())


def log_marker(name, date_str, status):
    with open(_log_path(name, date_str), "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {status}\n")


def run_task(name, script, timeout):
    date_str = datetime.date.today().strftime("%Y-%m-%d")
    if task_done_today(name, date_str):
        print(f"[run_once] {name} 今天已完成，跳过")
        return "skip"
    cmd = [str(PY), "-u", str(ROOT / script)]
    child_env = dict(os.environ)
    child_env["PYTHONPATH"] = str(ROOT)
    child_env["PYTHONUTF8"] = "1"
    logf = open(_log_path(name, date_str), "a", encoding="utf-8")
    logf.write(f"\n===== {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
    logf.flush()
    try:
        result = subprocess.run(
            cmd, cwd=str(ROOT), env=child_env, stdout=logf,
            stderr=subprocess.STDOUT, timeout=timeout, check=False,
        )
        if result.returncode == 0:
            log_marker(name, date_str, "RUN_OK")
            print(f"[run_once] {name} 完成")
            return "ok"
        log_marker(name, date_str, f"RUN_ERROR child_exit={result.returncode}")
        print(f"[run_once] {name} 子脚本失败，退出码={result.returncode}")
        return "error"
    except subprocess.TimeoutExpired:
        log_marker(name, date_str, "RUN_TIMEOUT")
        print(f"[run_once] {name} 超时(>{timeout}s)")
        return "timeout"
    except Exception as exc:
        log_marker(name, date_str, f"RUN_ERROR {type(exc).__name__}: {exc}")
        print(f"[run_once] {name} 错误: {exc}")
        return "error"
    finally:
        logf.close()


def catchup():
    today = datetime.date.today()
    date_str = today.strftime("%Y-%m-%d")
    if today.weekday() >= 5:
        print("[run_once] 周末，跳过")
        return
    order = [t for t in TASKS if not t["monthly_only"]]
    if today.day == 1:
        order.insert(4, next(t for t in TASKS if t["monthly_only"]))
    results = [run_task(t["name"], t["script"], t["timeout"]) for t in order]
    failures = [r for r in results if r in {"error", "timeout"}]
    if failures:
        print(f"[run_once] 补跑完成，但有 {len(failures)} 个任务失败或超时")
        return 1
    return 0


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    only_day = None
    if argv[0] == "--only-day":
        if len(argv) < 3:
            print(__doc__)
            return 2
        only_day = int(argv[1])
        argv = argv[2:]
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--catchup":
        return catchup()
    if len(argv) < 2:
        print(__doc__)
        return 2
    name = argv[0]
    script = argv[1]
    timeout = int(argv[2]) if len(argv) > 2 else 1800
    if only_day is not None and datetime.date.today().day != only_day:
        print(f"[run_once] 今天不是 {only_day} 号，跳过 {name}")
        return 0
    result = run_task(name, script, timeout)
    return 0 if result in {"ok", "skip"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
