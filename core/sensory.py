"""感觉记忆层：瞬时缓冲区，TTL 过期，只做预处理。"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .utils import clean_text, extract_keywords, is_garbage, new_msg_id


@dataclass
class SensoryItem:
    msg_id: str
    text: str
    keywords: List[str]
    ts: float


class SensoryMemory:
    """
    模拟人类感官瞬时接收：
    - 内存字典存储
    - TTL 自动过期
    - 降噪 / 过滤无效内容后向下流转
    """

    def __init__(self, ttl: float = 3.0):
        self.ttl = ttl
        self.cache: Dict[str, SensoryItem] = {}

    def add(self, content: str, msg_id: Optional[str] = None) -> Optional[SensoryItem]:
        clean = clean_text(content)
        if is_garbage(clean):
            return None
        mid = msg_id or new_msg_id()
        item = SensoryItem(
            msg_id=mid,
            text=clean,
            keywords=extract_keywords(clean),
            ts=time.time(),
        )
        self.cache[mid] = item
        return item

    def get_valid_data(self) -> List[SensoryItem]:
        """清理过期数据，返回仍有效的内容。"""
        now = time.time()
        expired = []
        valid = []
        for mid, item in self.cache.items():
            if now - item.ts > self.ttl:
                expired.append(mid)
            else:
                valid.append(item)
        for mid in expired:
            del self.cache[mid]
        return valid

    def get(self, msg_id: str) -> Optional[SensoryItem]:
        self.get_valid_data()
        return self.cache.get(msg_id)

    def clear(self) -> None:
        self.cache.clear()

    def forget(self, predicate_text: Optional[str] = None) -> int:
        """忘记：不传文本清空全部；传文本则删除包含该片段的条目。"""
        if predicate_text is None:
            n = len(self.cache)
            self.cache.clear()
            return n
        needle = predicate_text.strip().lower()
        remove = [
            mid for mid, item in self.cache.items()
            if needle in item.text.lower()
        ]
        for mid in remove:
            del self.cache[mid]
        return len(remove)

    def stats(self) -> dict:
        self.get_valid_data()
        return {"size": len(self.cache), "ttl": self.ttl}
