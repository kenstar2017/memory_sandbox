"""把一篇文档正文切成可检索的小块。

为什么不整篇算一条向量：现在的 embedder 是字符 n-gram 哈希（core/embedding.py），
一篇几千字的文档所有特征叠在一个 256 维向量里会被抹平——什么问题都能沾一点、
什么问题都不准。切成段落级的小块，命中才能精确到「出自哪一节」。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List

# 目标块长与硬上限。600~900 字是权衡：太短会把一个论点腰斩，
# 太长又退化成「整篇一条向量」那个毛病
TARGET_CHARS = 800
MIN_CHARS = 200
MAX_CHARS = 1200
# 块间重叠：结论常骑在两段中间，切口处不留重叠就会两边都召不回
OVERLAP_CHARS = 100

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*\S)\s*$")
# 飞书 raw_content 里的标题没有 # 前缀，但常见「一、」「1.1 」「第 3 章」这类编号行
_NUMBERED_RE = re.compile(
    r"^(?:第[一二三四五六七八九十百]+[章节部分]|"
    r"[一二三四五六七八九十]+[、.．]|"
    r"\d+(?:\.\d+)*[、.．]?)\s*(?P<title>\S.*)$"
)
# 编号后面得跟真正的文字。飞书文档里满是「1031」「07.27」这种孤零零的数字和日期，
# 只看「以数字开头」会把它们全当成小节标题，召回时印出「§ 07.27」这种毫无意义的出处
_WORDY_RE = re.compile(r"[A-Za-z\u4e00-\u9fff]")
_MAX_HEADING_CHARS = 60


@dataclass
class Chunk:
    seq: int
    heading_path: str
    text: str

    @property
    def embed_text(self) -> str:
        """算向量用的文本：标题路径拼在正文前面。

        标题里往往是最关键的检索词（「权限管理」「字段模型」），而正文里可能
        通篇都不再重复它。不带上标题，按小节名去问就召不回这一块。
        """
        return f"{self.heading_path}\n{self.text}" if self.heading_path else self.text


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped or len(stripped) > _MAX_HEADING_CHARS:
        return False
    if _HEADING_RE.match(stripped):
        return True
    # 编号行且不以句末标点收尾，才当标题；「1. 先跑起来，再点保存。」是正文
    m = _NUMBERED_RE.match(stripped)
    if m and not stripped.endswith(("。", "；", "，", ".", ";", ",")):
        return bool(_WORDY_RE.search(m.group("title")))
    return False


def _heading_level(line: str) -> int:
    m = _HEADING_RE.match(line.strip())
    if m:
        return len(m.group(1))
    # 无 # 前缀的编号标题统一按二级处理：与 markdown 混排时不至于吃掉一级标题
    return 2


def _heading_text(line: str) -> str:
    m = _HEADING_RE.match(line.strip())
    return m.group(2) if m else line.strip()


def _tail_overlap(text: str) -> str:
    """取一段尾巴当下一块的开头。尽量从句子边界起，免得切在词中间。"""
    if len(text) <= OVERLAP_CHARS:
        return text
    tail = text[-OVERLAP_CHARS:]
    for sep in ("\n", "。", ". ", "；", "; "):
        pos = tail.find(sep)
        if 0 <= pos < len(tail) - 1:
            return tail[pos + len(sep):]
    return tail


def split_document(content: str, *, title: str = "") -> List[Chunk]:
    """正文 → 块列表。空文档返回空列表。"""
    text = (content or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []

    chunks: List[Chunk] = []
    stack: List[str] = []  # 当前标题路径，按层级
    buf: List[str] = []
    buf_len = 0
    buf_path = title

    def flush(carry_over: bool = False) -> None:
        nonlocal buf, buf_len
        body = "\n".join(buf).strip()
        if body:
            chunks.append(Chunk(seq=len(chunks), heading_path=buf_path, text=body))
            tail = _tail_overlap(body) if carry_over else ""
        else:
            tail = ""
        buf = [tail] if tail else []
        buf_len = len(tail)

    def current_path() -> str:
        parts = ([title] if title else []) + stack
        return " / ".join(p for p in parts if p)

    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if _is_heading(line):
            # 标题自成边界：上一节先落块，免得两节内容混在一起
            flush()
            level = _heading_level(line)
            head = _heading_text(line)
            del stack[level - 1:]
            while len(stack) < level - 1:
                stack.append("")
            stack.append(head)
            buf_path = current_path()
            continue

        if not line.strip() and buf_len >= TARGET_CHARS:
            # 空行是天然切口，攒够了就在这切
            flush(carry_over=True)
            continue

        buf.append(line)
        buf_len += len(line) + 1
        if buf_len >= MAX_CHARS:
            flush(carry_over=True)

    flush()

    return _merge_tiny(chunks)


def _common_path(a: str, b: str) -> str:
    """两个标题路径的共同前缀。

    合并跨小节的碎块后，路径必须退到共同祖先：留着前一块的路径会让召回结果
    指着「权限管理 / 用户身份」，实际内容却还包含隔壁小节，读的人照着去翻原文会扑空。
    """
    if a == b:
        return a
    pa = [p for p in a.split(" / ") if p]
    pb = [p for p in b.split(" / ") if p]
    common: List[str] = []
    for x, y in zip(pa, pb):
        if x != y:
            break
        common.append(x)
    return " / ".join(common)


def _merge_tiny(chunks: List[Chunk]) -> List[Chunk]:
    """把过短的块并进前一块。

    标题密集的文档（大量小节，每节两三行）会切出一堆十几个字的碎块，
    这种块向量噪声大、召回出来也读不出意思。
    """
    merged: List[Chunk] = []
    for ch in chunks:
        if (
            merged
            and len(ch.text) < MIN_CHARS
            and len(merged[-1].text) + len(ch.text) <= MAX_CHARS
        ):
            prev = merged[-1]
            # 被并进来的小节保留自己的标题行，否则合并后就分不出哪句属于哪节了
            leaf = ch.heading_path.split(" / ")[-1] if ch.heading_path != prev.heading_path else ""
            joined = f"{prev.text}\n{leaf}\n{ch.text}" if leaf else f"{prev.text}\n{ch.text}"
            merged[-1] = Chunk(
                seq=prev.seq,
                heading_path=_common_path(prev.heading_path, ch.heading_path),
                text=joined,
            )
            continue
        merged.append(Chunk(seq=len(merged), heading_path=ch.heading_path, text=ch.text))
    return merged
