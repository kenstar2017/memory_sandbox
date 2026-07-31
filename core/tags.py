"""标签规范化与解析（长时记忆多维过滤）。"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence

_TAG_TOKEN_RE = re.compile(r"[#＃]?([A-Za-z0-9_\u4e00-\u9fff][\w\u4e00-\u9fff./-]{0,47})")
_HASH_IN_TEXT_RE = re.compile(r"[#＃]([A-Za-z0-9_\u4e00-\u9fff][\w\u4e00-\u9fff./-]{0,47})")


def normalize_tag(tag: str) -> str:
    """去掉 #、空白，统一小写（中文保持原样大小写无关）。"""
    t = (tag or "").strip()
    if not t:
        return ""
    t = t.lstrip("#＃").strip()
    if not t:
        return ""
    # ASCII 部分小写；整体长度限制
    t = "".join(ch.lower() if "A" <= ch <= "Z" else ch for ch in t)
    return t[:48]


def normalize_tags(tags: Optional[Iterable[str]] = None) -> List[str]:
    """去重保序的规范化标签列表。"""
    if not tags:
        return []
    out: List[str] = []
    seen = set()
    for raw in tags:
        if raw is None:
            continue
        if isinstance(raw, str) and ("," in raw or "，" in raw or " " in raw.strip()):
            parts = re.split(r"[,，\s]+", raw.strip())
        else:
            parts = [str(raw)]
        for p in parts:
            t = normalize_tag(p)
            if not t or t in seen:
                continue
            seen.add(t)
            out.append(t)
    return out[:32]


def parse_tags_from_text(text: str) -> List[str]:
    """从正文中提取 #tag 形式的标签。"""
    if not text:
        return []
    found = _HASH_IN_TEXT_RE.findall(text)
    return normalize_tags(found)


def merge_tags(*groups: Optional[Sequence[str]]) -> List[str]:
    merged: List[str] = []
    for g in groups:
        if g:
            merged.extend(g)
    return normalize_tags(merged)


def tags_match(record_tags: Sequence[str], required: Sequence[str], *, mode: str = "any") -> bool:
    """
    mode=any：命中任一 required 即通过
    mode=all：须包含全部 required
    required 为空：不过滤
    """
    req = normalize_tags(required)
    if not req:
        return True
    have = set(normalize_tags(record_tags))
    if mode == "all":
        return all(t in have for t in req)
    return any(t in have for t in req)
