# -*- coding: utf-8 -*-
"""超级智能体常驻调度器：持久化状态、有限重试、依赖与防重入。"""
import datetime
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "scripts"))
from task_state import TaskState, scheduler_lock
from wechat_push import alert_task_failure
from runtime_guard import verify_if_enabled

if os.name == "nt":
    PY = ROOT / ".venv" / "Scripts" / "python.exe"
else:
    PY = ROOT / ".venv" / "bin" / "python"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
STATE = TaskState(ROOT / "data" / "task_state.sqlite3")
LOCK_PATH = ROOT / "data" / ".scheduler.lock"
verify_if_enabled(ROOT)

TASKS = [
    {"name": "daily_signal_scan", "time": "15:00", "script": "scripts/daily_signal_scan.py", "timeout": 3600, "monthly_only": False, "max_attempts": 3},
    {"name": "sim_trade_tracker", "time": "15:20", "script": "scripts/sim_trade_tracker.py", "timeout": 1800, "monthly_only": False, "max_attempts": 2, "depends_on": ["daily_signal_scan"]},
    {"name": "daily_pipeline_codeact", "time": "15:30", "script": "scripts/daily_pipeline_codeact.py", "timeout": 1200, "monthly_only": False, "max_attempts": 2, "depends_on": ["sim_trade_tracker"]},
    {"name": "daily_optimize", "time": "16:30", "script": "scripts/daily_optimize.py", "timeout": 3600, "monthly_only": False, "max_attempts": 2, "depends_on": ["daily_pipeline_codeact"]},
    {"name": "wechat_daily_summary", "time": "17:45", "script": "scripts/daily_wechat_summary.py", "timeout": 300, "monthly_only": False, "max_attempts": 2, "depends_on": []},
    {"name": "monthly_reoptimize", "time": "17:50", "script": "scripts/monthly_reoptimize.py", "timeout": 3600, "monthly_only": True, "max_attempts": 2, "depends_on": ["daily_pipeline_codeact"]},
]


def _log_path(name, date_str):
    return LOG_DIR / f"{name}_{date_str}.log"


def log_done(name, date_str, status="RUN_OK"):
    with open(_log_path(name, date_str), "a", encoding="utf-8") as f:
        f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S} {status}\n")


def _period_key(task, date_str):
    return date_str[:7] if task.get("monthly_only") else ""


def _state_key(task, date_str):
    return date_str[:7] if task.get("monthly_only") else date_str


def task_completed(name, date_str):
    task = next(t for t in TASKS if t["name"] == name)
    state_date = _state_key(task, date_str)
    period = _period_key(task, date_str)
    status = STATE.status(name, state_date, period)
    if status == "succeeded":
        return True
    # 向后兼容升级前已有的成功日志；一旦本次任务写入SQLite则以SQLite为准。
    if status == "missing":
        p = _log_path(name, date_str)
        if p.exists() and any("RUN_OK" in ln for ln in p.read_text(encoding="utf-8").splitlines()):
            return True
    return False


def dependency_ready(task, date_str):
    for dep in task.get("depends_on", []):
        if not task_completed(dep, date_str):
            dep_task = next(t for t in TASKS if t["name"] == dep)
            dep_status = STATE.status(dep, _state_key(dep_task, date_str), _period_key(dep_task, date_str))
            if dep_status == "permanent_failed":
                return False, f"依赖 {dep} 已永久失败"
            return False, f"依赖 {dep} 尚未成功"
    return True, ""


def _terminate_process(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except OSError:
            pass


def _log_tail(path, limit=1800):
    try:
        return path.read_text(encoding="utf-8", errors="replace")[-limit:]
    except OSError:
        return ""


def _alert_if_final(task, date_str, claim, reason, log_path):
    if claim["attempt"] >= claim["max_attempts"]:
        alert_task_failure(task["name"], _state_key(task, date_str), claim["attempt"],
                           claim["max_attempts"], reason, str(log_path), _log_tail(log_path))


def run_task(task, date_str):
    period = _period_key(task, date_str)
    log_path = _log_path(task["name"], date_str)
    claim = STATE.claim(task["name"], _state_key(task, date_str), period, task.get("max_attempts", 2), str(log_path))
    if not claim:
        return None
    attempt = claim["attempt"]
    print(f"[{datetime.datetime.now():%H:%M:%S}] 开始 {task['name']} attempt={attempt}/{claim['max_attempts']}")
    with open(log_path, "a", encoding="utf-8") as logf:
        logf.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} 启动 attempt={attempt} =====\n")
        logf.flush()
        cmd = [str(PY), "-u", str(ROOT / task["script"])]
        proc = None
        try:
            child_env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUTF8="1")
            kwargs = dict(cwd=str(ROOT), env=child_env, stdout=logf, stderr=subprocess.STDOUT)
            if os.name != "nt":
                kwargs["start_new_session"] = True
            proc = subprocess.Popen(cmd, **kwargs)
            try:
                returncode = proc.wait(timeout=task["timeout"])
            except subprocess.TimeoutExpired:
                _terminate_process(proc)
                log_done(task["name"], date_str, "RUN_TIMEOUT")
                STATE.finish(task["name"], _state_key(task, date_str), period, "timeout", 124, "TimeoutExpired", f"超过 {task['timeout']}s")
                _alert_if_final(task, date_str, claim, f"超时>{task['timeout']}s", log_path)
                print(f"[超时] {task['name']} 超过 {task['timeout']}s")
                return 124
            if returncode == 0:
                log_done(task["name"], date_str, "RUN_OK")
                STATE.finish(task["name"], _state_key(task, date_str), period, "succeeded", 0)
                print(f"[{datetime.datetime.now():%H:%M:%S}] 完成 {task['name']}")
            elif returncode == 3:
                log_done(task["name"], date_str, "RUN_POSTPONED")
                STATE.finish(task["name"], _state_key(task, date_str), period, "postponed", 3, "Postponed")
                print(f"[{datetime.datetime.now():%H:%M:%S}] {task['name']} 顺延")
            else:
                log_done(task["name"], date_str, f"RUN_ERROR child_exit={returncode}")
                STATE.finish(task["name"], _state_key(task, date_str), period, "failed", returncode, "ChildExit", f"退出码={returncode}")
                _alert_if_final(task, date_str, claim, f"退出码={returncode}", log_path)
                print(f"[{datetime.datetime.now():%H:%M:%S}] {task['name']} 失败，退出码={returncode}")
            return returncode
        except Exception as exc:
            if proc is not None:
                _terminate_process(proc)
            log_done(task["name"], date_str, f"RUN_ERROR {type(exc).__name__}: {exc}")
            STATE.finish(task["name"], _state_key(task, date_str), period, "failed", 1, type(exc).__name__, str(exc))
            _alert_if_final(task, date_str, claim, str(exc), log_path)
            print(f"[错误] {task['name']}: {exc}")
            return 1


def tasks_for(date_obj):
    return [t for t in TASKS if not t["monthly_only"] or date_obj.day <= 10]


def due(task, now):
    hh, mm = map(int, task["time"].split(":"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    return now >= target


def run_due(today, date_str, catchup=False):
    now = datetime.datetime.now()
    for task in tasks_for(today):
        if task_completed(task["name"], date_str):
            continue
        if not due(task, now):
            continue
        ready, reason = dependency_ready(task, date_str)
        if not ready:
            if catchup:
                print(f"[跳过] {task['name']}：{reason}")
            continue
        # 状态库中的next_retry_at负责退避；claim失败时不重复启动。
        run_task(task, date_str)


def main():
    print("=" * 60)
    print("超级智能体(龟缠量化v6) 本地调度器 已启动")
    print(f"Python: {PY}")
    print("交易日任务: 15:00扫描 15:20模拟 15:30巡检 16:30优化 17:45日报; 每月首个交易日17:50月度重优化")
    print("Ctrl+C 退出。")
    print("=" * 60)
    try:
        with scheduler_lock(LOCK_PATH):
            last_date = None
            while True:
                today = datetime.date.today()
                date_str = today.strftime("%Y-%m-%d")
                if last_date != date_str:
                    if today.weekday() < 5:
                        print(f"[{date_str}] 工作日, 检查是否需要补跑...")
                        run_due(today, date_str, catchup=True)
                    else:
                        print(f"[{date_str}] 周末, 跳过(非交易日)")
                    last_date = date_str
                if today.weekday() < 5:
                    run_due(today, date_str)
                time.sleep(20)
    except RuntimeError as exc:
        print(f"[调度器] 未启动：{exc}")
        return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("调度器已退出")
