"""把飞书写操作整理成一条可检索的长时记忆。

问法统一成《标题》…链接，与 feishu_question 改写出来的读取型记忆同构，
这样「那篇 X 文档」既能命中读进来的正文，也能命中自己写过的改动。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import List, Optional

from .feishu_question import _truncate_on_boundary, is_real_doc_title
from .utils import clean_text

ACTION_LABELS = {
    "create": "新建文档",
    "append": "追加正文",
    "replace": "替换正文",
    "title": "改标题",
    "comment": "加评论",
    "board": "新建画板",
}

_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s{0,3}(```|~~~)")

_OUTLINE_MAX = 12
_EXCERPT_LIMIT = 600


@dataclass
class FeishuWriteMemory:
    question: str
    answer: str
    facts: dict


def _outline(content: str) -> List[str]:
    """取 Markdown 标题作大纲；代码块里的 # 是注释不是标题。"""
    out: List[str] = []
    in_fence = False
    for line in (content or "").splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line)
        if m:
            depth = len(m.group(1))
            text = clean_text(m.group(2))
            if text:
                out.append(f"{'  ' * (depth - 1)}- {text}")
        if len(out) >= _OUTLINE_MAX:
            break
    return out


def _excerpt(content: str) -> str:
    """正文摘录：去掉围栏标记与空行，截到 boundary，避免整篇塞进记忆。"""
    lines: List[str] = []
    for line in (content or "").splitlines():
        if _FENCE_RE.match(line):
            continue
        line = line.strip()
        if line:
            lines.append(line)
    body = " ".join(lines)
    if not body:
        return ""
    cut = _truncate_on_boundary(body, _EXCERPT_LIMIT)
    if len(body) > len(cut):
        return cut + "…"
    return cut


def _changed_feishu_side(
    action: str,
    ok: bool,
    document_id: str,
    blocks_written: int,
    blocks_deleted: int,
) -> bool:
    """飞书侧是否真的动过。全都没动（如未确认被拒、token 失效）就不该落库。"""
    if ok:
        return True
    if blocks_written or blocks_deleted:
        return True
    # 建文档、建画板都分两步：壳已经建出来、内容写失败，这个半成品必须留档好去清理
    return action in ("create", "board") and bool(document_id)


def build_write_memory(
    *,
    action: str,
    url: str,
    title: str = "",
    document_id: str = "",
    content: str = "",
    blocks_written: int = 0,
    blocks_deleted: int = 0,
    old_title: str = "",
    ok: bool = True,
    error: str = "",
    now: Optional[str] = None,
) -> Optional[FeishuWriteMemory]:
    """
    生成待写入的问答；飞书侧没有任何改动时返回 None。

    同一篇文档反复编辑时问法保持稳定，交给 save_memory 去重更新，
    以免一篇文档在记忆库里堆成十几条。
    """
    if action not in ACTION_LABELS:
        return None
    if not _changed_feishu_side(action, ok, document_id, blocks_written, blocks_deleted):
        return None

    label = ACTION_LABELS[action]
    doc_title = clean_text(title).strip()
    stamp = now or time.strftime("%Y-%m-%d %H:%M")

    # 评论必须与正文用不同问法：问法相同会被 save_memory 当同一条去重更新，
    # 于是一条评论就把整篇正文的大纲与摘录覆盖掉了
    # 三种问法互不覆盖：问法相同会被 save_memory 当同一条去重更新，
    # 于是一条评论、一个画板就把整篇正文的大纲与摘录顶掉了
    topic = {"comment": "评论记录", "board": "画板记录"}.get(action, "正文与写入记录")
    if is_real_doc_title(doc_title):
        question = f"《{doc_title}》飞书文档{topic} {url}".strip()
    else:
        question = f"飞书文档：{label}记录 {url}".strip()

    lines = [f"经记忆沙箱{label}（{'成功' if ok else '未完成'}）。", ""]
    if is_real_doc_title(doc_title):
        lines.append(f"- 标题：{doc_title}")
    if action == "title" and old_title:
        lines.append(f"- 原标题：{old_title}")
    lines.append(f"- 时间：{stamp}")
    if url:
        lines.append(f"- 链接：{url}")
    if document_id:
        lines.append(f"- document_id：{document_id}")
    if blocks_deleted:
        lines.append(f"- 删除原有块：{blocks_deleted}")
    if blocks_written:
        lines.append(("- 画板节点：" if action == "board" else "- 写入块：") + str(blocks_written))
    if not ok and error:
        lines.append(f"- 未完成原因：{clean_text(error)}")

    if action == "comment":
        # 评论是一段话，没有大纲可言，也不该叫「正文摘录」
        body = clean_text(content).strip()
        if body:
            lines += ["", "评论内容：", _truncate_on_boundary(body, _EXCERPT_LIMIT)]
    else:
        outline = _outline(content)
        if outline:
            lines += ["", "大纲：", *outline]

        excerpt = _excerpt(content)
        if excerpt:
            lines += ["", "正文摘录：", excerpt]

    # facts 只认 FACT_KEYS 白名单（见 core/structure.py），塞别的键会被静默丢掉；
    # document_id 与操作类型已写在 answer 里，不必也放不进 facts
    facts = {"path": url} if url else {}

    return FeishuWriteMemory(
        question=question, answer="\n".join(lines).strip(), facts=facts
    )
