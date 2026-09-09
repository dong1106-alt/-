#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""通用 agent 唤醒脚本（agent_followup_reminder 场景）

脚本本身不执行任何业务检查逻辑，只负责把传入的 task_description
按指定的 result_mode（本场景调用方传 notify）原文提交回主会话，
唤醒主 agent 跟进对应任务。上屏与否由 result_mode 决定，脚本不做业务判断。

同类唤醒任务复用本脚本：日历通过 script_path + script_args 挂载，
script_args 中传入各自的 task_description 即可，不为每个任务新建脚本。

参数顺序（codeact_args / script_args）：result_mode, task_description
- result_mode: display_only / notify / no_reply / auto
              （本场景默认 notify；auto 映射为 notify；非法值兜底 notify）
- task_description: 中文任务描述字符串，原文回传给主会话
"""

import asyncio
import sys

from codeact_sdk import CodeActSDK

VALID_MODES = {"display_only", "notify", "no_reply"}
DEFAULT_MODE = "notify"


def norm_mode(raw: str) -> str:
    """归一化 result_mode：auto/非法值兜底为 notify，合法值原样透传。"""
    mode = (raw or "").strip().lower()
    if mode in VALID_MODES:
        return mode
    return DEFAULT_MODE


async def main():
    # 参数顺序：result_mode, task_description（result_mode 固定第一，平台约定）
    result_mode = norm_mode(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_MODE)
    task_description = " ".join(sys.argv[2:]).strip() if len(sys.argv) > 2 else ""
    print(f"[参数] result_mode={result_mode}, task_description={task_description}")

    sdk = CodeActSDK()
    try:
        if not task_description:
            await sdk.submit_result(
                result_mode="notify",
                status="error",
                message="agent 唤醒执行失败：缺少 task_description 参数",
                data={"error_type": "MissingTaskDescription"},
            )
            return

        # 纯透传：不加工、不判断，原文提交回主会话
        await sdk.submit_result(
            result_mode=result_mode,
            status="success",
            message=task_description,
            data={"task_description": task_description},
        )
    except Exception as e:
        await sdk.submit_result(
            result_mode="notify",
            status="error",
            message=f"agent 唤醒执行失败: {e}",
            data={"error_type": type(e).__name__},
        )


if __name__ == "__main__":
    asyncio.run(main())
