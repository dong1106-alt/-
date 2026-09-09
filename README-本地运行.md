# 超级智能体(龟缠量化v6) · 本地运行说明

> 本目录由「超级智能体_本地版.zip」迁移改造而来，目标：在 Windows 本机常驻运行。
> 策略/风控规则一律未改，只做运行环境适配。

## 已完成的本地位改造
1. `config/settings.yaml` + `config/loader.py`：补上云端未导出的配置层，所有路径改为项目根目录相对定位（数据读 `data/`，结果写 `reports/`、日志写 `logs/`）。
2. `scripts/local_kline_skill.py`：本地“扣子 skill”替身，主策略代码零改动即走腾讯行情 API 拉前复权K线（多端点容灾）。
3. `codeact_sdk.py`：本地版 SDK 替身，把云端 `submit_result` 改为写 `reports/通知_*.txt` + 控制台输出（微信 Server酱推送已预留）。
4. 16 个脚本的云端绝对路径 `/app/data/所有对话/主对话` 全部改为项目根目录相对定位（含 `codeact/scripts`、`codeact/output` 兼容目录）。
5. `daily_pipeline.py`：本地每日巡检（净值/回撤/持仓只读检查）。
6. `scheduler.py` + `启动.bat` + `安装依赖.bat` + `一键检查.bat` + `requirements.txt`。

## 已验证（本机实测通过）
- 全部 .py `py_compile` 通过；无 `/app/data` 残留硬编码。
- 主程序导入成功，配置层/路径解析正常。
- 三状态参数校验：bull pre_filter=68 / bear=68(深度防守,max_pos=2,atr≈1.437,risk≈0.0545) / sideways=60，全部 ≥60 通过。
- 在线取数通道：拉取真实前复权K线成功（含当日 2026-09-02）。
- 端到端回测：2 只样本股 × 1 年真实行情回测跑通（2.4s）。
- 定时任务包装脚本（扫描/模拟/巡检/每日优化/月度优化）本地导入全部通过；月度 E2E 框架可运行（因本地暂无个股缓存，优化提前返回属预期）。

## 使用
1. 首次：双击 `安装依赖.bat`（需联网，装 pandas/pyarrow/optuna 等）。
2. 日常：双击 `启动.bat` 常驻后台（交易日 15:00 扫描 → 15:20 模拟 → 15:30 巡检 → 16:30 优化；每月1号 15:30 月度重优化；电脑关机后，下次开机自动补跑当天任务）。
3. 体检：双击 `一键检查.bat`（离线，校验三状态参数是否健康）。
4. 看结果：`reports/`（每日信号/巡检/通知）、`logs/`（任务日志）、`data/`（模拟盘与状态数据）。

## 注意
- 首次全市场扫描会在线重建 `data/stocks/` 个股K线缓存（约 4945 只，预计 1~3 小时，可中断续跑）。
- 交易日判断与取数需联网（腾讯行情 API）。
- 微信推送：到 sct.ftqq.com 免费申请 SENDKEY，填入 `config/settings.yaml` 的 `wechat_push.sckey` 并把 `enable` 改为 true。
- 模拟盘不扣税费、不会真实下单；报告默认只保留当天。
