"""飞书文档入库时：用标题 + 用户意图重写「问」。"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional, Sequence

from .feishu import FeishuFetchResult, extract_feishu_urls
from .utils import clean_text

_URL_STRIP_RE = re.compile(
    r"https?://[^\s<>\"']+?(?:larkoffice|feishu|larksuite)\.(?:com|cn)/"
    r"(?:wiki|docx|docs|doc)/[A-Za-z0-9_-]+",
    re.IGNORECASE,
)

# 用户粘贴链后常见套话（弱意图）
_WEAK_PHRASES = [
    r"读取记录该(?:前端)?技术文档",
    r"该文档为.+?技术文档",
    r"这是另一个关于.+?的(?:前端)?技术文档",
    r"这是(?:一篇|一个)?关于.+?的(?:前端)?技术文档",
    r"另一个关于.+?的(?:前端)?技术文档",
    r"读取(?:并)?(?:记录|总结|归纳)?(?:一下|下)?",
    r"帮我(?:看|读|总结|整理)(?:一下|下)?",
    r"请(?:帮我)?(?:总结|整理|归纳|读取)",
    r"同样整理归纳技术细节",
    r"为后续开发迭代做为技术储备",
    r"为后续开发迭代作为技术储备",
    r"整理归纳技术细节",
    r"做技术储备",
    r"^飞书文档[：:]\s*",
]

_INTENT_KEEP = re.compile(
    r"(技术细节|技术储备|前端|后端|客服|架构|接入|工单|IM|迭代|总结|要点|方案|踩坑|配置|部署)"
)


def strip_feishu_urls(text: str) -> str:
    t = _URL_STRIP_RE.sub(" ", text or "")
    t = re.sub(r"\s+", " ", t).strip(" ，,。.;；、")
    return t


def _title_from_content(content: str) -> str:
    """正文首行作标题兜底（API 未返回 title 时）。"""
    for line in (content or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if 2 <= len(line) <= 80 and not line.startswith("http"):
            return line
    return ""


def _title_from_answer(answer: str) -> str:
    if not answer:
        return ""
    for pat in (
        r"###\s*飞书文档[：:]\s*(.+)",
        r"《([^》]{2,80})》",
        r"标题[：:]\s*(.+)",
        r"文档标题[：:]\s*(.+)",
    ):
        m = re.search(pat, answer)
        if m:
            t = clean_text(m.group(1)).strip(" ：:#")
            if t and not t.startswith("http"):
                return t[:80]
    return ""


def resolve_doc_title(result: FeishuFetchResult) -> str:
    title = (result.title or "").strip()
    if title and title != (getattr(result, "document_id", "") or "") and not re.fullmatch(
        r"[A-Za-z0-9_-]{10,}", title
    ):
        return title
    # token 误填为 title 时改用正文首行
    fallback = _title_from_content(result.content or "")
    return fallback or title or "飞书文档"


def _compress_intent(intent: str, title: str) -> str:
    t = clean_text(intent)
    for p in _WEAK_PHRASES:
        t = re.sub(p, " ", t)
    t = re.sub(r"\s+", " ", t).strip(" ，,。.;；、")
    # 去掉与标题重复的片段
    if title and title in t:
        t = t.replace(title, " ").strip(" ，,")
    if not t:
        return ""
    # 若仍很长，优先保留关键语义词拼一句
    if len(t) > 36:
        keys = _INTENT_KEEP.findall(t)
        if keys:
            # 保序去重
            seen = []
            for k in keys:
                if k not in seen:
                    seen.append(k)
            t = "、".join(seen[:6])
        else:
            t = t[:36].rstrip("，, ")
    return t


def is_real_doc_title(title: str) -> bool:
    """是否像真实飞书文档标题（非 token / 非占位）。"""
    t = (title or "").strip()
    if not t or t in {"飞书文档", "飞书文档（读取失败）"}:
        return False
    if re.fullmatch(r"[A-Za-z0-9_-]{10,}", t):
        return False
    return True


def _is_real_title(title: str) -> bool:
    return is_real_doc_title(title)


def _current_bracket_title(question: str) -> str:
    m = re.match(r"《([^》]{1,80})》", (question or "").strip())
    return (m.group(1) or "").strip() if m else ""


def bracket_title_of(question: str) -> str:
    return _current_bracket_title(question)


def _already_rewritten(question: str, *, better_title: str = "") -> bool:
    """
    仅当已是「《真实标题》…」且没有更好标题时，才视为已优化。
    「飞书文档：口语…」是占位写法，不算最终问法（避免挡住标题重写）。
    """
    q = (question or "").strip()
    bracket = _current_bracket_title(q)
    if bracket and _is_real_title(bracket):
        if _is_real_title(better_title) and better_title != bracket:
            return False
        return True
    return False


def rewrite_feishu_memory_question(
    user_question: str,
    docs: Optional[Sequence[FeishuFetchResult]] = None,
    *,
    answer: str = "",
    title: str = "",
    force: bool = False,
) -> str:
    """
    将「URL + 口语指令」改成便于检索的问法：
    《文档标题》意图摘要 https://.../wiki/token
    （末尾保留链接，供飞书 token 硬过滤命中）
    """
    original = clean_text(user_question or "")
    refs = extract_feishu_urls(original)
    if not refs and docs:
        refs = extract_feishu_urls(" ".join(d.url for d in docs if getattr(d, "url", None)))
    # 答案/结构化字段里也可能只有链接
    if not refs:
        refs = extract_feishu_urls(answer or "")
    if not refs:
        return original

    ok_docs: List[FeishuFetchResult] = [d for d in (docs or []) if getattr(d, "ok", False)]
    titles: List[str] = []
    urls: List[str] = []
    if ok_docs:
        for d in ok_docs:
            titles.append(resolve_doc_title(d))
            urls.append(d.url or "")
    else:
        t = (title or "").strip() or _title_from_answer(answer)
        # 问里已有《真实标题》时优先保留，避免 force 重写时冲掉
        bracket = _current_bracket_title(original)
        if not _is_real_title(t) and _is_real_title(bracket):
            t = bracket
        titles = [t or "飞书文档"]
        urls = [refs[0].url]

    primary_title = titles[0] if titles else "飞书文档"
    if not force and _already_rewritten(original, better_title=primary_title):
        if extract_feishu_urls(original):
            return original
        return f"{original} {refs[0].url}".strip()
    # 已是《真实标题》且没有更好标题：即使 force 也不降级成「飞书文档：」
    if (
        force
        and _is_real_title(_current_bracket_title(original))
        and not _is_real_title(primary_title)
    ):
        if extract_feishu_urls(original):
            return original
        return f"{original} {refs[0].url}".strip()

    # 意图：剥 URL，再剥「飞书文档：」占位前缀与套话
    intent = strip_feishu_urls(original)
    intent = re.sub(r"^飞书文档[：:]\s*", "", intent).strip()
    # 若当前已是《旧标题》形式，意图只取书名号后的部分
    intent = re.sub(r"^《[^》]+》\s*", "", intent).strip()
    intent_bit = _compress_intent(intent, primary_title if _is_real_title(primary_title) else "")

    if len(titles) == 1:
        if not _is_real_title(primary_title):
            # 仍无标题：尽量用语义词，避免把整句口语塞进「飞书文档：」
            body = intent_bit or "飞书文档技术要点"
            if not body.startswith("《") and not body.startswith("飞书"):
                body = f"飞书文档：{body}"
        else:
            head = f"《{primary_title}》"
            body = f"{head}{intent_bit}" if intent_bit else f"{head}技术要点与储备"
    else:
        real = [t for t in titles if _is_real_title(t)]
        joined = "、".join(f"《{t}》" for t in (real or titles)[:3])
        body = f"{joined}{intent_bit or '多文档要点整理'}"

    # 所有相关 URL 挂在问末尾（token 过滤 + 可点回原文）
    url_tail = " ".join(u for u in urls if u) or refs[0].url
    out = f"{body} {url_tail}".strip()
    return out


def ensure_answer_has_feishu_links(answer: str, docs: Iterable[FeishuFetchResult]) -> str:
    """答案里没有链接时补一行，避免只改「问」后丢 token。"""
    a = answer or ""
    missing = []
    for d in docs:
        if not getattr(d, "ok", False):
            continue
        url = (d.url or "").strip()
        if url and url not in a:
            missing.append(url)
    if not missing:
        return a
    block = "链接：\n" + "\n".join(missing)
    if a.strip():
        return f"{block}\n\n{a}"
    return block
