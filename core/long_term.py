"""长时记忆层：陈述性记忆（向量+结构化）+ 程序性记忆（规则表）。"""

from __future__ import annotations

import json
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .embedding import LocalHasherEmbedder
from .question_optimize import extract_core, optimize_question
from .utils import clean_text, cosine_similarity, extract_keywords, keyword_overlap


@dataclass
class MemoryRecord:
    id: str
    question: str
    answer: str
    keywords: List[str]
    vector: List[float]
    scene: str = "general"
    weight: float = 1.0
    hit_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)


class LongTermMemory:
    """
    陈述性记忆：本地 JSON 向量库（可选后续换 Chroma）
    程序性记忆：本地 procedural.json 规则表
    """

    def __init__(
        self,
        persist_dir: str = "data/memory",
        similarity_threshold: float = 0.70,
        top_k: int = 3,
        reinforce_boost: float = 0.05,
        embedder: Optional[LocalHasherEmbedder] = None,
    ):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.similarity_threshold = similarity_threshold
        self.top_k = top_k
        self.reinforce_boost = reinforce_boost
        self.embedder = embedder or LocalHasherEmbedder()

        self.declarative_path = self.persist_dir / "declarative.json"
        self.procedural_path = self.persist_dir / "procedural.json"

        self.records: List[MemoryRecord] = []
        self.procedural: Dict[str, str] = {}
        self._load()

    # ---------- persistence ----------
    def reload(self) -> None:
        """从磁盘重新加载（多进程：App / MCP / CLI 共用同一记忆文件时必需）。"""
        self._load(declarative_only=False)

    def _load(self, declarative_only: bool = False) -> None:
        if self.declarative_path.is_file():
            with open(self.declarative_path, "r", encoding="utf-8") as f:
                raw = json.load(f) or []
            self.records = [MemoryRecord(**item) for item in raw]
        else:
            self.records = []

        if declarative_only:
            return

        if self.procedural_path.is_file():
            with open(self.procedural_path, "r", encoding="utf-8") as f:
                self.procedural = json.load(f) or {}
        else:
            self.procedural = {
                "翻译模板": "请将以下内容翻译为{lang}：\n{text}",
                "代码解释模板": "请用简洁中文解释以下代码的作用与关键点：\n{code}",
                "报错排查模板": "报错信息：{error}\n上下文：{context}\n请给出可能原因与排查步骤。",
            }
            self._save_procedural()

    def _save_declarative(self) -> None:
        data = [asdict(r) for r in self.records]
        with open(self.declarative_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save_procedural(self) -> None:
        with open(self.procedural_path, "w", encoding="utf-8") as f:
            json.dump(self.procedural, f, ensure_ascii=False, indent=2)

    # ---------- procedural ----------
    def get_procedure(self, name: str) -> Optional[str]:
        return self.procedural.get(name)

    def set_procedure(self, name: str, template: str) -> None:
        self.procedural[name] = template
        self._save_procedural()

    def match_procedure(self, text: str) -> Optional[str]:
        """简单关键词命中程序性模板名。"""
        t = text.strip().lower()
        for name, tpl in self.procedural.items():
            if name.lower() in t or t in name.lower():
                return f"[程序性记忆:{name}]\n{tpl}"
        return None

    # ---------- declarative search / save ----------
    def search(
        self,
        query: str,
        query_vec: Optional[List[float]] = None,
        scene: Optional[str] = None,
        threshold: Optional[float] = None,
        top_k: Optional[int] = None,
    ) -> Optional[str]:
        """
        向量 + 关键词混合检索。
        命中则返回整合后的答案文本；未命中返回 None。
        """
        hits = self.search_hits(query, query_vec=query_vec, scene=scene, threshold=threshold, top_k=top_k)
        if not hits:
            return None

        # 强化命中记忆
        for rec, score in hits:
            rec.hit_count += 1
            rec.weight = min(2.0, rec.weight + self.reinforce_boost)
            rec.updated_at = time.time()
        self._save_declarative()

        if len(hits) == 1:
            rec, score = hits[0]
            return rec.answer

        # 多条拼接
        parts = []
        for i, (rec, score) in enumerate(hits, 1):
            parts.append(f"[{i}] (相似度 {score:.2f}) {rec.answer}")
        return "\n".join(parts)

    def _record_aliases(self, rec: MemoryRecord) -> List[str]:
        meta = rec.meta or {}
        aliases = list(meta.get("aliases") or [])
        if rec.question and rec.question not in aliases:
            aliases.insert(0, rec.question)
        # 旧数据无 aliases：运行时补核心词
        core = meta.get("canonical_question") or extract_core(rec.question)
        if core and core not in aliases:
            aliases.append(core)
        return aliases

    def search_hits(
        self,
        query: str,
        query_vec: Optional[List[float]] = None,
        scene: Optional[str] = None,
        threshold: Optional[float] = None,
        top_k: Optional[int] = None,
    ) -> List[Tuple[MemoryRecord, float]]:
        # 每次检索前刷新磁盘，避免 MCP 与 App 写后互不可见
        self._load(declarative_only=True)
        thr = self.similarity_threshold if threshold is None else threshold
        k = self.top_k if top_k is None else top_k

        q_raw = clean_text(query)
        q_core = extract_core(q_raw)
        qvec = query_vec or self.embedder.embed(q_raw)
        qvec_core = self.embedder.embed(q_core) if q_core and q_core != q_raw else qvec
        qkw = extract_keywords(q_raw + " " + q_core)
        q_raw_l = q_raw.lower()
        q_core_l = q_core.lower()

        scored: List[Tuple[MemoryRecord, float]] = []
        for rec in self.records:
            vscore = max(
                cosine_similarity(qvec, rec.vector),
                cosine_similarity(qvec_core, rec.vector),
            )
            kscore = keyword_overlap(qkw, rec.keywords)
            # 陈述性：向量为主，关键词为辅
            score = 0.70 * vscore + 0.30 * kscore

            # 别名 / 包含关系加分（口语问法命中核心词）
            aliases = [a.lower() for a in self._record_aliases(rec) if a]
            contain_boost = 0.0
            for alias in aliases:
                if not alias:
                    continue
                if q_raw_l == alias or q_core_l == alias:
                    contain_boost = max(contain_boost, 0.35)
                elif alias in q_raw_l or q_core_l in alias or alias in q_core_l:
                    contain_boost = max(contain_boost, 0.22)
                elif q_raw_l in alias:
                    contain_boost = max(contain_boost, 0.12)
            score = min(1.0, score + contain_boost)

            # 情境依赖：同场景加权
            if scene and rec.scene == scene:
                score = min(1.0, score + 0.05)
            # 记忆强化权重
            score = min(1.0, score * (0.85 + 0.15 * rec.weight))
            if score >= thr:
                scored.append((rec, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]

    def save_memory(
        self,
        question: str,
        answer: str,
        scene: str = "general",
        meta: Optional[dict] = None,
        vector: Optional[List[float]] = None,
    ) -> MemoryRecord:
        """记忆巩固：写入前优化问题，提升后续口语/Cursor 检索命中。"""
        self._load(declarative_only=True)
        opt = optimize_question(question)
        q = opt.canonical
        a = answer.strip()
        opt_meta = opt.as_meta()
        if meta:
            opt_meta.update(meta)

        # 去重：同核心问题 / 高相似则更新
        existing = self.search_hits(opt.original, threshold=0.90, top_k=1)
        if not existing:
            existing = self.search_hits(q, threshold=0.90, top_k=1)
        embed_src = opt.embed_text
        vec = vector or self.embedder.embed(embed_src)
        # 关键词合并答案侧信息
        keywords = list(dict.fromkeys(opt.keywords + extract_keywords(a, top_k=8)))

        if existing:
            rec, _ = existing[0]
            rec.question = q
            rec.answer = a
            rec.keywords = keywords
            rec.vector = vec
            rec.scene = scene or rec.scene
            rec.weight = min(2.0, rec.weight + self.reinforce_boost)
            rec.hit_count += 1
            rec.updated_at = time.time()
            # 合并别名，避免覆盖历史变体
            old_aliases = list((rec.meta or {}).get("aliases") or [])
            merged = list(dict.fromkeys(old_aliases + opt.aliases + [opt.original, q]))
            rec.meta = {**(rec.meta or {}), **opt_meta, "aliases": merged[:32]}
            self._save_declarative()
            return rec

        rec = MemoryRecord(
            id=uuid.uuid4().hex[:12],
            question=q,
            answer=a,
            keywords=keywords,
            vector=vec,
            scene=scene or "general",
            meta=opt_meta,
        )
        self.records.append(rec)
        self._save_declarative()
        return rec

    def forget(self, keyword: Optional[str] = None) -> int:
        self._load(declarative_only=True)
        if keyword is None:
            n = len(self.records)
            self.records = []
            self._save_declarative()
            return n
        needle = keyword.strip().lower()
        before = len(self.records)
        self.records = [
            r for r in self.records
            if needle not in r.question.lower() and needle not in r.answer.lower()
        ]
        removed = before - len(self.records)
        if removed:
            self._save_declarative()
        return removed

    def delete_by_id(self, memory_id: str) -> Optional[MemoryRecord]:
        """按 id 删除单条陈述性记忆。"""
        self._load(declarative_only=True)
        mid = (memory_id or "").strip()
        if not mid:
            return None
        kept = []
        removed = None
        for r in self.records:
            if r.id == mid and removed is None:
                removed = r
            else:
                kept.append(r)
        if removed is None:
            return None
        self.records = kept
        self._save_declarative()
        return removed

    def reoptimize_all(self) -> int:
        """对已有记忆重新跑问题优化（补 aliases / 刷新向量），提升旧数据命中率。"""
        self._load(declarative_only=True)
        n = 0
        for rec in self.records:
            opt = optimize_question(rec.question)
            # 若有历史 original，一并纳入
            original = (rec.meta or {}).get("original_question") or rec.question
            opt2 = optimize_question(original)
            aliases = list(dict.fromkeys(opt.aliases + opt2.aliases + [rec.question, original]))
            rec.question = opt2.canonical or opt.canonical or rec.question
            rec.keywords = list(dict.fromkeys(opt2.keywords + extract_keywords(rec.answer, top_k=8)))
            rec.vector = self.embedder.embed(opt2.embed_text)
            rec.meta = {
                **(rec.meta or {}),
                **opt2.as_meta(),
                "aliases": aliases[:32],
            }
            rec.updated_at = time.time()
            n += 1
        if n:
            self._save_declarative()
        return n

    def backup_dir(self) -> Path:
        path = self.persist_dir / "backups"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def backup_declarative(self, dest: Optional[str] = None) -> Path:
        """
        手动备份陈述性长时记忆。
        默认写到 persist_dir/backups/declarative_YYYYMMDD_HHMMSS.json。
        """
        self._load(declarative_only=True)
        if dest:
            out = Path(dest).expanduser()
            if out.is_dir() or str(dest).endswith(("/", "\\")):
                out.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                out = out / f"declarative_{stamp}.json"
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
        else:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            out = self.backup_dir() / f"declarative_{stamp}.json"

        payload = {
            "version": 1,
            "kind": "declarative_backup",
            "created_at": time.time(),
            "created_at_iso": datetime.now().isoformat(timespec="seconds"),
            "count": len(self.records),
            "records": [asdict(r) for r in self.records],
        }
        with open(out, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        # 同步一份裸 declarative 副本，便于直接替换
        raw_copy = out.with_name(out.stem + "_raw.json")
        if self.declarative_path.is_file():
            shutil.copy2(self.declarative_path, raw_copy)
        else:
            raw_copy.write_text("[]", encoding="utf-8")
        return out

    def list_backups(self) -> List[Path]:
        d = self.backup_dir()
        files = sorted(d.glob("declarative_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        # 排除 *_raw.json
        return [p for p in files if not p.name.endswith("_raw.json")]

    def restore_declarative(self, path: Optional[str] = None) -> int:
        """从备份恢复陈述性记忆。path 为空则用最新一份备份。"""
        if path:
            src = Path(path).expanduser()
        else:
            backups = self.list_backups()
            if not backups:
                raise FileNotFoundError("没有可用的长时记忆备份")
            src = backups[0]
        if not src.is_file():
            raise FileNotFoundError(f"备份文件不存在：{src}")

        with open(src, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "records" in data:
            records = data["records"] or []
        elif isinstance(data, list):
            records = data
        else:
            raise ValueError("无法识别的备份格式")

        self.records = [MemoryRecord(**item) for item in records]
        self._save_declarative()
        return len(self.records)

    def archive_low_access(self, min_hits: int = 0, older_than_days: float = 30) -> int:
        """低访问记忆清理（遗忘机制）：可先归档再删除。"""
        self._load(declarative_only=True)
        now = time.time()
        keep = []
        archived = []
        for r in self.records:
            age_days = (now - r.updated_at) / 86400.0
            if r.hit_count <= min_hits and age_days >= older_than_days:
                archived.append(asdict(r))
            else:
                keep.append(r)
        if not archived:
            return 0
        archive_path = self.persist_dir / "declarative_archive.jsonl"
        with open(archive_path, "a", encoding="utf-8") as f:
            for item in archived:
                # 归档时去掉向量，显著缩小体积
                item.pop("vector", None)
                f.write(json.dumps(item, ensure_ascii=False) + "\n")
        self.records = keep
        self._save_declarative()
        return len(archived)

    def stats(self) -> dict:
        return {
            "declarative_count": len(self.records),
            "procedural_count": len(self.procedural),
            "threshold": self.similarity_threshold,
            "top_k": self.top_k,
            "persist_dir": str(self.persist_dir),
        }
