#!/usr/bin/env python3
"""postToolUse：若本轮调用了 memory_remember，打标供 stop hook 检查。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".cursor" / "memory-sandbox-hook-state"


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("{}")
        return 0

    tool = str(data.get("tool_name") or "")
    # Cursor MCP 工具名常见形式：MCP: user-memory-sandbox / memory_remember 等
    lowered = tool.lower()
    is_remember = (
        "memory_remember" in lowered
        or tool.endswith("memory_remember")
        or lowered.endswith("/memory_remember")
    )
    if not is_remember:
        # 也看 tool_input 里的 name（部分版本如此）
        tip = data.get("tool_input") or {}
        if isinstance(tip, dict):
            name = str(tip.get("name") or tip.get("tool") or "")
            is_remember = "memory_remember" in name

    if is_remember:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        conv = (
            data.get("conversation_id")
            or data.get("session_id")
            or data.get("generation_id")
            or "default"
        )
        (STATE_DIR / f"{conv}.remembered").write_text("1", encoding="utf-8")

    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
