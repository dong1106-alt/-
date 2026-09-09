# -*- coding: utf-8 -*-
"""
月度参数优化决策层 E2E 验证 runner（由 monthly_reopt_e2e_verify.py 以子进程调用）。
在项目根目录执行：真实导入 龟缠量化v6_optimized.py -> monthly_reoptimize() -> 6项断言 -> JSON结果。
用法: python3 _e2e_monthly_runner.py <result_json_path> <full_log_path>
"""
from pathlib import Path as _P
_ROOT = _P(__file__).resolve().parent.parent  # === LOCAL-PATCH v1 ===
import importlib.util
import io
import json
import os
import re
import sys
import traceback

BASE_DIR = f"{_ROOT}"
MAIN_FILE = os.path.join(BASE_DIR, "龟缠量化v6_optimized.py")
PARAMS_FILE = os.path.join(BASE_DIR, "data", "optimal_params.json")
STATES = ["bull", "bear", "sideways", "transition"]
CORE_STATES = ["bull", "bear", "sideways"]


class Tee:
    """同时写内存缓冲与实时日志文件（供wrapper超时后读取进度）。"""
    def __init__(self, log_path):
        self.buf = io.StringIO()
        self.f = open(log_path, "w", encoding="utf-8")

    def write(self, s):
        self.buf.write(s)
        try:
            self.f.write(s)
            self.f.flush()
        except Exception:
            pass

    def flush(self):
        try:
            self.f.flush()
        except Exception:
            pass

    def close(self):
        try:
            self.f.close()
        except Exception:
            pass


def main():
    result_json_path = sys.argv[1]
    log_path = sys.argv[2]
    tee = Tee(log_path)

    result = {
        "phase": "init",
        "fatal_error": None,
        "report": None,
        "state_runs": {},
        "decisions": [],
        "final_file": None,
        "assertions": [],
        "all_pass": False,
    }

    def add_assert(name, passed, detail):
        result["assertions"].append({"name": name, "pass": bool(passed), "detail": detail})
        print(f"[断言] {'PASS' if passed else 'FAIL'} {name}: {detail}", flush=True)

    try:
        os.chdir(BASE_DIR)
        sys.path.insert(0, BASE_DIR)

        print("=" * 60, flush=True)
        print("E2E runner: 导入主模块...", flush=True)
        spec = importlib.util.spec_from_file_location("gc_main", MAIN_FILE)
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        print("E2E runner: 模块导入完成，调用 monthly_reoptimize()", flush=True)
        print("=" * 60, flush=True)

        old_stdout = sys.stdout
        sys.stdout = tee
        try:
            report = m.monthly_reoptimize()
        finally:
            sys.stdout = old_stdout

        opt_log = tee.buf.getvalue()
        result["phase"] = "reoptimize_done"
        result["report"] = report if isinstance(report, str) else (str(report) if report else None)
        print("\n===== monthly_reoptimize 返回 =====", flush=True)
        print(result["report"] or "(无报告/返回None)", flush=True)

        if not report:
            add_assert("月度优化主流程返回报告", False,
                       "monthly_reoptimize() 返回空（优化可能失败提前return），后续断言无法执行")
            result["phase"] = "no_report"
            _dump(result_json_path, result, log_path)
            return

        # ---------- 断言a：从运行日志解析每个状态的训练/验证段交易与夏普 ----------
        markers = list(re.finditer(r"优化市场状态:\s*(\w+)\s*\(共(\d+)天\)", opt_log))
        state_blocks = {}
        for i, mt in enumerate(markers):
            st = mt.group(1)
            seg_start = mt.start()
            seg_end = markers[i + 1].start() if i + 1 < len(markers) else len(opt_log)
            state_blocks[st] = opt_log[seg_start:seg_end]

        for st in STATES:
            blk = state_blocks.get(st)
            if blk is None:
                result["state_runs"][st] = {"present": False}
                continue
            mv = re.search(r"验证集:\s*夏普=([-\d.]+|None|nan),\s*收益=([-\d.]+|None|nan)%,\s*交易=(\d+),\s*回撤=([-\d.]+|None|nan)%", blk)
            mb = re.search(r"基准验证:\s*夏普=([-\d.]+|None|nan),\s*收益=([-\d.]+|None|nan)%,\s*交易=(\d+),\s*回撤=([-\d.]+|None|nan)%", blk)
            mt_sharpe = re.search(r"最佳训练夏普:\s*([-\d.]+)", blk)
            m_wf = re.search(r"(✅ 验证通过|⚠️ 验证未通过)", blk)
            m_period = re.search(r"训练段:\s*(\S+)\s*~\s*(\S+).*?验证段:\s*(\S+)\s*~\s*(\S+)", blk, re.S)

            def _f(x):
                try:
                    return float(x)
                except Exception:
                    return None

            run = {
                "present": True,
                "val_sharpe": _f(mv.group(1)) if mv else None,
                "val_return": _f(mv.group(2)) if mv else None,
                "val_trades": int(mv.group(3)) if mv else None,
                "val_drawdown": _f(mv.group(4)) if mv else None,
                "baseline_val_sharpe": _f(mb.group(1)) if mb else None,
                "baseline_val_trades": int(mb.group(3)) if mb else None,
                "best_train_sharpe": _f(mt_sharpe.group(1)) if mt_sharpe else None,
                "walk_forward": "pass" if (m_wf and "通过" in m_wf.group(1)) else ("fail" if m_wf else "unknown"),
                "train_period": f"{m_period.group(1)}~{m_period.group(2)}" if m_period else None,
                "val_period": f"{m_period.group(3)}~{m_period.group(4)}" if m_period else None,
            }
            result["state_runs"][st] = run

        a_details = []
        a_pass = True
        for st in CORE_STATES:
            run = result["state_runs"].get(st, {})
            if not run.get("present"):
                a_pass = False
                a_details.append(f"{st}: 本次未运行(状态天数<30被跳过)")
                continue
            problems = []
            # 训练段：trades<10 时 objective 返回 -10 哨兵
            bts = run.get("best_train_sharpe")
            if bts is None:
                problems.append("训练段夏普缺失")
            elif bts <= -10:
                problems.append(f"训练段0交易哨兵(最佳训练夏普={bts})")
            # 验证段：trades>0 且夏普非None/非-10哨兵
            vt = run.get("val_trades")
            vs = run.get("val_sharpe")
            if vt is None or vt <= 0:
                problems.append(f"验证段trades={vt}(需>0)")
            if vs is None:
                problems.append("验证段夏普=None(0交易/回测失败)")
            elif vs <= -10:
                problems.append(f"验证段夏普哨兵值={vs}")
            if problems:
                a_pass = False
                a_details.append(f"{st}: " + "; ".join(problems) +
                                 f" [train_sharpe={bts}, val_trades={vt}, val_sharpe={vs}]")
            else:
                a_details.append(f"{st}: train_sharpe={bts:.3f}, val_trades={vt}, val_sharpe={vs:.3f}, "
                                 f"baseline_val_sharpe={run.get('baseline_val_sharpe')}, wf={run.get('walk_forward')}")
        if result["state_runs"].get("transition", {}).get("present"):
            tr = result["state_runs"]["transition"]
            a_details.append(f"transition: 本次运行 val_trades={tr.get('val_trades')}, val_sharpe={tr.get('val_sharpe')}")
        else:
            a_details.append("transition: 本次未运行(状态天数<30被跳过，文件无该状态条目)")
        add_assert("a.三状态训练/验证段trades>0且无-10哨兵", a_pass, " | ".join(a_details))

        # ---------- Step5 决策解析（断言f + 决策汇总） ----------
        step5 = opt_log.split("[5/5] 参数决策")[-1] if "[5/5] 参数决策" in opt_log else ""
        for line in step5.splitlines():
            ma = re.search(r"✅\s*(\w+):\s*采纳新参数\s*\(夏普\s*([-\d.]+)\s*→\s*([-\d.]+)\)", line)
            mr = re.search(r"⏭️\s*(\w+):\s*保留旧参数\s*\((.+)\)", line)
            if ma:
                result["decisions"].append({"state": ma.group(1), "action": "adopted",
                                            "old_sharpe": _f(ma.group(2)), "new_sharpe": _f(ma.group(3)),
                                            "reason": "采纳新参数"})
            elif mr:
                result["decisions"].append({"state": mr.group(1), "action": "rejected", "reason": mr.group(2).strip()})

        # ---------- 断言f：拒绝理由必须是真实熔断原因 ----------
        rejections = [d for d in result["decisions"] if d["action"] == "rejected"]
        f_details = []
        f_pass = True
        GENERIC = "夏普提升仅"  # 笼统理由特征
        for d in rejections:
            rsn = d["reason"]
            is_specific = any(k in rsn for k in ("walk-forward", "熔断1", "熔断2", "熔断3"))
            is_generic_only = (GENERIC in rsn) and not is_specific
            st_run = result["state_runs"].get(d["state"], {})
            wf_fail = st_run.get("walk_forward") == "fail"
            if is_generic_only:
                f_pass = False
                f_details.append(f"{d['state']}: 笼统理由「{rsn}」" +
                                 ("，但该状态walk-forward未通过，应显示walk-forward拒绝" if wf_fail else ""))
            else:
                tag = []
                if wf_fail and "walk-forward" not in rsn:
                    tag.append("注意:该状态wf未通过但理由非walk-forward")
                f_details.append(f"{d['state']}: {rsn}" + (f" [{';'.join(tag)}]" if tag else ""))
        if not rejections:
            f_details.append("本次无拒绝项（全部采纳），断言f自动通过")
        add_assert("f.拒绝理由为真实熔断原因(非笼统提升不足)", f_pass,
                   " | ".join(f_details) if f_details else "无拒绝项")

        # ---------- 落盘文件断言 b/c/d ----------
        with open(PARAMS_FILE, "r", encoding="utf-8") as f:
            final_data = json.load(f)
        final_results = {r["state"]: r for r in final_data.get("results", [])}
        result["final_file"] = {
            st: {
                "status": r.get("status"),
                "pre_filter_threshold": r.get("params", {}).get("pre_filter_threshold"),
                "max_concurrent_positions": r.get("params", {}).get("max_concurrent_positions"),
                "atr_multiplier": r.get("params", {}).get("atr_multiplier"),
                "stop_multiplier_base": r.get("params", {}).get("stop_multiplier_base"),
                "base_risk_pct": r.get("params", {}).get("base_risk_pct"),
                "val_sharpe": r.get("val_sharpe"),
                "val_trades": r.get("val_trades"),
            }
            for st, r in final_results.items()
        }

        # 断言b：每个状态 status=adopted 且 pre_filter>=60
        b_details, b_pass = [], True
        for st, r in final_results.items():
            probs = []
            if r.get("status") != "adopted":
                probs.append(f"status={r.get('status')}(要求adopted，否则select_params跳过该条目)")
                b_pass = False
            pf = r.get("params", {}).get("pre_filter_threshold")
            if pf is None or pf < 60:
                probs.append(f"pre_filter_threshold={pf}(要求>=60)")
                b_pass = False
            b_details.append(f"{st}: status={r.get('status')}, pre_filter={pf}" +
                             ("  <-- " + ";".join(probs) if probs else ""))
        add_assert("b.落盘条目status=adopted且pre_filter>=60", b_pass, " | ".join(b_details))

        # 断言c：bear 反馈深度防守参数
        bear = final_results.get("bear")
        c_pass, c_details = False, "落盘文件无bear条目"
        if bear:
            bp = bear.get("params", {})
            checks = [
                ("pre_filter_threshold", bp.get("pre_filter_threshold"), 68, 0.5),
                ("max_concurrent_positions", bp.get("max_concurrent_positions"), 2, 0.1),
                ("atr_multiplier", bp.get("atr_multiplier"), 1.437, 0.02),
                ("stop_multiplier_base", bp.get("stop_multiplier_base"), 2.06, 0.03),
                ("base_risk_pct", bp.get("base_risk_pct"), 0.0545, 0.002),
            ]
            bad = [f"{k}={v}(期望≈{exp})" for k, v, exp, tol in checks
                   if v is None or abs(float(v) - exp) > tol]
            c_pass = len(bad) == 0
            c_details = ("bear防守参数全部符合(pre_filter=68/max_pos=2/atr≈1.437/stop≈2.06/risk≈0.0545)"
                         if c_pass else "bear防守参数不符: " + "; ".join(bad) +
                         f" [status={bear.get('status')}]")
        add_assert("c.bear为反馈深度防守参数", c_pass, c_details)

        # 断言d：sideways max_concurrent_positions=5（采纳合规提案也允许但需说明）
        sw = final_results.get("sideways")
        d_pass, d_details = False, "落盘文件无sideways条目"
        if sw:
            mp = sw.get("params", {}).get("max_concurrent_positions")
            sw_dec = next((d for d in result["decisions"] if d["state"] == "sideways"), None)
            if mp == 5:
                d_pass = True
                d_details = (f"sideways max_pos=5（决策={sw_dec['action'] if sw_dec else '?'}: "
                             f"{sw_dec['reason'] if sw_dec else ''}，回滚/基准值正确）")
            else:
                # 非5：只有"采纳了合规提案"才允许（熔断1基准5，±30%硬限=> max_pos∈[4,6]为合规）
                if sw_dec and sw_dec["action"] == "adopted" and 4 <= (mp or 0) <= 6:
                    d_pass = True
                    d_details = (f"sideways max_pos={mp}（新合规提案被采纳，在基准5±30%内，允许；"
                                 f"理由: {sw_dec['reason']}）")
                else:
                    d_pass = False
                    d_details = (f"sideways max_pos={mp}(偏离基准5且非合规采纳，"
                                 f"决策={sw_dec['action'] if sw_dec else '?'}: {sw_dec['reason'] if sw_dec else ''})")
        add_assert("d.sideways max_concurrent_positions=5(或合规采纳)", d_pass, d_details)

        # ---------- 断言e：select_params 四状态 pre_filter>=60 ----------
        e_details, e_pass = [], True
        for st in STATES:
            try:
                ret = m.select_params(st)
                params = ret[0] if isinstance(ret, tuple) else ret
                source = ret[1] if isinstance(ret, tuple) and len(ret) > 1 else ""
                pf = params.get("pre_filter_threshold")
                ok = pf is not None and pf >= 60
                if not ok:
                    e_pass = False
                e_details.append(f"{st}: pre_filter={pf}, source={source}")
            except Exception as ex:
                e_pass = False
                e_details.append(f"{st}: select_params异常 {type(ex).__name__}: {ex}")
        add_assert("e.select_params四状态pre_filter>=60", e_pass, " | ".join(e_details))

        result["all_pass"] = all(a["pass"] for a in result["assertions"])
        result["phase"] = "done"
        _dump(result_json_path, result, log_path)

    except Exception as e:
        result["fatal_error"] = traceback.format_exc()
        result["phase"] = "fatal"
        try:
            add_assert("runner执行无异常", False, f"{type(e).__name__}: {e}")
        except Exception:
            pass
        _dump(result_json_path, result, log_path)
    finally:
        tee.close()


def _dump(path, result, log_path):
    result["log_path"] = log_path
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)
    print(f"[runner] 结果已写入 {path}", flush=True)


if __name__ == "__main__":
    main()