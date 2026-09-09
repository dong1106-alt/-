#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""超级智能体·每日微信日报（Server酱）
由 scheduler.py 在交易日 17:45 调用：读取当天 logs/ 与 reports/，
生成中文日报并推送到微信。未启用推送时仅打印跳过，不报错。"""
import datetime
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

from wechat_push import wechat_push  # noqa: E402

LOGS = ROOT / "logs"
REPORTS = ROOT / "reports"
DATA = ROOT / "data"
WEEK_CN = "一二三四五六日"


def _read(path, maxlen=8000):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:maxlen]
    except Exception:
        return ""


def task_state(name, ds, log_dir=LOGS):
    log = Path(log_dir) / f"{name}_{ds}.log"
    if not log.exists():
        return "missing"
    txt = _read(log, 300000)
    # 同一日志可能包含补跑：以最后一个运行标记为准，避免“先失败、后成功”仍被日报报成异常。
    markers = [line for line in txt.splitlines()
               if "RUN_OK" in line or "RUN_ERROR" in line or "RUN_TIMEOUT" in line]
    if markers:
        last = markers[-1]
        if "RUN_OK" in last:
            return "ok"
        return "timeout" if "RUN_TIMEOUT" in last else "error"
    if "Traceback" in txt:
        return "error"
    return "ok"


def _find(d, keys):
    if isinstance(d, dict):
        for k in keys:
            if k in d and d[k] is not None:
                return d[k]
        for v in d.values():
            r = _find(v, keys)
            if r is not None:
                return r
    return None


def portfolio_brief(data_dir=DATA):
    p = Path(data_dir) / "sim_trades" / "portfolio.json"
    if not p.exists():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    nav = _find(d, ["net_value", "total_value", "total_asset", "equity", "net_asset", "净值", "总资产", "净资产"])
    cash = _find(d, ["cash", "available_cash", "现金"])
    npos = _find(d, ["position_count", "positions"])
    parts = []
    if nav is not None:
        parts.append(f"净值≈{nav}")
    if cash is not None:
        parts.append(f"现金≈{cash}")
    if isinstance(npos, int):
        parts.append(f"持仓数={npos}")
    elif isinstance(npos, (list, dict)):
        parts.append(f"持仓数≈{len(npos)}")
    return "，".join(parts) if parts else None


def json_list_summary(path, topn=5):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None
    # 信号文件的真实结构：buy_signals / sell_signals。日报分别报告买卖数量，
    # 避免把 market_state 等字典字段误算成“信号条数”。
    if isinstance(data, dict) and ("buy_signals" in data or "sell_signals" in data):
        buys = data.get("buy_signals") or []
        sells = data.get("sell_signals") or []
        names = []
        for it in buys[:topn]:
            if isinstance(it, dict):
                name = it.get("name") or it.get("code") or it.get("symbol") or ""
                score = it.get("quality_score") or it.get("score") or ""
                names.append(f"{name}" + (f"({score})" if score not in ("", None) else ""))
        return len(buys), len(sells), names

    items = data if isinstance(data, list) else None
    if isinstance(data, dict):
        for k in ("signals", "data", "items", "result", "results"):
            v = data.get(k)
            if isinstance(v, list):
                items = v
                break
        if items is None and data:
            items = list(data.values())
    if not isinstance(items, list):
        return None
    names = []
    for it in items[:topn]:
        if isinstance(it, dict):
            name = it.get("name") or it.get("stock_name") or it.get("code") or it.get("symbol") or ""
            score = it.get("score") or it.get("quality_score") or it.get("quality") or it.get("综合质量评分") or ""
            names.append(f"{name}" + (f"({score})" if score not in ("", None) else ""))
        elif it is not None:
            names.append(str(it))
    return len(items), 0, names


def active_shadow_run():
    """返回当前影子运行目录及候选标识；无指针时兼容旧v6影子目录。"""
    base = ROOT / "data" / "shadow"
    pointer = base / "active_run.json"
    try:
        info = json.loads(pointer.read_text(encoding="utf-8"))
        run_dir = (ROOT / info["run_dir"]).resolve()
        if run_dir.is_relative_to(base.resolve()):
            return run_dir, info.get("candidate_id", "v6-risk-baseline")
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        pass
    return base, "v6-risk-baseline"


def candidate_brief(ds):
    path = ROOT / "data" / "candidates" / "evaluations" / f"candidate-{ds}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return f"{data.get('decision', 'unknown')}：{data.get('reason', '无说明')}"
    except (OSError, ValueError, json.JSONDecodeError):
        return "今日未生成候选或数据不足"


def main():
    today = datetime.date.today()
    ds = today.strftime("%Y-%m-%d")
    if today.weekday() >= 5:
        print(f"[summary] {ds} 周末，无交易任务，跳过")
        return 0

    names = ["daily_signal_scan", "sim_trade_tracker", "daily_pipeline_codeact", "daily_optimize"]
    if today.day == 1:
        names.append("monthly_reoptimize")
    status = {n: task_state(n, ds) for n in names}

    lines = [f"## 超级智能体日报 {ds}（周{WEEK_CN[today.weekday()]}）"]

    if status["daily_signal_scan"] == "ok":
        res = None
        for cand in (
            ROOT / "reports" / "latest_signals.json",
            DATA / "latest_signals.json",
            DATA / "signals" / f"signals_{ds}.json",
        ):
            if cand.exists():
                res = json_list_summary(cand)
                if res:
                    break
        if res:
            buy_cnt, sell_cnt, top = res
            head = f"- 扫描：已运行，买入 {buy_cnt} 条、卖出 {sell_cnt} 条"
            if top:
                head += f"，Top{len(top)}：{'、'.join(top)}"
            lines.append(head)
        else:
            lines.append("- 扫描：已运行（信号明细见 data/latest_signals.json 或日志）")
    elif status["daily_signal_scan"] in {"error", "timeout"}:
        lines.append("- 扫描：运行异常/超时，见日志")
    else:
        lines.append("- 扫描：今日未运行")

    pb = portfolio_brief()
    if status["sim_trade_tracker"] == "ok":
        lines.append("- 模拟交易：已运行" + (f"，{pb}" if pb else ""))
    elif status["sim_trade_tracker"] in {"error", "timeout"}:
        lines.append("- 模拟交易：运行异常/超时，见日志")
    else:
        lines.append("- 模拟交易：今日未运行")

    patrol_txt = _read(REPORTS / f"每日巡检_{ds}.txt", 1800)
    if patrol_txt:
        tail = [ln.strip() for ln in patrol_txt.splitlines() if ln.strip()][-6:]
        lines.append("- 巡检：" + ("；".join(tail) if tail else "已运行"))
    else:
        lines.append("- 巡检：" + ("运行异常/超时，见日志" if status["daily_pipeline_codeact"] in {"error", "timeout"} else "今日未运行"))

    if status["daily_optimize"] == "ok":
        tail = _read(LOGS / f"daily_optimize_{ds}.log", 1200)
        if tail:
            lines.append("- 每日优化：已完成，节选：")
            lines.append("> " + tail.replace("\n", "\n> "))
        else:
            lines.append("- 每日优化：已完成")
    elif status["daily_optimize"] in {"error", "timeout"}:
        lines.append("- 每日优化：运行异常/超时，见日志")
    else:
        lines.append("- 每日优化：今日未运行")

    if today.day == 1:
        ms = status.get("monthly_reoptimize", "missing")
        if ms == "ok":
            lines.append("- 月度重优化：已完成")
        elif ms in {"error", "timeout"}:
            lines.append("- 月度重优化：运行异常/超时，见日志")
        else:
            lines.append("- 月度重优化：仍在进行或未完成")

    # v6 影子组合：只读 shadow/ 下的独立文件，绝不把影子结果混入主盘。
    shadow_data, candidate_id = active_shadow_run()
    shadow_logs = shadow_data / "logs"
    shadow_status = {
        n: task_state(n, ds, shadow_logs)
        for n in ("shadow_scan", "shadow_sim_trade", "shadow_patrol", "shadow_evaluate")
    }
    shadow_report = _read(shadow_data / "reports" / f"每日巡检_{ds}.txt", 1800)
    shadow_pb = portfolio_brief(shadow_data)
    lines.append("")
    lines.append(f"【影子组合（{candidate_id}，不改变主模拟盘）】")
    if shadow_status["shadow_scan"] == "ok":
        shadow_signal = shadow_data / "signals" / "latest_signals.json"
        shadow_res = json_list_summary(shadow_signal) if shadow_signal.exists() else None
        if shadow_res:
            buy_cnt, sell_cnt, top = shadow_res
            lines.append(f"- 扫描：已运行，买入 {buy_cnt} 条、卖出 {sell_cnt} 条" + (f"，Top{len(top)}：{'、'.join(top)}" if top else ""))
        else:
            lines.append("- 扫描：已运行，信号明细缺失")
    elif shadow_status["shadow_scan"] in {"error", "timeout"}:
        lines.append("- 扫描：运行异常/超时，见影子日志")
    else:
        lines.append("- 扫描：今日未运行")
    if shadow_status["shadow_sim_trade"] == "ok":
        lines.append("- 模拟交易：已运行" + (f"，{shadow_pb}" if shadow_pb else ""))
    elif shadow_status["shadow_sim_trade"] in {"error", "timeout"}:
        lines.append("- 模拟交易：运行异常/超时，见影子日志")
    else:
        lines.append("- 模拟交易：今日未运行")
    if shadow_report:
        tail = [ln.strip() for ln in shadow_report.splitlines() if ln.strip()][-5:]
        lines.append("- 巡检：" + ("；".join(tail) if tail else "已运行"))
    else:
        lines.append("- 巡检：今日未运行或报告缺失")
    eval_path = shadow_data / "reports" / f"候选评估_{ds}.txt"
    eval_txt = _read(eval_path, 900)
    lines.append("- 参数评估：" + (eval_txt.replace("\n", "；") if eval_txt else "今日未运行或报告缺失"))
    lines.append("- 新候选样本外预筛：" + candidate_brief(ds))

    missing = [k for k, v in status.items() if v == "missing"]
    errors = [k for k, v in status.items() if v in {"error", "timeout"}]
    if missing:
        lines.append(f"\n说明：以下任务今日无日志（未到时间或电脑未开机）：{'、'.join(missing)}")
    if errors:
        lines.append(f"异常：{'、'.join(errors)} 存在错误/超时标记，详见对应日志。")
    shadow_missing = [k for k, v in shadow_status.items() if v == "missing"]
    shadow_errors = [k for k, v in shadow_status.items() if v in {"error", "timeout"}]
    if shadow_missing:
        lines.append(f"影子说明：以下任务今日无日志：{'、'.join(shadow_missing)}")
    if shadow_errors:
        lines.append(f"影子异常：{'、'.join(shadow_errors)} 存在错误/超时标记，未编造结果。")

    title = f"超级智能体日报 {ds}"
    desp = "\n".join(lines)
    print("=" * 60)
    print(title)
    print(desp)
    print("=" * 60)
    wechat_push(title, desp)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
