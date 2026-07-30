"""本地轻量 Embedding：字符/词 n-gram 哈希向量，无需下载模型。"""

from __future__ import annotations

import hashlib
import math
import re
from typing import List, Sequence


class LocalHasherEmbedder:
    """
    基于特征哈希的本地向量化器。
    - 同维度、同算法保证输入与历史记忆可匹配
    - 纯 Python，零外部模型依赖，适合本地开发沙箱
    """

    def __init__(self, dim: int = 256):
        if dim < 32:
            raise ValueError("embedding dim must be >= 32")
        self.dim = dim
        self._token_re = re.compile(r"[\u4e00-\u9fff]|[A-Za-z0-9_./:@#-]+")

    def embed(self, text: str) -> List[float]:
        text = (text or "").strip().lower()
        # 防御：特征哈希前去掉非法 surrogate，避免 UnicodeEncodeError
        text = text.encode("utf-8", errors="surrogatepass").decode("utf-8", errors="replace")
        vec = [0.0] * self.dim
        if not text:
            return vec

        features = self._features(text)
        for feat, weight in features:
            idx, sign = self._hash_feature(feat)
            vec[idx] += sign * weight

        # L2 normalize
        norm = math.sqrt(sum(x * x for x in vec))
        if norm > 0:
            vec = [x / norm for x in vec]
        return vec

    def embed_batch(self, texts: Sequence[str]) -> List[List[float]]:
        return [self.embed(t) for t in texts]

    def _features(self, text: str) -> List[tuple]:
        feats: List[tuple] = []
        # 词级
        tokens = self._token_re.findall(text)
        for t in tokens:
            feats.append((f"w:{t}", 1.0))
        # 二元词
        for i in range(len(tokens) - 1):
            feats.append((f"b:{tokens[i]}_{tokens[i + 1]}", 0.8))
        # 中文字符 bigram（提升短句召回）
        chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
        for i in range(len(chars) - 1):
            feats.append((f"c:{chars[i]}{chars[i + 1]}", 0.6))
        # 全长字符 trigram（英文标识符友好）
        compact = re.sub(r"\s+", "", text)
        for i in range(max(0, len(compact) - 2)):
            feats.append((f"t:{compact[i:i + 3]}", 0.4))
        return feats

    def _hash_feature(self, feat: str) -> tuple:
        digest = hashlib.md5(feat.encode("utf-8", errors="surrogatepass")).digest()
        idx = int.from_bytes(digest[:4], "little") % self.dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        return idx, sign
