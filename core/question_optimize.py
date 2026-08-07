"""存储时优化问题：归一化、提取核心词、生成检索别名，提高命中率。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Set

from .utils import clean_text, extract_keywords


# 口语化问句尾巴（位置/路径/怎么找…）
_TAIL_PATTERNS = [
    r"(?:的)?(?:文件)?位置(?:在哪(?:里|儿)?|是什么|是啥)?$",
    r"(?:在)?哪(?:里|儿)?(?:可以)?(?:找到|看|查)?$",
    r"(?:的)?(?:代码)?路径(?:是什么|在哪(?:里|儿)?)?$",
    # 尾巴要有边界：`.*$` 会把「怎么启动并配置代理服务」整条吃掉，只剩个项目名
    r"(?:怎么|如何)(?:找|查|定位|打开|进入|启动|运行|配置|使用)[^，,。；;]{0,4}$",
    # 「为什么」不是「什么」：拆开会留下一个悬空的「为」
    r"(?<!为)(?:是)?什么(?:意思|东西|组件|模块|文件)?$",
    r"(?:的)?(?:入口|目录|地址)(?:在哪(?:里|儿)?|是什么)?$",
    r"(?:在)?哪个(?:目录|文件夹|文件|模块|包)?(?:里|下)?$",
    # 动词必须在，否则任何以「一下」收尾的句子都会被削一刀（「记一下」→「记」）
    r"(?:请)?(?:告诉|问)(?:我|下|一下)?$",
]

# 口语化前缀
_HEAD_PATTERNS = [
    r"^(?:请问|问一下|帮我看下|帮我查下|帮我找下|麻烦问下)",
    r"^(?:我想知道|我想问|我要找)",
    r"^(?:哪里有|哪儿有)",
]

# 从核心词扩展的常见问法模板（便于 Cursor 口语检索）
_ALIAS_TEMPLATES = [
    "{core}",
    "{core}在哪",
    "{core}在哪里",
    "{core}位置",
    "{core}位置在哪",
    "{core}路径",
    "{core}怎么找",
    "{core}是什么",
]


@dataclass
class OptimizedQuestion:
    original: str
    canonical: str
    aliases: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    embed_text: str = ""

    def as_meta(self) -> dict:
        return {
            "original_question": self.original,
            "canonical_question": self.canonical,
            "aliases": self.aliases,
            "optimized": True,
        }


def _strip_heads(text: str) -> str:
    t = text
    for p in _HEAD_PATTERNS:
        t = re.sub(p, "", t, flags=re.IGNORECASE)
    return t.strip()


def _strip_tails(text: str) -> str:
    t = text
    changed = True
    # 多轮剥离，处理「位置在哪里」叠词
    while changed:
        before = t
        for p in _TAIL_PATTERNS:
            t2 = re.sub(p, "", t, flags=re.IGNORECASE).strip()
            if t2:
                t = t2
        changed = t != before
    return t.strip(" ：:？?!.。、,，")


def extract_core(question: str) -> str:
    """提取便于检索的核心短语。"""
    t = clean_text(question)
    t = re.sub(r"[？?!.。！]+$", "", t).strip()
    t = _strip_heads(t)
    t = _strip_tails(t)
    # 去掉残留助词
    t = re.sub(r"^(?:的|了|吗|呢|吧)+|(?:的|了|吗|呢|吧)+$", "", t).strip()
    return t or clean_text(question)


def safe_canonical(original: str) -> str:
    """
    存进库的问法：只清掉口水前缀和句末标点，**绝不截断**。

    标题是用户回头认这条记忆的唯一凭据，砍尾巴看着更「核心」，实际是三重损失：
    「…，为什么」剩「…，为」根本读不懂；「灰度开关的文件位置在哪里」剩「灰度开关」
    丢了这条到底讲什么；「agency 项目怎么启动」剩「agency 项目」还会跟同项目的
    其它记忆撞在一起。而且剥离不幂等——重跑一次「优化已有记忆」就再掉一个字。
    检索侧完全不吃亏：核心词（extract_core）照样进 aliases 和向量文本。
    """
    plain = _strip_heads(re.sub(r"[？?!.。！]+$", "", clean_text(original)).strip())
    if len(re.findall(r"[\w\u4e00-\u9fff]", plain)) < 2:
        return clean_text(original) or original
    return plain


def generate_aliases(original: str, canonical: str, *, core: str = "") -> List[str]:
    aliases: Set[str] = set()
    for item in (original, canonical):
        item = clean_text(item)
        if item:
            aliases.add(item)
            aliases.add(re.sub(r"[？?!.。！]+$", "", item).strip())

    core = core or canonical or extract_core(original)
    if core:
        aliases.add(core)
        for tpl in _ALIAS_TEMPLATES:
            aliases.add(tpl.format(core=core))

    # 英文/数字标识单独保留（如 PK、CreateActivityStage）
    for m in re.finditer(r"[A-Za-z][A-Za-z0-9_-]{1,}", original + " " + canonical):
        aliases.add(m.group(0))
        aliases.add(m.group(0).lower())

    # 清洗空串与过短噪声
    cleaned = []
    for a in aliases:
        a = clean_text(a)
        if not a or len(a) < 2:
            continue
        cleaned.append(a)
    # 去重保序：canonical 优先
    ordered = []
    seen = set()
    for a in [canonical, original] + cleaned:
        key = a.lower()
        if not a or key in seen:
            continue
        seen.add(key)
        ordered.append(a)
    return ordered[:24]


def optimize_question(question: str) -> OptimizedQuestion:
    """
    存储前优化问题：
    - canonical：存进库的问法。只清口水前缀，不截断（见 safe_canonical）
    - aliases：常见变体（含更激进的核心词 extract_core），检索时用于加分
    - embed_text：原文+核心+别名拼接，向量覆盖更广
    """
    original = clean_text(question)
    core = extract_core(original)
    canonical = safe_canonical(original)

    aliases = generate_aliases(original, canonical, core=core)
    # 关键词：核心 + 别名 + 原文
    kw_source = " ".join([canonical, original] + aliases[:8])
    keywords = extract_keywords(kw_source, top_k=20)
    # 向量文本：核心词必须在，否则整句标题会把它稀释掉
    embed_parts = [canonical] + ([core] if core and core != canonical else [])
    for a in aliases:
        if a not in embed_parts:
            embed_parts.append(a)
        if len(embed_parts) >= 8:
            break
    embed_text = " 。 ".join(embed_parts)

    return OptimizedQuestion(
        original=original,
        canonical=canonical,
        aliases=aliases,
        keywords=keywords,
        embed_text=embed_text,
    )
