"""把飞书文档链接收成可确认的记忆候选（依赖已有飞书读文档能力）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .config import FeishuConfig
from .feishu import extract_feishu_urls, feishu_configured, fetch_feishu_docs_for_text
from .scrub import scrub_text


def build_feishu_bookmark_candidates(
    cfg: Optional[FeishuConfig],
    text: str,
    *,
    config_path: Optional[str] = None,
    max_chars: int = 1200,
) -> Dict[str, Any]:
    """
    拉取飞书链接正文，生成待确认记忆候选（不写盘）。
    """
    refs = extract_feishu_urls(text or "")
    if not refs:
        return {
            "candidates": [],
            "hint": "未检测到飞书 wiki/docx 链接。",
        }
    if not feishu_configured(cfg):
        return {
            "candidates": [],
            "urls": [r.url for r in refs],
            "hint": "未配置飞书凭证。请先 feishu.enabled + 登录授权，再重试。",
        }

    results, _ = fetch_feishu_docs_for_text(cfg, text, config_path=config_path)
    cands: List[dict] = []
    for r in results:
        if not r.ok:
            cands.append(
                {
                    "question": f"飞书文档（读取失败）",
                    "answer": f"{r.url}\n失败：{r.error}",
                    "kind": "qa",
                    "tags": ["feishu"],
                    "facts": {},
                    "confidence": 0.2,
                    "error": r.error,
                    "url": r.url,
                }
            )
            continue
        body = scrub_text((r.content or "")[:max_chars]).text
        from .feishu_question import resolve_doc_title, rewrite_feishu_memory_question

        title = resolve_doc_title(r)
        q = rewrite_feishu_memory_question(
            f"{r.url} {title}",
            [r],
            title=title,
        )
        cands.append(
            {
                "question": q,
                "answer": f"链接：{r.url}\n标题：{title}\n\n摘要：\n{body}",
                "kind": "qa",
                "tags": ["feishu", "docs"],
                "facts": {"path": r.url},
                "confidence": 0.7,
                "url": r.url,
                "title": title,
            }
        )
    return {
        "candidates": cands,
        "hint": "请确认后再 memory_remember；正文已做脱敏截断。",
    }
