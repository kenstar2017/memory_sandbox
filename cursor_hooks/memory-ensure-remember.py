#!/usr/bin/env python3
"""stop：本轮没落库就追问一次，强制把可复用结论写进长时记忆。

用户级 hook：对所有项目生效。只靠规则文件提醒不可靠——AI 会忘，
尤其是「读需求 → 调研代码 → 写技术文档」这种长流程，做完就结束了。

marker 由 memory-mark.py 在 memory_remember 成功后写下，
这里检查完就删掉：marker 表示「距上次 stop 之间落过库」，所以多轮对话里
每一轮都会独立判断。

读侧标记（.prepared / .gated）也在这里按轮清掉，这样「每轮都要先查记忆」，
而不是一个会话里查一次就永久放行。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

STATE_DIR = Path.home() / ".cursor" / "memory-sandbox-hook-state"

# 子 agent 用的是它自己的 conversation_id，其 stop 未必经过这里，标记会留下来；
# 单个文件只有 1 字节，定期清掉过期的即可
_STALE_AFTER = 7 * 24 * 3600

FOLLOWUP = (
    "【记忆沙箱·落库兜底】本轮尚未调用 memory_remember。"
    "若本轮产生了可复用结论，现在必须写入长时记忆：\n"
    "- 需求/方案：字段模型与约束、口径、取舍与最终结论\n"
    "- 排障：现象、根因、修复步骤、关键路径与命令\n"
    "- 代码改动：改了什么、为什么、关键文件与函数\n"
    "- 调研/评审：调研结论、评审发现的问题与处理结论\n"
    "写法：question 用精简问法或用户原话（不要写「帮我看看」）；"
    "answer 写清原因 + 做法 + 关键路径/命令；scene=dev，可带 tags。\n"
    "注意：飞书写操作自动落的那条只有文档链接、大纲和一小段摘录，"
    "**不等于**记下了本轮结论，仍需单独 memory_remember。\n"
    "不要写入密钥 / token / 密码。\n"
    "若本轮确实无可复用结论（纯寒暄、纯记忆管理指令、用户说过不要记住），"
    "回一句说明即可，不必强行写入。"
)


def _conversation_id(data: dict) -> str:
    for key in ("conversation_id", "session_id", "generation_id"):
        val = data.get(key)
        if val:
            return str(val)
    return "default"


def _reset_turn_state(conv: str) -> None:
    """清掉本轮的读侧标记，下一轮必须重新查记忆。"""
    for suffix in ("prepared", "gated"):
        try:
            (STATE_DIR / f"{conv}.{suffix}").unlink()
        except OSError:
            pass


def _prune_stale() -> None:
    cutoff = time.time() - _STALE_AFTER
    try:
        entries = list(STATE_DIR.iterdir())
    except OSError:
        return
    for path in entries:
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
        except OSError:
            pass


def _done(conv: str) -> int:
    """本轮真的结束了：清掉读侧标记，顺手清理过期状态文件。"""
    _reset_turn_state(conv)
    _prune_stale()
    print("{}")
    return 0


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except Exception:
        print("{}")
        return 0

    conv = _conversation_id(data)

    # 只在正常完成时追问；loop_count>0 说明这轮就是被追问出来的，不能再追，否则死循环
    if (data.get("status") or "completed") != "completed":
        return _done(conv)
    try:
        loop_count = int(data.get("loop_count") or 0)
    except (TypeError, ValueError):
        loop_count = 0
    if loop_count > 0:
        return _done(conv)

    marker = STATE_DIR / f"{conv}.remembered"
    try:
        if marker.is_file():
            marker.unlink()
            return _done(conv)
    except OSError:
        # 读不了状态就别乱追问，宁可漏一次
        print("{}")
        return 0

    # 要追问，说明这轮还没结束：读侧标记留着，别让补落库的那一轮又被门禁拦一次
    print(json.dumps({"followup_message": FOLLOWUP}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
