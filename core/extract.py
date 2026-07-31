"""从终端输出 / diff / 粘贴文本中提炼候选记忆（需人工确认后写入）。"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import List, Optional, Sequence

from .tags import normalize_tags

_CMD_RE = re.compile(
    r"(?m)^(?:\$\s*|❯\s*|>\s*)?"
    r"((?:pnpm|npm|yarn|bun|pip|pip3|uv|poetry|cargo|go|make|cmake|"
    r"docker|kubectl|git|curl|wget|python3?|node|npx|brew|ssh|"
    r"\./scripts/[\w./-]+|scripts/[\w./-]+)"
    r"[^\n]{0,200})$"
)
_PATH_RE = re.compile(
    r"(?m)((?:~/|\.{1,2}/|/(?:Users|home|var|tmp|opt|usr)/|"
    r"(?:core|src|scripts|docs|tests|apps?)/)[\w./@+-]{3,160})"
)
_ENV_RE = re.compile(
    r"(?m)^([A-Z][A-Z0-9_]{1,64})=(.+)$"
)
_PITFALL_RE = re.compile(
    r"(?im)^(.*(?:error|exception|failed|失败|报错|踩坑|坑：|注意[:：]|warning).{0,200})$"
)
_DECISION_RE = re.compile(
    r"(?im)^(.*(?:决定|选用|改用|采用|结论[:：]|最终方案|prefer|switch to).{0,200})$"
)

# 明显噪声
_NOISE_ENV = {
    "PATH", "HOME", "PWD", "SHELL", "USER", "LANG", "TERM", "TMPDIR",
    "OLDPWD", "SHLVL", "EDITOR", "VISUAL", "_",
}


@dataclass
class MemoryCandidate:
    question: str
    answer: str
    kind: str = "qa"
    tags: List[str] = field(default_factory=list)
    facts: dict = field(default_factory=dict)
    confidence: float = 0.5
    source_line: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


def _dedupe(cands: Sequence[MemoryCandidate]) -> List[MemoryCandidate]:
    seen = set()
    out: List[MemoryCandidate] = []
    for c in cands:
        key = (c.kind, c.question.strip().lower(), c.answer.strip().lower()[:120])
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return out


def extract_memory_candidates(
    text: str,
    *,
    max_n: int = 3,
    tags: Optional[Sequence[str]] = None,
) -> List[MemoryCandidate]:
    """
    启发式提炼 1~max_n 条候选。
    不写盘；调用方确认后再 memory_remember。
    """
    if not (text or "").strip():
        return []

    base_tags = normalize_tags(tags)
    cands: List[MemoryCandidate] = []

    for m in _CMD_RE.finditer(text):
        cmd = m.group(1).strip()
        if len(cmd) < 3:
            continue
        cands.append(
            MemoryCandidate(
                question=f"命令：{cmd.split()[0]}",
                answer=cmd,
                kind="command",
                tags=list(base_tags) or ["command"],
                facts={"command": cmd},
                confidence=0.72,
                source_line=cmd,
            )
        )

    for m in _ENV_RE.finditer(text):
        key, val = m.group(1), m.group(2).strip().strip("'\"")
        if key in _NOISE_ENV or len(val) < 1:
            continue
        # 疑似密钥：仍提炼，但答案侧留给 scrub
        cands.append(
            MemoryCandidate(
                question=f"环境变量 {key}",
                answer=f"{key}={val}",
                kind="env",
                tags=list(base_tags) or ["env"],
                facts={"env": f"{key}={val}"},
                confidence=0.65,
                source_line=m.group(0)[:200],
            )
        )

    for m in _PATH_RE.finditer(text):
        path = m.group(1).rstrip(".,;:)")
        if path.count("/") < 1 and not path.startswith("~"):
            continue
        # 过短/纯扩展名跳过
        if len(path) < 5:
            continue
        cands.append(
            MemoryCandidate(
                question=f"路径：{path.split('/')[-1]}",
                answer=path,
                kind="path",
                tags=list(base_tags) or ["path"],
                facts={"path": path},
                confidence=0.55,
                source_line=path,
            )
        )

    for m in _PITFALL_RE.finditer(text):
        line = m.group(1).strip()
        if len(line) < 8:
            continue
        cands.append(
            MemoryCandidate(
                question="踩坑/报错要点",
                answer=line[:500],
                kind="pitfall",
                tags=list(base_tags) or ["pitfall"],
                facts={"pitfall": line[:500]},
                confidence=0.6,
                source_line=line[:200],
            )
        )

    for m in _DECISION_RE.finditer(text):
        line = m.group(1).strip()
        if len(line) < 6:
            continue
        cands.append(
            MemoryCandidate(
                question="方案决策",
                answer=line[:500],
                kind="decision",
                tags=list(base_tags) or ["decision"],
                facts={"decision": line[:500]},
                confidence=0.58,
                source_line=line[:200],
            )
        )

    # 置信度降序，同类优先保留高分
    cands.sort(key=lambda c: c.confidence, reverse=True)
    cands = _dedupe(cands)

    # 多样性：尽量覆盖不同 kind
    picked: List[MemoryCandidate] = []
    seen_kinds = set()
    for c in cands:
        if c.kind not in seen_kinds or len(picked) < max_n:
            if len(picked) >= max_n:
                break
            picked.append(c)
            seen_kinds.add(c.kind)
    return picked[:max_n]


def suggest_tags_from_text(text: str, limit: int = 5) -> List[str]:
    """从文本粗提建议标签（供 Web/MCP 提示）。"""
    from .tags import parse_tags_from_text

    tags = list(parse_tags_from_text(text))
    lower = (text or "").lower()
    heuristics = [
        ("feishu", ("飞书", "feishu", "lark")),
        ("frontend", ("frontend", "前端", "react", "vue", "pnpm")),
        ("backend", ("backend", "后端", "fastapi", "django")),
        ("bugfix", ("bugfix", "报错", "error", "修复")),
        ("devops", ("docker", "k8s", "kubectl", "ci")),
        ("git", ("git ", "pull request", "merge")),
    ]
    for tag, keys in heuristics:
        if any(k in lower for k in keys) and tag not in tags:
            tags.append(tag)
    return tags[:limit]
