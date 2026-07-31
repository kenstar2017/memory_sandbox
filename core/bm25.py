"""轻量 BM25（Okapi）检索，纯 Python，无外部依赖。"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

from .utils import tokenize


class BM25Index:
    """对文档列表建内存索引，支持增量重建。"""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: List[List[str]] = []
        self._doc_len: List[int] = []
        self._avgdl: float = 0.0
        self._df: Dict[str, int] = {}
        self._n = 0

    def rebuild(self, documents: Sequence[str]) -> None:
        self._docs = [tokenize(d or "") for d in documents]
        self._doc_len = [len(toks) for toks in self._docs]
        self._n = len(self._docs)
        self._avgdl = (sum(self._doc_len) / self._n) if self._n else 0.0
        df: Dict[str, int] = {}
        for toks in self._docs:
            for t in set(toks):
                df[t] = df.get(t, 0) + 1
        self._df = df

    def score(self, query: str) -> List[float]:
        """返回与文档等长的原始 BM25 分数列表。"""
        if not self._n:
            return []
        q_tokens = tokenize(query or "")
        if not q_tokens:
            return [0.0] * self._n
        scores = [0.0] * self._n
        avgdl = self._avgdl or 1.0
        for qt in q_tokens:
            df = self._df.get(qt, 0)
            if df <= 0:
                continue
            idf = math.log(1.0 + (self._n - df + 0.5) / (df + 0.5))
            for i, toks in enumerate(self._docs):
                if not toks:
                    continue
                tf = toks.count(qt)
                if tf <= 0:
                    continue
                dl = self._doc_len[i] or 1
                denom = tf + self.k1 * (1.0 - self.b + self.b * dl / avgdl)
                scores[i] += idf * (tf * (self.k1 + 1.0) / denom)
        return scores

    def ranked(
        self,
        query: str,
        *,
        top_k: int = 10,
        min_score: float = 0.0,
    ) -> List[Tuple[int, float]]:
        raw = self.score(query)
        if not raw:
            return []
        mx = max(raw) if raw else 0.0
        # 归一化到 0~1，便于与向量分混合
        pairs = []
        for i, s in enumerate(raw):
            norm = (s / mx) if mx > 0 else 0.0
            if norm >= min_score:
                pairs.append((i, norm, s))
        pairs.sort(key=lambda x: x[1], reverse=True)
        return [(i, norm) for i, norm, _ in pairs[:top_k]]
