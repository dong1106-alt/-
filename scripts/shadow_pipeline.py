#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v6 影子组合全链路入口。

影子链路独立写入 data/shadow/，默认不修改主模拟盘：
扫描 -> 影子模拟交易 -> 影子巡检 -> 候选参数评估。
"""
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_project_python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
PYTHON = _project_python if _project_python.exists() else Path(sys.executable)
SHADOW_BASE = ROOT / "data" / "shadow"
SHADOW = SHADOW_BASE
MAIN_PORTFOLIO = ROOT / "data" / "sim_trades" / "portfolio.json"
MAIN_POOL = ROOT / "data" / "all_main_board_codes.txt"


def _date():
    return dt.date.today().isoformat()


def _select_shadow_run():
    """候选参数通过静态预筛后才获得独立影子目录。"""
    active = ROOT / "data" / "candidates" / "active_shadow.json"
    if not active.exists():
        return SHADOW_BASE, None, "v6-risk-baseline"
    try:
        payload = json.loads(active.read_text(encoding="utf-8"))
        candidate_id = str(payload.get("candidate_meta", {}).get("candidate_id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]+", candidate_id):
            raise ValueError("候选标识非法")
        return SHADOW_BASE / "runs" / candidate_id, active, candidate_id
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[shadow] 候选配置不可用，退回v6风险影子：{exc}")
        return SHADOW_BASE, None, "v6-risk-baseline"


def _prepare(candidate_id):
    for name in ("signals", "sim_trades", "reports", "logs", "evaluation"):
        (SHADOW / name).mkdir(parents=True, exist_ok=True)
    target = SHADOW / "sim_trades" / "portfolio.json"
    meta = SHADOW / "shadow_meta.json"
    if not target.exists():
        if not MAIN_PORTFOLIO.exists():
            raise FileNotFoundError(f"主模拟盘不存在：{MAIN_PORTFOLIO}")
        initial_hash = hashlib.sha256(MAIN_PORTFOLIO.read_bytes()).hexdigest()
        shutil.copy2(MAIN_PORTFOLIO, target)
        portfolio = json.loads(target.read_text(encoding="utf-8"))
        meta.write_text(json.dumps({
            "created_at": dt.datetime.now().isoformat(timespec="seconds"),
            "baseline_trade_count": len(portfolio.get("trade_history", [])),
            "baseline_position_count": len(portfolio.get("positions", [])),
            "candidate_id": candidate_id,
            "baseline_main_total_value": portfolio.get("total_value", 0),
            "initial_portfolio_hash": initial_hash,
            "note": "从主模拟盘复制的只读起点；之后与主盘独立运行",
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[shadow] 已创建独立起点：{target}")
    elif not meta.exists():
        raise RuntimeError("existing shadow account has no verifiable starting metadata")


def _run_step(name, script, env_extra, timeout):
    ds = _date()
    logfile = SHADOW / "logs" / f"{name}_{ds}.log"
    env = dict(os.environ)
    env.update({
        "PYTHONPATH": str(ROOT), "PYTHONUTF8": "1",
        "CODEACT_REPORTS_DIR": str(SHADOW / "reports"),
        "TRADING_DAY_FLAG_DIR": str(SHADOW),
        "SIM_TRADING_DAY_FLAG_DIR": str(SHADOW),
        "SHADOW_ROOT": str(SHADOW),
        **env_extra,
    })
    cmd = [str(PYTHON), "-u", str(ROOT / script)]
    print(f"[shadow] 开始 {name}")
    with logfile.open("a", encoding="utf-8") as fh:
        fh.write(f"\n===== {dt.datetime.now():%Y-%m-%d %H:%M:%S} 启动 =====\n")
        try:
            result = subprocess.run(
                cmd, cwd=str(ROOT), env=env, stdout=fh,
                stderr=subprocess.STDOUT, timeout=timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            fh.write(f"RUN_TIMEOUT timeout={timeout}\n")
            print(f"[shadow] {name} 超时")
            return "timeout"
        except Exception as exc:
            fh.write(f"RUN_ERROR {type(exc).__name__}: {exc}\n")
            print(f"[shadow] {name} 启动失败：{exc}")
            return "error"
        if result.returncode == 0:
            fh.write("RUN_OK\n")
            print(f"[shadow] {name} 完成")
            return "ok"
        fh.write(f"RUN_ERROR child_exit={result.returncode}\n")
        print(f"[shadow] {name} 失败，退出码={result.returncode}")
        return "error"


def _scan_and_trade(candidate_params, prefix, stock_pool=None):
    signal_dir = SHADOW / "signals"
    sim_dir = SHADOW / "sim_trades"
    statuses = {}
    statuses["scan"] = _run_step(
        f"{prefix}_scan", "scripts/daily_signal_scan.py",
        {
            "SIGNAL_OUTPUT_DIR": str(signal_dir),
            "COMPANY_INVEST_GATE": "1",
            **({"SHADOW_STOCK_POOL_FILE": str(stock_pool)} if stock_pool else {}),
            **({"OPTIMAL_PARAMS_FILE": str(candidate_params)} if candidate_params else {}),
        }, 3600,
    )
    shadow_signal = signal_dir / "latest_signals.json"
    main_signal = ROOT / "reports" / "latest_signals.json"
    if not candidate_params and not shadow_signal.exists() and main_signal.exists():
        try:
            payload = json.loads(main_signal.read_text(encoding="utf-8"))
            if payload.get("scan_date") == _date():
                shutil.copy2(main_signal, shadow_signal)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if statuses["scan"] == "ok" and shadow_signal.exists():
        statuses["sim_trade"] = _run_step(
            f"{prefix}_sim_trade", "scripts/sim_trade_tracker.py",
            {
                "SIM_SIGNALS_FILE": str(shadow_signal),
                "SIM_PORTFOLIO_DIR": str(sim_dir),
                "SIM_SINGLE_STOCK_MAX_PCT": "25",
                "SIM_MAX_STOP_PCT": "0.12",
                "SIM_SHADOW_MODE": "1",
                "SIM_MAX_HOLD_DAYS": "20",
            }, 1800,
        )
    else:
        statuses["sim_trade"] = "skipped"
    return statuses


def main():
    global SHADOW
    if dt.date.today().weekday() >= 5:
        print(f"[shadow] {_date()} 周末，跳过影子链路")
        return 0
    SHADOW, candidate_params, candidate_id = _select_shadow_run()
    _prepare(candidate_id)
    candidate_shadow = SHADOW
    paired_root = None
    stock_pool = None
    if candidate_params:
        stock_pool = candidate_shadow / "stock_pool.txt"
        if not stock_pool.exists():
            if not MAIN_POOL.exists():
                raise FileNotFoundError(f"stock pool missing: {MAIN_POOL}")
            shutil.copy2(MAIN_POOL, stock_pool)
        paired_root = SHADOW_BASE / "runs" / f"{candidate_id}__baseline"
        SHADOW = paired_root
        _prepare(f"{candidate_id}__baseline")
        candidate_meta = json.loads((candidate_shadow / "shadow_meta.json").read_text(encoding="utf-8"))
        baseline_meta = json.loads((paired_root / "shadow_meta.json").read_text(encoding="utf-8"))
        if (not candidate_meta.get("initial_portfolio_hash")
                or candidate_meta["initial_portfolio_hash"] != baseline_meta.get("initial_portfolio_hash")):
            raise RuntimeError("paired shadow accounts have different starting portfolios")
        SHADOW = candidate_shadow
    active_run = {
        "candidate_id": candidate_id,
        "run_dir": str(SHADOW.relative_to(ROOT)),
        "candidate_params_file": str(candidate_params) if candidate_params else None,
        "updated_at": dt.datetime.now().isoformat(timespec="seconds"),
    }
    (SHADOW_BASE / "active_run.json").write_text(
        json.dumps(active_run, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    statuses = _scan_and_trade(candidate_params, "shadow", stock_pool)
    if candidate_params:
        SHADOW = paired_root
        paired = _scan_and_trade(None, "paired_baseline", stock_pool)
        SHADOW = candidate_shadow
        statuses.update({f"paired_{key}": value for key, value in paired.items()})
    statuses["patrol"] = _run_step(
        "shadow_patrol", "scripts/shadow_patrol.py", {}, 300,
    )
    statuses["evaluate"] = _run_step(
        "shadow_evaluate", "scripts/shadow_evaluate.py",
        {"PAIRED_BASELINE_ROOT": str(paired_root)} if paired_root else {}, 300,
    )
    out = SHADOW / "evaluation" / f"chain_status_{_date()}.json"
    out.write_text(json.dumps({
        "date": _date(), "strategy": "v6-shadow", "candidate_id": candidate_id, "statuses": statuses,
        "main_portfolio_untouched": True,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [v for v in statuses.values() if v in {"error", "timeout"}]
    print(f"[shadow] 链路状态：{json.dumps(statuses, ensure_ascii=False)}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
