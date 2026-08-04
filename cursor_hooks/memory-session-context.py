#!/usr/bin/env python3
"""sessionStart：把记忆沙箱调用协议注入每个会话的初始系统上下文。

为什么用 sessionStart：这是文档里唯一端到端可用的注入点。
beforeSubmitPrompt 只有 continue / user_message，返回 additional_context 会被静默忽略
（日志里看着像成功）；postToolUse 的 additional_context 目前也到不了模型。

作用等价于「所有项目都挂上了 memory-sandbox 规则」，不必再去 Customize → Rules 手填。
"""
from __future__ import annotations

import json
import sys

CONTEXT = """【记忆沙箱调用协议 · 本机全局】
本机装了记忆沙箱 MCP（user-memory-sandbox），它存着跨项目的模板、口径、约定与踩坑结论。

1. 动手之前先查记忆：调用 memory_prepare，query 用用户原话。写文档、写方案、改代码、
   排障之前都要查——模板和规范往往记忆里已经有了，凭空写会跑偏。
2. 返回的 references / context_pack 必须纳入本轮推理，但以当前仓库/现状为准，不要整段照抄。
3. 不要因为 hit_local=true 就短路结束；改代码、查代码、排障仍要读代码再给结论。
4. 结束前必须 memory_remember 固化本轮可复用结论：排障根因与修复步骤、代码改动与关键路径、
   需求字段模型与口径、调研与评审结论、方案取舍。question 用精简问法或用户原话
   （不要写「帮我看看」），answer 写清原因 + 做法 + 关键路径/命令，scene=dev。
5. 飞书文档：写操作（新建/改正文/改标题/加评论）必须先在对话里说清改哪一篇、改成什么，
   取得用户本轮明确同意后才能传 confirmed=true；上一轮批准过不算本次的确认。
   写成功会自动落一条只含链接与大纲的操作记录，那不等于记下了结论，结论仍要单独 remember。
6. 永远不要把密钥 / token / 密码写入记忆。
7. 纯寒暄、纯记忆管理指令（备份/清空/查状态/切场景）可跳过以上。"""


def main() -> int:
    # 注入失败也绝不能影响会话启动
    try:
        sys.stdin.read()
    except Exception:
        pass
    print(json.dumps({"additional_context": CONTEXT}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
