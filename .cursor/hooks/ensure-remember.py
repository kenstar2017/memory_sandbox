#!/usr/bin/env python3
"""stop：若本轮未 memory_remember，自动追问一轮强制写入（含排障/改代码）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".cursor" / "memory-sandbox-hook-state"

FOLLOWUP = (
    "【记忆沙箱强制落库】本轮结束前必须调用 memory_remember，把用户问题与最终结论写入长时记忆。"
    "排障、改代码、配置变更也要记。"
    "question 用精简问法或用户原话；answer 写清原因+做法+关键路径/命令；scene=dev。"
    "若刚才已调用过 memory_remember，只需回复「已记入长时记忆」。"
    "不要写入密钥/token/密码。"
)


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("{}")
        return 0

    status = data.get("status") or "completed"
    loop_count = int(data.get("loop_count") or 0)
    if status != "completed" or loop_count > 0:
        print("{}")
        return 0

    conv = (
        data.get("conversation_id")
        or data.get("session_id")
        or data.get("generation_id")
        or "default"
    )
    marker = STATE_DIR / f"{conv}.remembered"
    if marker.is_file():
        try:
            marker.unlink()
        except OSError:
            pass
        print("{}")
        return 0

    print(json.dumps({"followup_message": FOLLOWUP}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
