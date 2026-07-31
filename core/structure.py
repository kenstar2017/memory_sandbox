"""长时记忆结构化类型与 facts 字段。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

# 问答仍是主载体；kind 标明知识形态
STRUCTURE_KINDS = (
    "qa",
    "command",
    "path",
    "env",
    "pitfall",
    "decision",
)

# facts 允许的键（与 kind 对应的补充字段）
FACT_KEYS = ("command", "path", "env", "pitfall", "decision")


def normalize_kind(kind: Optional[str] = None) -> str:
    k = (kind or "qa").strip().lower()
    if k not in STRUCTURE_KINDS:
        return "qa"
    return k


def normalize_facts(facts: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    if not facts:
        return {}
    out: Dict[str, str] = {}
    for key in FACT_KEYS:
        val = facts.get(key)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            out[key] = text[:2000]
    return out


def infer_kind(facts: Dict[str, str], explicit: Optional[str] = None) -> str:
    if explicit:
        return normalize_kind(explicit)
    for key in FACT_KEYS:
        if facts.get(key):
            return key
    return "qa"


def merge_facts(
    old: Optional[Dict[str, str]] = None,
    new: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    merged = dict(normalize_facts(old))
    merged.update(normalize_facts(new))
    return merged


def facts_search_blob(facts: Dict[str, str]) -> str:
    """拼进关键词/检索文本。"""
    parts = [f"{k}:{v}" for k, v in sorted((facts or {}).items()) if v]
    return " ".join(parts)


def format_facts_line(facts: Dict[str, str], kind: str = "qa") -> str:
    if not facts:
        return ""
    bits: List[str] = []
    order = [kind] if kind in facts else []
    order += [k for k in FACT_KEYS if k in facts and k not in order]
    for k in order:
        bits.append(f"{k}={facts[k]}")
    return "; ".join(bits)
