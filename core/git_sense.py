"""根据 Git 变更，提示可能过时的本地记忆（只读 git，不改仓库）。"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Sequence


@dataclass
class StaleHint:
    memory_id: str
    question: str
    answer_preview: str
    matched_paths: List[str] = field(default_factory=list)
    reason: str = ""
    tags: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class ReviewHint:
    question: str
    answer: str
    source: str = "git_log"
    confidence: float = 0.5

    def as_dict(self) -> dict:
        return asdict(self)


def _run_git(cwd: Path, args: Sequence[str], timeout: float = 8.0) -> str:
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout or ""


def resolve_git_root(cwd: Optional[str] = None) -> Optional[Path]:
    base = Path(cwd or ".").expanduser().resolve()
    out = _run_git(base, ["rev-parse", "--show-toplevel"])
    root = (out or "").strip()
    if not root:
        return None
    path = Path(root)
    return path if path.is_dir() else None


def list_changed_paths(
    cwd: Optional[str] = None,
    *,
    include_uncommitted: bool = True,
    since_ref: str = "HEAD~20",
    max_files: int = 80,
) -> List[str]:
    """列出近期变更文件路径（相对仓库根）。"""
    root = resolve_git_root(cwd)
    if root is None:
        return []
    paths: List[str] = []
    seen = set()

    def _add_lines(text: str) -> None:
        for line in (text or "").splitlines():
            p = line.strip().lstrip("AMDRCU? ")
            # status porcelain: XY path 或 rename
            if " -> " in p:
                p = p.split(" -> ", 1)[-1].strip()
            # 去引号
            p = p.strip().strip('"')
            if not p or p in seen:
                continue
            seen.add(p)
            paths.append(p)

    if include_uncommitted:
        _add_lines(_run_git(root, ["status", "--porcelain"]))
        _add_lines(_run_git(root, ["diff", "--name-only"]))
        _add_lines(_run_git(root, ["diff", "--cached", "--name-only"]))

    # 近期提交触达的文件
    ref = (since_ref or "").strip() or "HEAD~20"
    _add_lines(_run_git(root, ["diff", "--name-only", f"{ref}...HEAD"]))

    return paths[: max(1, max_files)]


def _basename_tokens(path: str) -> List[str]:
    parts = re.split(r"[/\\]", path)
    out = []
    for p in parts:
        if not p or p in {".", ".."}:
            continue
        out.append(p.lower())
        stem = Path(p).stem.lower()
        if stem and stem != p.lower():
            out.append(stem)
    return out


def find_stale_memories(
    records: Sequence[Any],
    changed_paths: Sequence[str],
    *,
    limit: int = 8,
) -> List[StaleHint]:
    """把变更路径与记忆里的 path/正文 做轻量匹配。"""
    if not changed_paths or not records:
        return []

    path_l = [p.replace("\\", "/").lower() for p in changed_paths]
    basenames = set()
    for p in path_l:
        basenames.update(_basename_tokens(p))

    hints: List[StaleHint] = []
    for rec in records:
        q = getattr(rec, "question", "") or ""
        a = getattr(rec, "answer", "") or ""
        tags = list(getattr(rec, "tags", None) or [])
        facts = dict(getattr(rec, "facts", None) or {})
        blob = " ".join(
            [
                q,
                a,
                facts.get("path", ""),
                facts.get("command", ""),
                " ".join(tags),
                " ".join(getattr(rec, "keywords", None) or []),
            ]
        ).lower()
        if not blob.strip():
            continue

        matched = []
        for p, pl in zip(changed_paths, path_l):
            name = Path(pl).name
            stem = Path(pl).stem
            if pl in blob or name in blob or (stem and len(stem) >= 3 and stem in blob):
                matched.append(p)
                continue
            # 目录片段
            for part in pl.split("/"):
                if len(part) >= 4 and part in blob:
                    matched.append(p)
                    break

        if not matched:
            continue
        hints.append(
            StaleHint(
                memory_id=getattr(rec, "id", "") or "",
                question=q,
                answer_preview=(a[:160] + ("…" if len(a) > 160 else "")),
                matched_paths=list(dict.fromkeys(matched))[:6],
                reason="变更文件与记忆正文/路径疑似相关，建议核对是否过时",
                tags=tags,
            )
        )
        if len(hints) >= limit:
            break
    return hints


def suggest_review_habits(
    cwd: Optional[str] = None,
    *,
    max_commits: int = 12,
    max_hints: int = 3,
) -> List[ReviewHint]:
    """从近期 commit message 提炼可沉淀的 Review/协作习惯候选。"""
    root = resolve_git_root(cwd)
    if root is None:
        return []
    n = max(1, min(int(max_commits), 30))
    log = _run_git(
        root,
        ["log", f"-{n}", "--pretty=format:%s"],
    )
    if not log.strip():
        return []

    hints: List[ReviewHint] = []
    patterns = [
        (r"(?i)fix|bugfix|修复", "修 bug 时记得写清根因与复现"),
        (r"(?i)refactor|重构", "重构提交建议附带兼容说明"),
        (r"(?i)test|单测", "补测试的改动可记成项目测试约定"),
        (r"(?i)docs?|文档", "文档变更可沉淀到项目知识包"),
        (r"(?i)feat|新增|支持", "新功能上线路径/开关值得记一条"),
        (r"禁止|不要|避免|务必|必须", "约束类 commit 可固化为团队约定"),
    ]
    seen = set()
    for line in log.splitlines():
        msg = line.strip()
        if len(msg) < 4:
            continue
        for pat, tip in patterns:
            if re.search(pat, msg):
                key = (tip, msg[:80])
                if key in seen:
                    continue
                seen.add(key)
                hints.append(
                    ReviewHint(
                        question=f"协作习惯：{tip}",
                        answer=f"近期提交：「{msg}」。建议记成团队约定或踩坑说明。",
                        source="git_log",
                        confidence=0.55,
                    )
                )
                break
        if len(hints) >= max_hints:
            break
    return hints
