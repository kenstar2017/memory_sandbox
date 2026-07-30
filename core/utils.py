"""通用工具：关键词提取、文本清洗、相似度等。"""

from __future__ import annotations

import math
import re
import uuid
from typing import Iterable, List, Sequence


_GARBAGE_RE = re.compile(
    r"^[\s\u0000-\u001f\ufffd]+$"
    r"|^[^\w\u4e00-\u9fff]{1,}$",
    re.UNICODE,
)

# 中英简单分词：连续中文 / 英文单词 / 数字
_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_./:@#-]+")

# 停用词（极简）
_STOPWORDS = {
    "的", "了", "是", "在", "我", "你", "他", "她", "它", "们",
    "和", "与", "或", "及", "等", "啊", "呢", "吧", "吗", "么",
    "这", "那", "有", "没", "不", "也", "就", "都", "而", "被",
    "把", "让", "给", "对", "从", "到", "为", "以", "一个", "一下",
    "the", "a", "an", "is", "are", "was", "were", "be", "to", "of",
    "and", "or", "in", "on", "at", "for", "with", "by", "as", "it",
}


def new_msg_id() -> str:
    return uuid.uuid4().hex[:12]


def clean_text(content: str) -> str:
    if content is None:
        return ""
    # 终端粘贴/错误解码可能带 lone surrogates，utf-8 严格编码会崩
    text = content.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text


def is_garbage(text: str) -> bool:
    if not text:
        return True
    if _GARBAGE_RE.match(text):
        return True
    # 有效字符过少
    useful = re.findall(r"[\w\u4e00-\u9fff]", text)
    return len(useful) < 1


def tokenize(text: str) -> List[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    out = [t for t in tokens if t and t not in _STOPWORDS]
    # 连续中文补 bigram，避免「组件」被拆成单字导致检索弱
    chars = [c for c in text.lower() if "\u4e00" <= c <= "\u9fff"]
    for i in range(len(chars) - 1):
        bg = chars[i] + chars[i + 1]
        if bg not in _STOPWORDS:
            out.append(bg)
    return out


def extract_keywords(text: str, top_k: int = 12) -> List[str]:
    """简易关键词：按词频排序，保留有意义 token。"""
    tokens = tokenize(text)
    if not tokens:
        return []
    freq: dict = {}
    for t in tokens:
        # 单字中文权重略降；词/bigram 更高
        if len(t) >= 2:
            weight = 1.2
        elif "\u4e00" <= t <= "\u9fff":
            weight = 0.5
        else:
            weight = 1.0
        freq[t] = freq.get(t, 0.0) + weight
    ranked = sorted(freq.items(), key=lambda x: (-x[1], x[0]))
    return [w for w, _ in ranked[:top_k]]


def cosine_similarity(a: Sequence[float], b: Sequence[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def keyword_overlap(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


# Cursor 对话默认追加的长时记忆标记（与 MCP memory_prepare / 规则文案保持一致）
LONG_TERM_RECORD_MARKER = "记录到长期记忆"
LONG_TERM_RECORD_SUFFIX = "记录到长期记忆。"


def assemble_long_term_query(query: str) -> str:
    """
    将用户问题拼成「xxxx，记录到长期记忆。」。
    若原文已以「记录到长期记忆」结尾则不重复追加。
    """
    text = (query or "").strip()
    if not text:
        return LONG_TERM_RECORD_SUFFIX
    # 已包含标记（允许末尾有/无句号）
    compact = re.sub(r"[。．.!\s]+$", "", text)
    if compact.endswith(LONG_TERM_RECORD_MARKER):
        if text.endswith(("。", ".", "．")):
            return text
        return text + "。"
    # 去掉末尾标点再拼，避免「？，记录…」或双句号
    base = re.sub(r"[，,。．.!？?；;：:\s]+$", "", text)
    return f"{base}，{LONG_TERM_RECORD_SUFFIX}"
