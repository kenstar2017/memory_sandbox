#!/usr/bin/env python3
"""postToolUse：记录本轮用过哪些记忆沙箱工具，供读侧门禁与写侧兜底判断。

- 调用过 memory_prepare / memory_ask  → 写 {conv}.prepared（读侧门禁据此放行）
- 调用过 memory_remember              → 写 {conv}.remembered（stop 兜底据此不追问）

标记按 conversation_id 存放，每轮 stop 时清掉，所以「每轮都要查、每轮都要记」。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".cursor" / "memory-sandbox-hook-state"


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
    # 兜底 hook 自己绝不能挡住正常工作流，任何异常都静默放过
    try:
        data = json.load(sys.stdin)
        tool = _tool_name(data).lower()
        marks = []
        if "memory_prepare" in tool or "memory_ask" in tool:
            marks.append("prepared")
        if "memory_remember" in tool:
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
