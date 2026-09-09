# -*- coding: utf-8 -*-
"""
月度参数优化决策层 E2E 验证（wrapper）。
流程：备份 optimal_params.json -> 子进程真实执行 monthly_reoptimize()（540s硬超时）
     -> runner 内完成6项断言并产出JSON -> 本脚本汇总并以 notify 提交。
参数: sys.argv[1]=result_mode(默认notify) sys.argv[2]=项目根目录(默认/app/data/所有对话/主对话)
      sys.argv[3]=子进程超时秒(默认540)
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===
import asyncio
import json
import os
import shutil
import sys
from datetime import datetime

from codeact_sdk import CodeActSDK

DEFAULT_BASE = f"{_ROOT}"
RUNNER_NAME = "_e2e_monthly_runner.py"


def tail(path, n=3000):
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - n * 3))
            return f.read().decode("utf-8", errors="replace")[-n:]
    except Exception as e:
        return f"(读取日志失败: {e})"


async def main():
    result_mode = sys.argv[1] if len(sys.argv) > 1 else "notify"
    base_dir = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_BASE
    proc_timeout = int(sys.argv[3]) if len(sys.argv) > 3 else 540
    actual_mode = result_mode if result_mode in ("display_only", "notify", "no_reply") else "notify"

    print(f"[参数] result_mode={result_mode}-> {actual_mode}, base={base_dir}, timeout={proc_timeout}s")
    sdk = CodeActSDK()

    params_file = os.path.join(base_dir, "data", "optimal_params.json")
    backup_file = os.path.join(base_dir, "data", "optimal_params.json.bak_e2e_pre")
    runner_path = os.path.join(base_dir, "codeact", "scripts", RUNNER_NAME)
    out_dir = os.path.join(base_dir, "codeact", "output")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    result_json = os.path.join(out_dir, f"e2e_monthly_result_{stamp}.json")
    log_path = os.path.join(out_dir, f"e2e_monthly_run_{stamp}.log")

    backup_ok = False
    try:
        # ---- 1. 备份 ----
        shutil.copy2(params_file, backup_file)
        backup_ok = os.path.exists(backup_file)
        print(f"[备份] {params_file} -> {backup_file} (ok={backup_ok})")

        # ---- 2. 子进程真实运行（540s 硬超时）----
        cmd = [sys.executable, "-u", runner_path, result_json, log_path]
        print(f"[运行] {' '.join(cmd)} (cwd={base_dir})")
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=base_dir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=proc_timeout)
            rc = proc.returncode
            stdout_s = stdout.decode("utf-8", errors="replace") if stdout else ""
            timed_out = False
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception:
                pass
            rc = -9
            stdout_s = ""
            timed_out = True
            print(f"[超时] 子进程超过 {proc_timeout}s 被 kill")

        print(f"[子进程] exit={rc}, timed_out={timed_out}")

        # ---- 3. 超时/崩溃路径 ----
        if timed_out:
            msg = (
                f"[主人](at://owner) 月度优化E2E验证：{proc_timeout}s超时被kill\n"
                f"进度（stdout最后位置）:\n{tail(log_path, 2500)}"
            )
            await sdk.submit_result(
                result_mode="notify", status="error", message=msg,
                data={"error_type": "timeout", "timeout_sec": proc_timeout,
                      "backup": backup_file, "log_path": log_path},
            )
            return

        if not os.path.exists(result_json):
            msg = (
                f"[主人](at://owner) 月度优化E2E验证：子进程异常退出(rc={rc})，未产出结果JSON。\n"
                f"stdout尾部:\n{stdout_s[-2500:]}\n日志尾部:\n{tail(log_path, 1500)}"
            )
            await sdk.submit_result(
                result_mode="notify", status="error", message=msg,
                data={"error_type": "subprocess_failed", "returncode": rc,
                      "backup": backup_file, "log_path": log_path},
            )
            return

        with open(result_json, "r", encoding="utf-8") as f:
            res = json.load(f)

        # ---- 4. 汇总 ----
        lines = []
        lines.append("[主人](at://owner) 月度参数优化决策层 E2E 验证结果")
        lines.append("")

        if res.get("fatal_error"):
            lines.append("❌ runner 发生致命异常：")
            lines.append(res["fatal_error"][-1500:])
            await sdk.submit_result(
                result_mode="notify", status="error",
                message="\n".join(lines)[:3500],
                data={"error_type": "runner_fatal", "result_json": result_json,
                      "log_path": log_path, "backup": backup_file},
            )
            return

        # ① 三状态 trades 与验证夏普
        lines.append("① 三状态训练/验证段交易与夏普：")
        for st in ["bull", "bear", "sideways", "transition"]:
            run = res.get("state_runs", {}).get(st, {})
            if not run.get("present"):
                lines.append(f"  - {st}: 本次未运行（状态天数<30被跳过/文件无条目）")
                continue
            lines.append(
                f"  - {st}: 训练夏普={run.get('best_train_sharpe')}, "
                f"验证trades={run.get('val_trades')}, 验证夏普={run.get('val_sharpe')}, "
                f"基准夏普={run.get('baseline_val_sharpe')}, walk-forward={run.get('walk_forward')}"
            )
        lines.append("")

        # ② 决策结果
        lines.append("② 决策结果（采纳/拒绝+真实理由）：")
        decs = res.get("decisions", [])
        if not decs:
            lines.append("  - 未解析到Step5决策（可能优化提前失败）")
        for d in decs:
            if d["action"] == "adopted":
                lines.append(f"  - ✅ {d['state']}: 采纳 (夏普 {d.get('old_sharpe')} → {d.get('new_sharpe')})")
            else:
                lines.append(f"  - ⏭️ {d['state']}: 拒绝/保留旧参数 — {d['reason']}")
        lines.append("")

        # ③ 落盘文件三状态
        lines.append("③ 落盘后 optimal_params.json：")
        ff = res.get("final_file") or {}
        for st in ["bull", "bear", "sideways", "transition"]:
            r = ff.get(st)
            if not r:
                lines.append(f"  - {st}: 无条目")
                continue
            lines.append(
                f"  - {st}: status={r.get('status')}, pre_filter={r.get('pre_filter_threshold')}, "
                f"max_pos={r.get('max_concurrent_positions')}, val_sharpe={r.get('val_sharpe')}, "
                f"val_trades={r.get('val_trades')}"
            )
        lines.append("")

        # ④ bear 防守参数核对
        bear_a = next((a for a in res["assertions"] if a["name"].startswith("c.")), None)
        lines.append("④ bear 反馈深度防守参数核对：")
        lines.append(f"  - {bear_a['detail'] if bear_a else '未执行'}")
        lines.append("")

        # ⑤ 断言总览
        lines.append("⑤ 断言结果：")
        all_pass = True
        for a in res["assertions"]:
            mark = "✅" if a["pass"] else "❌"
            lines.append(f"  {mark} {a['name']}")
            if not a["pass"]:
                all_pass = False
                lines.append(f"       → {a['detail'][:600]}")
        lines.append("")
        lines.append(f"总结论：{'✅ 全部断言通过' if all_pass else '❌ 存在失败断言（详见上文❌项）'}")
        lines.append(f"备份: {backup_file} | 完整日志: {log_path}")
        lines.append(f"结果JSON: {result_json}")

        message = "\n".join(lines)
        # message 长度保护
        if len(message) > 5500:
            message = message[:5200] + "\n...(截断，完整内容见结果JSON/日志)"

        await sdk.submit_result(
            result_mode=actual_mode,
            status="success",
            message=message,
            data={
                "all_pass": all_pass,
                "assertions": [{"name": a["name"], "pass": a["pass"]} for a in res["assertions"]],
                "decisions": decs,
                "final_file": ff,
                "state_runs": res.get("state_runs", {}),
                "backup": backup_file,
                "result_json": result_json,
                "log_path": log_path,
            },
        )

    except Exception as e:
        await sdk.submit_result(
            result_mode="notify", status="error",
            message=f"[主人](at://owner) E2E验证wrapper执行失败: {type(e).__name__}: {e}",
            data={"error_type": type(e).__name__, "backup": backup_file if backup_ok else None},
        )


asyncio.run(main())