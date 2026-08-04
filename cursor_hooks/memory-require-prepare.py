#!/usr/bin/env python3
"""preToolUse：本轮没查过记忆就要动手改东西时，拦一次并要求先 memory_prepare。

起因：agent 直接写技术方案文档，而「前端技术方案模板」早就在记忆里，它没查，写出来跑偏了。
光靠规则提醒不够，读侧也要有门禁。

只拦「动手」和「委派」，不拦读：Read / Grep / Shell 这些照常跑，探索速度不受影响。
同一轮最多拦一次（写 .gated 标记）——这样 MCP 挂了或 agent 坚持要做时不会死锁，
拦一次的提示配合 sessionStart 注入的协议已经足够。

子 agent 的工具调用同样会触发本 hook，但用的是它自己新的 conversation_id，
所以子 agent 也要自己查一次记忆（它的上下文是干净的，本来就该查）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".cursor" / "memory-sandbox-hook-state"

# 改文件、删文件、拉子 agent
GATED_TOOLS = {"write", "delete", "task"}
# 飞书写操作（别人立刻看得见，最不该在没查记忆的情况下做）
GATED_MCP_KEYWORDS = (
    "feishu_create_doc",
    "feishu_edit_body",
    "feishu_set_title",
    "feishu_comment",
)

AGENT_MESSAGE = (
    "【记忆沙箱】本轮还没查过记忆就要动手了，已拦下这一次。"
    "请先调用 memory_prepare（query 用用户原话），看记忆里有没有现成的模板、规范、口径或"
    "踩坑结论——这类东西常常已经记过，凭空写容易跑偏。"
    "查完再重试刚才这个操作即可；确认无关也会放行（同一轮只拦一次）。"
)

USER_MESSAGE = "记忆沙箱：本轮还没查记忆，已拦下这次操作并要求先检索。"


def _conversation_id(data: dict) -> str:
    for key in ("conversation_id", "session_id", "generation_id"):
        val = data.get(key)
        if val:
            return str(val)
    return "default"


def _is_gated(tool: str) -> bool:
    lowered = tool.lower()
    # 记忆沙箱自己的工具永远放行，否则「先查记忆」这条路本身就被堵死了
    if "memory_prepare" in lowered or "memory_ask" in lowered:
        return False
    if lowered in GATED_TOOLS:
        return True
    # MCP 工具名形如 MCP:memory_feishu_create_doc
    return any(kw in lowered for kw in GATED_MCP_KEYWORDS)


def main() -> int:
    allow = json.dumps({"permission": "allow"})
    try:
        data = json.load(sys.stdin)
    except Exception:
        print(allow)
        return 0

    try:
        tool = str(data.get("tool_name") or "")
        if not _is_gated(tool):
            print(allow)
            return 0

        conv = _conversation_id(data)
        if (STATE_DIR / f"{conv}.prepared").is_file():
            print(allow)
            return 0

        # 已经拦过一次就放行，避免 MCP 不可用时把整轮卡死
        gated = STATE_DIR / f"{conv}.gated"
        if gated.is_file():
            print(allow)
            return 0

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        gated.write_text("1", encoding="utf-8")
    except Exception:
        # 门禁自身出问题时必须放行，不能挡住正常工作流
        print(allow)
        return 0

    print(
        json.dumps(
            {
                "permission": "deny",
                "agent_message": AGENT_MESSAGE,
                "user_message": USER_MESSAGE,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
