#!/usr/bin/env python3
"""postToolUse：记录本轮用过哪些记忆沙箱工具，供读侧门禁与写侧兜底判断。

- 调用过 memory_prepare / memory_ask  → 写 {conv}.prepared（读侧门禁据此放行）
- 调用过 memory_remember / memory_update / memory_delete
                                      → 写 {conv}.remembered（stop 兜底据此不追问）

标记按 conversation_id 存放，每轮 stop 时清掉，所以「每轮都要查、每轮都要记」。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".cursor" / "memory-sandbox-hook-state"


def _nested_run() -> bool:
    """记忆沙箱自己拉起来的嵌套 agent（`MEMORY_SANDBOX_NESTED=1`）不记标记。

    它那侧读写两个门禁都已放行，标记写了也没人看，只会往状态目录里堆文件。
    """
    return (os.environ.get("MEMORY_SANDBOX_NESTED") or "").strip().lower() not in (
        "",
        "0",
        "false",
    )


def _conversation_id(data: dict) -> str:
    for key in ("conversation_id", "session_id", "generation_id"):
        val = data.get(key)
        if val:
            return str(val)
    return "default"


def _tool_name(data: dict) -> str:
    # Cursor 的 MCP 工具名有多种形式，如 MCP:memory_remember
    tool = str(data.get("tool_name") or "")
    if tool:
        return tool
    tip = data.get("tool_input")
    if isinstance(tip, dict):
        return str(tip.get("name") or tip.get("tool") or "")
    return ""


def main() -> int:
    if _nested_run():
        print("{}")
        return 0
    # 兜底 hook 自己绝不能挡住正常工作流，任何异常都静默放过
    try:
        data = json.load(sys.stdin)
        tool = _tool_name(data).lower()
        marks = []
        if "memory_prepare" in tool or "memory_ask" in tool:
            marks.append("prepared")
        # 改掉过时结论、删掉已失效条目，同样是「这一轮维护了记忆」。
        # 只认 memory_remember 的话，正确地更新了旧条目反而会被判成没落库，
        # 兜底提示就会催着再写一条新的——那正是规则里禁止的新旧并存。
        if any(
            name in tool
            for name in ("memory_remember", "memory_update", "memory_delete")
        ):
            marks.append("remembered")
        if marks:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            conv = _conversation_id(data)
            for mark in marks:
                (STATE_DIR / f"{conv}.{mark}").write_text("1", encoding="utf-8")
    except Exception:
        pass
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
