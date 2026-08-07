#!/usr/bin/env python3
"""beforeSubmitPrompt：用户一按发送就先去检索记忆，把召回结果存好待投递。

为什么不在这里直接注入上下文：官方明确说 beforeSubmitPrompt 只认 continue /
user_message，任何改写 prompt 的字段都会被静默丢弃（校验器不报错，日志里看着像成功）。
所以这个 hook 只负责「取」，投递交给 preToolUse 的 agent_message——那是有官方
文档、且实测能把文本送到模型的通道。

放在这里取而不是等 agent 自己调 memory_prepare 的好处：
1. 用的是**用户原话**，比 agent 转述的检索词更贴近意图；
2. 提问那一刻就取完了，投递时没有额外等待；
3. 不依赖 agent「想起来」要查，也不依赖 sessionStart 注入（那个有竞态、会被静默丢）。

检索走本机 HTTP（BloomBox / app_web.py --api-only），只用标准库，所以本脚本
与仓库彻底解耦。后端没在跑就直接放过：门禁会退回「要求 agent 自己查一次」。
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path.home() / ".cursor" / "memory-sandbox-hook-state"

API_BASE = os.environ.get("MEMORY_SANDBOX_API", "http://127.0.0.1:8765")
# 这是阻塞用户发送的路径，宁可放弃预取也不能让人等
TIMEOUT_S = 1.5
# 太短的话（「好」「继续」「嗯」）没有检索价值，还会召回一堆噪音
MIN_QUERY_CHARS = 4
MAX_QUERY_CHARS = 600
# agent_message 里塞的上限，留足余量给门禁自己的说明
MAX_PACK_CHARS = 8000

# 纯记忆管理指令：本来就不需要先检索，别为它多拦一次
SKIP_PREFIXES = ("/", "@")
SKIP_KEYWORDS = (
    "清空工作记忆",
    "备份记忆",
    "记忆状态",
    "切换场景",
)


def _nested_run() -> bool:
    """记忆沙箱自己拉起来的嵌套 agent（`MEMORY_SANDBOX_NESTED=1`）不预取。

    机器人已经检索过一遍并把参考塞进它的上下文了，这里再打一次 /api/prepare
    纯属重复，还会把它自己的 conversation 状态混进来。
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


def _reset_turn(conv: str) -> None:
    """新一轮开始：清掉上一轮的读侧标记。

    这里清比在 stop 里清更准——beforeSubmitPrompt 正好是每轮的起点，
    而 stop 可能因为 followup 循环反复触发。
    """
    for suffix in ("prepared", "gated", "pack"):
        try:
            (STATE_DIR / f"{conv}.{suffix}").unlink()
        except OSError:
            pass


def _should_skip(prompt: str) -> bool:
    if len(prompt) < MIN_QUERY_CHARS:
        return True
    if prompt.startswith(SKIP_PREFIXES):
        return True
    return any(kw in prompt for kw in SKIP_KEYWORDS)


def _fetch_pack(query: str) -> str:
    """向本机记忆沙箱要软召回上下文；失败返回空串。"""
    body = json.dumps({"query": query[:MAX_QUERY_CHARS], "top_k": 5}).encode("utf-8")
    req = urllib.request.Request(
        f"{API_BASE}/api/prepare",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
        payload = json.load(resp)
    return str(payload.get("context_pack") or "")


def main() -> int:
    # continue=true 必须无论如何都打印：这个 hook 卡住就等于卡住用户发消息
    proceed = json.dumps({"continue": True})
    if _nested_run():
        print(proceed)
        return 0
    try:
        data = json.load(sys.stdin)
    except Exception:
        print(proceed)
        return 0

    try:
        conv = _conversation_id(data)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _reset_turn(conv)

        prompt = str(data.get("prompt") or "").strip()
        if _should_skip(prompt):
            print(proceed)
            return 0

        pack = _fetch_pack(prompt)
        if pack.strip():
            (STATE_DIR / f"{conv}.pack").write_text(
                pack[:MAX_PACK_CHARS], encoding="utf-8"
            )
        else:
            # 检索成功但没有相关记忆：本轮读侧就算过了，一次都不用拦
            (STATE_DIR / f"{conv}.prepared").write_text("prefetch", encoding="utf-8")
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        # 后端没起来 / 超时：什么都不做，门禁自会退回「要求 agent 自己查」
        pass
    except Exception:
        pass

    print(proceed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
