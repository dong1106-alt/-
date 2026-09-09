#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地化 codeact_sdk 替代层:
原扣子沙箱的 CodeActSDK.submit_result() 负责向 APP/主会话推送。
本地改为: 结果写入 reports/ 目录 + 控制台输出(可选的桌面弹窗/微信推送留空)。
接口与用法保持兼容: `sdk = CodeActSDK(); await sdk.submit_result(result_mode=..., status=..., message=..., data=...)`
"""
import asyncio
import datetime
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPORTS_DIR = Path(os.environ.get("CODEACT_REPORTS_DIR", str(ROOT / "reports")))


class CodeActSDK:
    def __init__(self, *args, **kwargs):
        pass

    async def submit_result(self, result_mode="display_only", status="success",
                            message="", data=None, **kwargs):
        try:
            REPORTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out = REPORTS_DIR / f"通知_{ts}.txt"
            lines = []
            lines.append(f"时间: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"mode: {result_mode} | status: {status}")
            lines.append("=" * 60)
            lines.append(str(message))
            if data:
                lines.append("")
                lines.append("data: " + json.dumps(data, ensure_ascii=False, default=str))
            out.write_text("\n".join(lines), encoding="utf-8")
            print(f"[本地通知] {out}")
            print(str(message)[:2000])
        except Exception as e:
            print(f"[通知写入失败] {e}")

    def close(self):
        pass
