# 超级智能体（龟缠量化v6）本地迁移改造说明

> 你好 Codex。这个文件夹是一套完整运行中的 A 股量化信号系统，原运行在扣子（Coze）云沙箱，
> 用户要把它搬到**本地个人电脑（Windows / macOS）**常驻运行。请按本说明改造，**不要改动任何交易策略逻辑和风控参数规则**，只做运行环境适配。

## 一、系统是什么

龟缠安泰 v6 量化策略（主文件：`龟缠量化v6_optimized.py`，约 4600+ 行），每个交易日自动：
1. 15:00 扫描沪深主板+创业板全部股票，输出买入/卖出信号（综合质量评分排序 Top10，带分批买入区间）
2. 15:20 模拟盘自动执行信号（portfolio 记账，不扣税费）
3. 15:30 巡检（净值、回撤、持仓检查）
4. 16:30 每日参数优化（Optuna 小样本 + 熔断保护）
5. 每月 1 号 15:30 月度全量重优化（三市场状态 walk-forward + 四层熔断决策）

市场状态：bull / bear / sideways / transition，由指数数据判定；状态决定仓位大小，**不改变选股标准**。

## 二、文件清单

| 路径 | 作用 |
|------|------|
| `龟缠量化v6_optimized.py` | 主策略：数据下载、指标、信号、回测、Optuna 优化、熔断决策、模拟盘全部在内 |
| `scripts/daily_signal_scan.py` | 15:00 信号扫描（30并发，腾讯API多端点） |
| `scripts/sim_trade_tracker.py` | 15:20 模拟盘执行 |
| `scripts/daily_pipeline_codeact.py` | 15:30 巡检 |
| `scripts/daily_optimize.py` | 16:30 每日优化 wrapper（沙箱540s超时） |
| `scripts/monthly_reoptimize.py` | 每月1号月度优化 wrapper（沙箱540s超时） |
| `scripts/monthly_reopt_e2e_verify.py` + `_e2e_monthly_runner.py` | 月度决策层端到端验证脚本（改完代码必须跑它验证） |
| `scripts/guichan_v6_stock_signal_analysis.py` | 单股盘中实时分析（按需工具） |
| 其他 .py | 回测对比、信号校验、烟雾测试等辅助工具 |
| `data/` | 核心状态数据（见下），**已剔除 data/stocks/ 330M 的个股K线缓存——首次运行自动在线重建** |

`data/` 内：`market_state_timeline.json`（582天市场状态）、`optimal_params.json`（三状态在用参数）、`feedback_adjustments.json`（反馈引擎调整）、`params_history.json`、`sim_trades/`（模拟盘组合与交易记录）、`index/`（指数K线）、`all_main_board_codes.txt`（股票名单）等。

## 三、必须完成的改造

### 1. 路径去硬编码
grep 所有 `/app/data/所有对话/主对话` 及沙箱绝对路径，统一改为**项目根目录相对定位**：
- 主文件：`ROOT = Path(__file__).resolve().parent`，数据目录 `ROOT/'data'`
- `scripts/` 下脚本：`ROOT = Path(__file__).resolve().parent.parent`，并 `sys.path.insert(0, str(ROOT))` 后再 import 主策略
- 输出引用 `codeact/output/` 的地方：信号报告改 `ROOT/'reports'`，运行日志改 `ROOT/'logs'`（目录自建）；注意 `sim_trade_tracker.py` 依赖扫描产出的 `latest_signals.json`，保持两者读写路径一致（建议统一放 `data/`）

### 2. 通知层替换（原为扣子沙箱 SDK）
脚本中 `submit_result(result_mode='display_only'/'notify')` 是向扣子APP推送的沙箱专有调用，替换为本地通知：
- 每次运行结果写入 `reports/报告_日期.txt`
- 桌面弹窗通知（可选，用 plyer；导入失败静默跳过）
- 预留微信推送配置位（Server酱 sct.ftqq.com：POST https://sctapi.ftqq.com/{SENDKEY}.send，title/desp；token 从 config.json 读，为空跳过；失败静默）
- 无事项场景（如非交易日跳过、优化未达标保留旧参数）只写日志不弹窗

### 3. 定时调度（原为扣子日历触发，本地需要自建）
写一个常驻 `scheduler.py`（或用系统计划任务/cron，二选一，推荐常驻脚本对小白用户更简单）：
- 周一至周五 15:00 扫描 → 15:20 模拟交易 → 15:30 巡检 → 16:30 每日优化；每月 1 号 15:30 月度优化（脚本内部已有交易日判断，非交易日自动跳过，调度器照触发即可）
- subprocess 调用脚本，日志写 `logs/任务名_日期.log`
- **开机补跑**：调度器启动时若今天已过任务时间但 logs 里没有今天成功记录，立即按链路顺序补跑（用户电脑白天可能关机）
- 超时放宽：本地无沙箱 600 秒限制，扫描类 1800s、月度 3600s（同步把 daily_optimize.py / monthly_reoptimize.py wrapper 里的 540 超时常量改 1800/3600）
- 提供 Windows `启动.bat`（cd /d %~dp0 && python scheduler.py）、macOS `start.sh`，以及 `安装依赖.bat/setup.sh`（建 venv + pip install）

### 4. Python 依赖
requirements：`pandas numpy pyarrow requests optuna plyer schedule`（版本不限，3.10+ 均可）。
脚本里原有"缺包自动 pip 安装"逻辑保留。

## 四、绝对不能动的铁律（策略/风控规则）

1. **统一买入门槛：综合质量评分 ≥ 60**（动态微调：牛市 -5、熊市 +5；bear 当前因反馈引擎深度防守实际 68）。市场状态只决定仓位，绝不降低选股标准。
2. **参数熔断基准是 STATE_BASELINES（状态专属基准，pre_filter=60），不是 v5 默认 BASELINE_PARAMS（pre_filter=40）**；新参数偏离状态基准 ±30% 硬拒、±20% 软限超 5 个拒、新夏普 < 同口径基准×0.95 拒、月度夏普提升 <10% 不采纳。
3. **月度决策链路**：run_optimization 之前必须快照旧参数（old_snapshot）；compare_params 在内存决策后必须落盘回写；回滚被拒提案时保留旧条目原有 status；select_params 任何回退路径 pre_filter 不得低于 60。
4. **风控变更验收**：任何风控逻辑改动必须回测验证，夏普下降超 5% 回滚。
5. K 线一律**前复权 qfq**；回测窗口不得先截断 K 线再算指标（指标用完整数据，仅限制交易/统计窗口）。
6. 模拟盘**不扣税费**；熔断时不足 100 股零头全部卖出；暴跌日不买入、涨停板不追。
7. 数据源以**腾讯为主**：proxy.finance.qq.com 端点第一优先，qt.gtimg.cn 实时行情兜底；新浪/东方财富/网易在沙箱 IP 被封（本地网络可能可用，但不要改回主通道）。
8. 每日信号推送只展示 Top10、按综合质量评分降序、必须带分批买入区间；完整报告只保留当天。

## 五、改造后必须做的验证（禁止只改不验）

1. 全部 .py 文件 `py_compile` 通过；grep 确认无 `/app/data` 残留
2. 全新目录拷贝后验证：导入主策略，调用 `select_params('bull')` / `('bear')` / `('sideways')`，三状态 pre_filter 必须 ≥60，且 bear = 68（深度防守参数：max_positions=2、atr_multiplier≈1.437、stop_loss≈2.06、base_risk≈0.0545）
3. 跑 `scripts/monthly_reopt_e2e_verify.py` 真实端到端（会跑完整月度优化，约 10-20 分钟，本地不限时），断言：三状态回测有交易、决策落盘后三状态 status=adopted、pre_filter 全 ≥60。内存模拟/单元测试不算数。
4. 首次运行信号扫描会在线重建 data/stocks/ 缓存（4945 只股票，本地网络预计 1-3 小时，后台自动完成，中断可续）。

## 六、当前系统状态（2026-09-01）

- 市场状态：sideways（上证 3979.89，目标仓位 54%）
- 模拟盘：6/10 持仓，总资产 ¥979,637，累计 -2.04%，最大回撤 -4.74%
- 三状态在用参数：bull pre_filter=68/max_pos=5；bear pre_filter=68/max_pos=2（8/25 反馈引擎深度防守）；sideways pre_filter=60/max_pos=5
