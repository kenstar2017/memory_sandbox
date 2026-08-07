#!/usr/bin/env python3
"""preToolUse：把预取到的记忆交给模型；没预取到就要求它自己查一次。

起因：agent 直接写技术方案文档，而「前端技术方案模板」早就在记忆里，它没查，写出来跑偏了。
光靠规则提醒不够，读侧也要有门禁。

两种模式，取决于 beforeSubmitPrompt 那一步有没有取到东西：

1. **有预取包**（`<conv>.pack`）：本轮第一个工具调用拦一次，把召回的参考问答放进
   `agent_message` 直接送给模型，然后标记本轮读侧已完成。模型不必自己调 memory_prepare
   就已经拿到知识——这是 hook 唯一能把文本送到模型的官方通道
   （beforeSubmitPrompt 改不了 prompt，sessionStart / postToolUse 的
   additional_context 都有已确认的投递 bug）。
2. **没有预取包**（后端没跑 / 超时）：退回原来的行为，只拦「动手」和「委派」
   （Write / Delete / Task 与飞书写操作），要求它先 memory_prepare。

有预取包时之所以连 Read / Grep 也拦：读只读一半就下判断同样会跑偏，而这一拦是
**有货才拦**——记忆里没有相关内容时 beforeSubmitPrompt 已经直接标记通过，一次都不拦。

同一轮最多拦一次（`.gated` 标记），所以 MCP 挂了或 agent 坚持要做时不会死锁。

子 agent 的工具调用同样会触发本 hook，但用的是它自己新的 conversation_id，
所以子 agent 会走「没有预取包」那条路自己查一次（它的上下文是干净的，本来就该查）。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

STATE_DIR = Path.home() / ".cursor" / "memory-sandbox-hook-state"


def _nested_run() -> bool:
    """
    记忆沙箱自己拉起来的嵌套 agent（`MEMORY_SANDBOX_NESTED=1`）不走门禁。

    机器人已经把召回的参考问答塞进它的上下文了，再拦一次只是让它多调一遍
    memory_prepare、白等几十秒。
    """
    return (os.environ.get("MEMORY_SANDBOX_NESTED") or "").strip().lower() not in (
        "",
        "0",
        "false",
    )

# 改文件、删文件、拉子 agent
GATED_TOOLS = {"write", "delete", "task"}
# 飞书写操作（别人立刻看得见，最不该在没查记忆的情况下做）
GATED_MCP_KEYWORDS = (
    "feishu_create_doc",
    "feishu_edit_body",
    "feishu_set_title",
    "feishu_comment",
)

DELIVER_HEAD = (
    "【记忆沙箱 · 已按用户原话预检索】下面是本机长时记忆里与本轮最相关的历史结论，"
    "已在你开工前取好，请纳入本轮推理：\n\n"
)
DELIVER_TAIL = (
    "\n\n以上是参考，可能过时：以当前仓库与用户上下文为准，不要整段照抄当最终实现。"
    "刚才那个操作已被拦下一次（本轮仅一次），现在直接重试即可，不必再调 memory_prepare。"
    "\n\n**维护记忆是你的职责**：若发现上面某条与仓库现状矛盾、取值/规范已经变了、"
    "或结论已被推翻，用那条的 id（每条括号里的 id=）调 memory_update 改掉它；"
    "整条已经彻底失效就 memory_delete。只在回答里提一句「这条过时了」是不够的——"
    "留在库里会一直误导后续检索。本轮结束前还要 memory_remember 把新结论写回。"
)

DELIVER_USER_MESSAGE = "记忆沙箱：已把预检索到的相关记忆交给 AI（本轮拦一次）。"

ASK_AGENT_MESSAGE = (
    "【记忆沙箱】本轮还没查过记忆就要动手了，已拦下这一次。"
    "请先调用 memory_prepare（query 用用户原话），看记忆里有没有现成的模板、规范、口径或"
    "踩坑结论——这类东西常常已经记过，凭空写容易跑偏。"
    "查完再重试刚才这个操作即可；确认无关也会放行（同一轮只拦一次）。"
)

ASK_USER_MESSAGE = "记忆沙箱：本轮还没查记忆，已拦下这次操作并要求先检索。"


def _conversation_id(data: dict) -> str:
    for key in ("conversation_id", "session_id", "generation_id"):
        val = data.get(key)
        if val:
            return str(val)
    return "default"


def _is_memory_tool(tool: str) -> bool:
    """记忆沙箱自己的工具永远放行，否则「先查记忆」这条路本身就被堵死了。"""
    lowered = tool.lower()
    return "memory_prepare" in lowered or "memory_ask" in lowered


def _is_gated(tool: str) -> bool:
    lowered = tool.lower()
    if lowered in GATED_TOOLS:
        return True
    # MCP 工具名形如 MCP:memory_feishu_create_doc
    return any(kw in lowered for kw in GATED_MCP_KEYWORDS)


def _deny(agent_message: str, user_message: str) -> None:
    print(
        json.dumps(
            {
                "permission": "deny",
                "agent_message": agent_message,
                "user_message": user_message,
            },
            ensure_ascii=False,
        )
    )


def main() -> int:
    allow = json.dumps({"permission": "allow"})
    if _nested_run():
        print(allow)
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        print(allow)
        return 0

    try:
        tool = str(data.get("tool_name") or "")
        if _is_memory_tool(tool):
            print(allow)
            return 0

        conv = _conversation_id(data)
        # 快路径：本轮读侧已完成（agent 查过了，或预取发现无相关记忆，或已投递过）
        if (STATE_DIR / f"{conv}.prepared").is_file():
            print(allow)
            return 0

        # 已经拦过一次就放行，避免 MCP 不可用时把整轮卡死
        gated = STATE_DIR / f"{conv}.gated"
        if gated.is_file():
            print(allow)
            return 0

        pack_path = STATE_DIR / f"{conv}.pack"
        pack = ""
        try:
            pack = pack_path.read_text(encoding="utf-8").strip()
        except OSError:
            pack = ""

        # 没预取到东西时只拦「动手」，读操作照常放行，不影响探索速度
        if not pack and not _is_gated(tool):
            print(allow)
            return 0

        STATE_DIR.mkdir(parents=True, exist_ok=True)
        gated.write_text("1", encoding="utf-8")

        if pack:
            # 投递即视为本轮读侧已完成：知识已经进模型了，没必要再要求它查一遍
            (STATE_DIR / f"{conv}.prepared").write_text("delivered", encoding="utf-8")
            try:
                pack_path.unlink()
            except OSError:
                pass
    except Exception:
        # 门禁自身出问题时必须放行，不能挡住正常工作流
        print(allow)
        return 0

    if pack:
        _deny(DELIVER_HEAD + pack + DELIVER_TAIL, DELIVER_USER_MESSAGE)
    else:
        _deny(ASK_AGENT_MESSAGE, ASK_USER_MESSAGE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
