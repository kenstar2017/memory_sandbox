"""知识包：可分享的记忆子集（不含向量与密钥明文）。"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union

from .scrub import scrub_text
from .structure import normalize_facts, normalize_kind
from .tags import normalize_tags, tags_match

PACK_KIND = "memory_pack"
PACK_VERSION = 1


@dataclass
class MemoryPack:
    name: str
    records: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    description: str = ""
    created_at: float = field(default_factory=time.time)
    created_at_iso: str = ""
    version: int = PACK_VERSION
    kind: str = PACK_KIND

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "kind": self.kind,
            "name": self.name,
            "description": self.description,
            "tags": list(self.tags),
            "created_at": self.created_at,
            "created_at_iso": self.created_at_iso
            or datetime.fromtimestamp(self.created_at).isoformat(timespec="seconds"),
            "count": len(self.records),
            "records": list(self.records),
        }


def _pack_record(rec: Any, *, scrub: bool = True) -> Dict[str, Any]:
    """从 MemoryRecord 或 dict 导出轻量条目。"""
    if hasattr(rec, "question"):
        q = rec.question
        a = rec.answer
        scene = getattr(rec, "scene", "general") or "general"
        tags = list(getattr(rec, "tags", None) or [])
        kind = getattr(rec, "kind", "qa") or "qa"
        facts = dict(getattr(rec, "facts", None) or {})
        keywords = list(getattr(rec, "keywords", None) or [])
        meta = dict(getattr(rec, "meta", None) or {})
        rid = getattr(rec, "id", "") or ""
    else:
        q = rec.get("question", "")
        a = rec.get("answer", "")
        scene = rec.get("scene") or "general"
        tags = list(rec.get("tags") or [])
        kind = rec.get("kind") or "qa"
        facts = dict(rec.get("facts") or {})
        keywords = list(rec.get("keywords") or [])
        meta = dict(rec.get("meta") or {})
        rid = rec.get("id") or ""

    if scrub:
        q = scrub_text(q).text
        a = scrub_text(a).text
        facts = {k: scrub_text(str(v)).text for k, v in facts.items()}

    # 去掉可能含密钥痕迹的 meta 字段
    safe_meta = {
        k: v
        for k, v in meta.items()
        if k in {"aliases", "canonical_question", "original_question", "embed_text"}
    }
    return {
        "id": rid,
        "question": q,
        "answer": a,
        "scene": scene,
        "tags": normalize_tags(tags),
        "kind": normalize_kind(kind),
        "facts": normalize_facts(facts),
        "keywords": keywords[:24],
        "meta": safe_meta,
    }


def build_pack(
    records: Sequence[Any],
    *,
    name: str,
    description: str = "",
    tags: Optional[Sequence[str]] = None,
    filter_tags: Optional[Sequence[str]] = None,
    filter_scene: Optional[str] = None,
    scrub: bool = True,
    limit: int = 500,
) -> MemoryPack:
    want_tags = normalize_tags(filter_tags)
    scene = (filter_scene or "").strip()
    picked: List[Dict[str, Any]] = []
    for rec in records:
        rec_tags = normalize_tags(getattr(rec, "tags", None) or (rec.get("tags") if isinstance(rec, dict) else []))
        rec_scene = getattr(rec, "scene", None) or (rec.get("scene") if isinstance(rec, dict) else "general")
        if want_tags and not tags_match(rec_tags, want_tags, mode="any"):
            continue
        if scene and (rec_scene or "") != scene:
            continue
        picked.append(_pack_record(rec, scrub=scrub))
        if len(picked) >= max(1, limit):
            break
    now = time.time()
    return MemoryPack(
        name=(name or "memory-pack").strip() or "memory-pack",
        description=(description or "").strip(),
        tags=normalize_tags(tags) or want_tags,
        records=picked,
        created_at=now,
        created_at_iso=datetime.fromtimestamp(now).isoformat(timespec="seconds"),
    )


def write_pack(pack: MemoryPack, dest: Union[str, Path]) -> Path:
    out = Path(dest).expanduser()
    if out.is_dir() or str(dest).endswith(("/", "\\")):
        out.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in pack.name)[:48]
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = out / f"pack_{safe}_{stamp}.json"
    else:
        out.parent.mkdir(parents=True, exist_ok=True)
    payload = pack.as_dict()
    with open(out, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    return out


def packs_dir(persist_dir: Union[str, Path]) -> Path:
    path = Path(persist_dir) / "packs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def list_local_packs(persist_dir: Union[str, Path]) -> List[Dict[str, Any]]:
    """列出本地已导出的知识包文件。"""
    d = packs_dir(persist_dir)
    files = sorted(d.glob("pack_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    out: List[Dict[str, Any]] = []
    for p in files[:50]:
        meta: Dict[str, Any] = {
            "path": str(p),
            "name": p.stem,
            "size": p.stat().st_size,
            "mtime": p.stat().st_mtime,
        }
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                meta["name"] = data.get("name") or meta["name"]
                meta["count"] = data.get("count") or len(data.get("records") or [])
                meta["tags"] = data.get("tags") or []
                meta["description"] = data.get("description") or ""
        except (OSError, json.JSONDecodeError):
            pass
        out.append(meta)
    return out


def load_pack(path: Union[str, Path]) -> MemoryPack:
    src = Path(path).expanduser()
    if not src.is_file():
        raise FileNotFoundError(f"知识包不存在：{src}")
    with open(src, "r", encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        records = data
        name = src.stem
        description = ""
        tags: List[str] = []
    elif isinstance(data, dict):
        if data.get("kind") not in (None, PACK_KIND, "declarative_backup"):
            # 仍尝试读取 records
            pass
        records = data.get("records") or []
        name = data.get("name") or src.stem
        description = data.get("description") or ""
        tags = normalize_tags(data.get("tags") or [])
    else:
        raise ValueError("无法识别的知识包格式")
    cleaned = [_pack_record(r, scrub=True) for r in records]
    return MemoryPack(name=name, description=description, tags=tags, records=cleaned)
