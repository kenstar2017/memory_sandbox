"""长时记忆层：陈述性记忆（向量+结构化）+ 程序性记忆（规则表）。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple

from .bm25 import BM25Index
from .embedding import LocalHasherEmbedder
from .question_optimize import extract_core, optimize_question
from .scrub import scrub_text
from .structure import (
    facts_search_blob,
    infer_kind,
    merge_facts,
    normalize_facts,
    normalize_kind,
)
from .tags import merge_tags, normalize_tags, tags_match
from .utils import clean_text, cosine_similarity, extract_keywords, keyword_overlap

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore


@dataclass
class MemoryRecord:
    id: str
    question: str
    answer: str
    keywords: List[str]
    vector: List[float]
    scene: str = "general"
    tags: List[str] = field(default_factory=list)
    kind: str = "qa"
    facts: Dict[str, str] = field(default_factory=dict)
    weight: float = 1.0
    hit_count: int = 0
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchHit:
    """可解释检索命中。"""

    record: MemoryRecord
    score: float
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "id": self.record.id,
            "question": self.record.question,
            "answer": self.record.answer,
            "scene": self.record.scene,
            "tags": list(self.record.tags or []),
            "kind": self.record.kind or "qa",
            "facts": dict(self.record.facts or {}),
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
            "hit_count": self.record.hit_count,
            "weight": round(self.record.weight, 3),
        }


def _record_from_dict(item: dict) -> MemoryRecord:
    """兼容旧 JSON：忽略未知字段，补全 tags/kind/facts。"""
    known = {f.name for f in fields(MemoryRecord)}
    data = {k: v for k, v in (item or {}).items() if k in known}
    data["tags"] = normalize_tags(data.get("tags") or [])
    data["facts"] = normalize_facts(data.get("facts") or {})
    data["kind"] = normalize_kind(data.get("kind") or infer_kind(data["facts"]))
    if data.get("meta") is None:
        data["meta"] = {}
    if data.get("keywords") is None:
        data["keywords"] = []
    if data.get("vector") is None:
        data["vector"] = []
    return MemoryRecord(**data)


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
        *,
        bm25_enabled: bool = True,
        vector_weight: float = 0.55,
        keyword_weight: float = 0.20,
        bm25_weight: float = 0.25,
        aging_enabled: bool = True,
        aging_days: float = 90.0,
        aging_min_hits: int = 0,
        aging_decay: float = 0.15,
    ):
        self.persist_dir = Path(persist_dir)
        self.persist_dir.mkdir(parents=True, exist_ok=True)
        self.similarity_threshold = similarity_threshold
        self.top_k = top_k
        self.reinforce_boost = reinforce_boost
        self.embedder = embedder or LocalHasherEmbedder()
        self.bm25_enabled = bool(bm25_enabled)
        self.vector_weight = float(vector_weight)
        self.keyword_weight = float(keyword_weight)
        self.bm25_weight = float(bm25_weight)
        self.aging_enabled = bool(aging_enabled)
        self.aging_days = float(aging_days)
        self.aging_min_hits = int(aging_min_hits)
        self.aging_decay = float(aging_decay)

        self.declarative_path = self.persist_dir / "declarative.json"
        self.procedural_path = self.persist_dir / "procedural.json"
        self._declarative_lock_path = self.persist_dir / "declarative.json.lock"
        self._procedural_lock_path = self.persist_dir / "procedural.json.lock"

        self.records: List[MemoryRecord] = []
        self.procedural: Dict[str, str] = {}
        # declarative.json 的 (mtime_ns, size)：未变则跳过重新解析
        self._decl_stamp: Optional[Tuple[int, int]] = None
        # records 文本内容的版本号，BM25 索引据此判断能否复用
        self._records_version: int = 0
        self._bm25_cache: Optional[Tuple[int, BM25Index]] = None
        self._load()

    def _hybrid_weights(self) -> Tuple[float, float, float]:
        vw = max(0.0, self.vector_weight)
        kw = max(0.0, self.keyword_weight)
        bw = max(0.0, self.bm25_weight) if self.bm25_enabled else 0.0
        total = vw + kw + bw
        if total <= 0:
            return 0.70, 0.30, 0.0
        return vw / total, kw / total, bw / total

    def _bm25_index(self) -> BM25Index:
        """按 records 版本复用 BM25 索引；reinforce 只改权重不动文本，无需重建。"""
        cached = self._bm25_cache
        if cached is not None and cached[0] == self._records_version:
            return cached[1]
        index = BM25Index()
        index.rebuild([self._record_doc_text(r) for r in self.records])
        self._bm25_cache = (self._records_version, index)
        return index

    @staticmethod
    def _record_doc_text(rec: MemoryRecord) -> str:
        parts = [
            rec.question or "",
            rec.answer or "",
            " ".join(rec.keywords or []),
            " ".join(rec.tags or []),
            facts_search_blob(rec.facts or {}),
        ]
        return " ".join(p for p in parts if p)

    # ---------- locking / atomic IO ----------
    @contextmanager
    def _file_lock(self, lock_path: Path, exclusive: bool = True) -> Iterator[None]:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(lock_path, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            if fcntl is not None:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            fh.close()

    @staticmethod
    def _atomic_write_json(path: Path, data: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_name, path)
        except Exception:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # ---------- persistence ----------
    def reload(self) -> None:
        """从磁盘重新加载（多进程：App / MCP / CLI 共用同一记忆文件时必需）。"""
        self._load(declarative_only=False)

    def revision(self) -> str:
        """
        declarative.json 的变更标记（mtime_ns:size），不解析文件内容。

        App / MCP / CLI 是三个进程共用同一份记忆文件，原子写会同时改 mtime 与 size，
        前端据此轮询「别处有没有写入」，不必每次把整份记忆拉下来比对。
        """
        stamp = self._decl_stamp_now()
        return "" if stamp is None else f"{stamp[0]}:{stamp[1]}"

    def _decl_stamp_now(self) -> Optional[Tuple[int, int]]:
        """declarative.json 的 (mtime_ns, size)；不存在时 None。"""
        try:
            st = self.declarative_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _load(self, declarative_only: bool = False) -> None:
        with self._file_lock(self._declarative_lock_path, exclusive=False):
            stamp = self._decl_stamp_now()
            # 原子写会更新 mtime/size，据此判断别的进程有没有改过盘
            if stamp is None:
                if self.records or self._decl_stamp is not None:
                    self.records = []
                    self._records_version += 1
                self._decl_stamp = None
            elif stamp != self._decl_stamp:
                with open(self.declarative_path, "r", encoding="utf-8") as f:
                    raw = json.load(f) or []
                self.records = [_record_from_dict(item) for item in raw]
                self._decl_stamp = stamp
                self._records_version += 1

        if declarative_only:
            return

        need_seed_procedural = False
        with self._file_lock(self._procedural_lock_path, exclusive=False):
            if self.procedural_path.is_file():
                with open(self.procedural_path, "r", encoding="utf-8") as f:
                    self.procedural = json.load(f) or {}
            else:
                self.procedural = {
                    "翻译模板": "请将以下内容翻译为{lang}：\n{text}",
                    "代码解释模板": "请用简洁中文解释以下代码的作用与关键点：\n{code}",
                    "报错排查模板": "报错信息：{error}\n上下文：{context}\n请给出可能原因与排查步骤。",
                }
                need_seed_procedural = True
        if need_seed_procedural:
            self._save_procedural()

    def _save_declarative(self) -> None:
        data = [asdict(r) for r in self.records]
        with self._file_lock(self._declarative_lock_path, exclusive=True):
            self._atomic_write_json(self.declarative_path, data)
            self._decl_stamp = self._decl_stamp_now()
            self._records_version += 1

    def _save_procedural(self) -> None:
        with self._file_lock(self._procedural_lock_path, exclusive=True):
            self._atomic_write_json(self.procedural_path, self.procedural)

    def _mutate_declarative(self, mutator) -> Any:
        """加排他锁：重载 → 修改 → 原子写，避免 MCP/Web/CLI 丢写。"""
        with self._file_lock(self._declarative_lock_path, exclusive=True):
            if self.declarative_path.is_file():
                with open(self.declarative_path, "r", encoding="utf-8") as f:
                    raw = json.load(f) or []
                self.records = [_record_from_dict(item) for item in raw]
            else:
                self.records = []
            result = mutator()
            self._atomic_write_json(self.declarative_path, [asdict(r) for r in self.records])
            self._decl_stamp = self._decl_stamp_now()
            self._records_version += 1
            return result

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
        tags: Optional[Sequence[str]] = None,
        kind: Optional[str] = None,
        threshold: Optional[float] = None,
        top_k: Optional[int] = None,
        *,
        reinforce: bool = True,
    ) -> Optional[str]:
        """
        向量 + 关键词混合检索。
        命中则返回整合后的答案文本；未命中返回 None。
        多条命中时带分数，避免冲突时假装单一答案。
        """
        hits = self.search_hits(
            query,
            query_vec=query_vec,
            scene=scene,
            tags=tags,
            kind=kind,
            threshold=threshold,
            top_k=top_k,
        )
        if not hits:
            return None

        if reinforce:
            self.reinforce_hits(hits)

        return self.format_hit_answers(hits)

    def reinforce_hits(self, hits: Sequence[SearchHit]) -> None:
        ids = {h.record.id for h in hits}

        def _mut() -> None:
            now = time.time()
            for rec in self.records:
                if rec.id in ids:
                    rec.hit_count += 1
                    rec.weight = min(2.0, rec.weight + self.reinforce_boost)
                    rec.updated_at = now

        self._mutate_declarative(_mut)
        # 同步 hits 内对象（可能是旧引用）
        by_id = {r.id: r for r in self.records}
        for h in hits:
            if h.record.id in by_id:
                h.record = by_id[h.record.id]

    @staticmethod
    def format_hit_answers(hits: Sequence[SearchHit]) -> str:
        if len(hits) == 1:
            return hits[0].record.answer
        parts = []
        for i, hit in enumerate(hits, 1):
            tag_s = f" tags={','.join(hit.record.tags)}" if hit.record.tags else ""
            reason_s = f"；{'/'.join(hit.reasons[:3])}" if hit.reasons else ""
            parts.append(
                f"[{i}] (相似度 {hit.score:.2f}{tag_s}{reason_s}) {hit.record.answer}"
            )
        return "\n".join(parts)

    def soft_threshold(self, hard_threshold: Optional[float] = None) -> float:
        """软召回阈值：相对硬命中阈值低 0.25，下限 0.35。"""
        hard = self.similarity_threshold if hard_threshold is None else float(hard_threshold)
        return max(0.35, hard - 0.25)

    def collect_references(
        self,
        query: str,
        *,
        query_vec: Optional[List[float]] = None,
        scene: Optional[str] = None,
        tags: Optional[Sequence[str]] = None,
        kind: Optional[str] = None,
        top_k: int = 5,
        threshold: Optional[float] = None,
        tag_mode: str = "any",
    ) -> List[SearchHit]:
        """
        软召回相关问答（供 Cursor 等外部 Agent 作参考，不必达硬命中阈值）。
        飞书 token 等硬过滤仍走 search_hits。
        """
        thr = self.soft_threshold() if threshold is None else float(threshold)
        k = max(1, min(int(top_k or 5), 20))
        return self.search_hits(
            query,
            query_vec=query_vec,
            scene=scene,
            tags=tags,
            kind=kind,
            threshold=thr,
            top_k=k,
            tag_mode=tag_mode,
        )

    @staticmethod
    def format_context_pack(
        hits: Sequence[SearchHit],
        *,
        max_answer_chars: int = 800,
    ) -> str:
        """拼给外部 Agent 的参考问答纯文本块。"""
        if not hits:
            return ""
        blocks: List[str] = [
            "【记忆沙箱 · 参考问答】以下为相关历史结论，供结合当前项目上下文使用；"
            "可能过时，以仓库/现状为准，勿直接当作最终实现。"
            "若某条与现状矛盾，用它的 id 调 memory_update 修正或 memory_delete 删除，"
            "别留着误导后续检索。"
        ]
        for i, hit in enumerate(hits, 1):
            rec = hit.record
            ans = (rec.answer or "").strip()
            if max_answer_chars > 0 and len(ans) > max_answer_chars:
                ans = ans[:max_answer_chars].rstrip() + "…"
            tags = ",".join(rec.tags or [])
            tag_s = f" tags={tags}" if tags else ""
            reason_s = "/".join(hit.reasons[:4]) if hit.reasons else ""
            # id 必须给：hook 投递的是纯文本，没有 id 就没法回头修这条
            meta = f"id={rec.id} score={hit.score:.2f}{tag_s}"
            if reason_s:
                meta += f" reasons={reason_s}"
            blocks.append(
                f"### 参考问答 {i}\n"
                f"问：{rec.question}\n"
                f"答：{ans}\n"
                f"（{meta}）"
            )
        return "\n\n".join(blocks)

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
        tags: Optional[Sequence[str]] = None,
        kind: Optional[str] = None,
        threshold: Optional[float] = None,
        top_k: Optional[int] = None,
        tag_mode: str = "any",
    ) -> List[SearchHit]:
        # 每次检索前刷新磁盘，避免 MCP 与 App 写后互不可见
        self._load(declarative_only=True)
        thr = self.similarity_threshold if threshold is None else threshold
        k = self.top_k if top_k is None else top_k
        required_tags = normalize_tags(tags)
        want_kind = normalize_kind(kind) if (kind or "").strip() else None

        q_raw = clean_text(query)
        q_core = extract_core(q_raw)
        qvec = query_vec or self.embedder.embed(q_raw)
        qvec_core = self.embedder.embed(q_core) if q_core and q_core != q_raw else qvec
        qkw = extract_keywords(q_raw + " " + q_core)
        q_raw_l = q_raw.lower()
        q_core_l = q_core.lower()
        vw, kw_w, bw = self._hybrid_weights()

        # 查询含飞书链接时：只允许 token 对得上的记忆命中
        from .feishu import extract_feishu_tokens, record_matches_feishu_tokens

        required_feishu_tokens = extract_feishu_tokens(query)

        # BM25：索引随记忆版本缓存，避免每次检索都重建
        bm25_norm = [0.0] * len(self.records)
        if bw > 0 and self.records:
            raw_bm25 = self._bm25_index().score(q_raw + " " + q_core)
            mx_b = max(raw_bm25) if raw_bm25 else 0.0
            if mx_b > 0:
                bm25_norm = [s / mx_b for s in raw_bm25]

        now = time.time()
        scored: List[SearchHit] = []
        for ri, rec in enumerate(self.records):
            if required_tags and not tags_match(rec.tags, required_tags, mode=tag_mode):
                continue
            if want_kind is not None and normalize_kind(rec.kind) != want_kind:
                continue
            if required_feishu_tokens:
                fact_blob_early = facts_search_blob(rec.facts or {})
                if not record_matches_feishu_tokens(
                    [
                        rec.question or "",
                        rec.answer or "",
                        fact_blob_early,
                        " ".join(rec.tags or []),
                        " ".join((rec.meta or {}).get("aliases") or []),
                    ],
                    required_feishu_tokens,
                ):
                    continue

            reasons: List[str] = []
            vscore = max(
                cosine_similarity(qvec, rec.vector),
                cosine_similarity(qvec_core, rec.vector),
            )
            fact_blob = facts_search_blob(rec.facts or {})
            rec_kw = list(rec.keywords or []) + extract_keywords(fact_blob, top_k=6)
            kscore = keyword_overlap(qkw, rec_kw)
            bscore = bm25_norm[ri] if ri < len(bm25_norm) else 0.0
            # 向量 + 关键词 + BM25 混合
            score = vw * vscore + kw_w * kscore + bw * bscore
            if vscore >= 0.55:
                reasons.append(f"vector:{vscore:.2f}")
            if kscore >= 0.35:
                reasons.append(f"keywords:{kscore:.2f}")
            if bscore >= 0.35:
                reasons.append(f"bm25:{bscore:.2f}")
            if required_feishu_tokens:
                matched_tok = next(
                    (t for t in required_feishu_tokens if t in (rec.question or "") + (rec.answer or "")),
                    next(iter(required_feishu_tokens)),
                )
                reasons.append(f"feishu_token:{matched_tok[:16]}")

            # 别名 / 包含关系加分（口语问法命中核心词）
            aliases = [a.lower() for a in self._record_aliases(rec) if a]
            contain_boost = 0.0
            matched_alias = ""
            for alias in aliases:
                if not alias:
                    continue
                if q_raw_l == alias or q_core_l == alias:
                    contain_boost = max(contain_boost, 0.35)
                    matched_alias = alias
                elif alias in q_raw_l or q_core_l in alias or alias in q_core_l:
                    if contain_boost < 0.22:
                        matched_alias = alias
                    contain_boost = max(contain_boost, 0.22)
                elif q_raw_l in alias:
                    if contain_boost < 0.12:
                        matched_alias = alias
                    contain_boost = max(contain_boost, 0.12)
            if contain_boost:
                score = min(1.0, score + contain_boost)
                reasons.append(f"alias:{matched_alias[:40]}")

            # 短主题词：整词出现在问句/别名中且 BM25 有信号时再抬一档，
            # 避免长标题技术文档卡在阈值下（0.66 < 0.70）而本地未命中。
            title_l = (rec.question or "").lower()
            short_topic = (
                2 <= len(q_core_l) <= 8
                and bscore >= 0.45
                and (
                    q_core_l in title_l
                    or any(q_core_l in a for a in aliases if a)
                )
            )
            if short_topic:
                # 越短的主题词越依赖字面包含，加分略高
                topic_boost = 0.14 if len(q_core_l) <= 4 else 0.08
                score = min(1.0, score + topic_boost)
                reasons.append("title_topic")

            # facts 文本包含
            if fact_blob and (q_core_l in fact_blob.lower() or any(w in fact_blob.lower() for w in qkw[:4])):
                score = min(1.0, score + 0.08)
                reasons.append(f"facts:{rec.kind or 'qa'}")

            # 情境依赖：同场景加权
            if scene and rec.scene == scene:
                score = min(1.0, score + 0.05)
                reasons.append(f"scene:{rec.scene}")

            # 标签加权：查询带 tag 且记录匹配
            if required_tags:
                overlap = [t for t in required_tags if t in set(normalize_tags(rec.tags))]
                if overlap:
                    score = min(1.0, score + 0.08 * min(3, len(overlap)))
                    reasons.append("tag:" + ",".join(overlap[:4]))

            if rec.kind and rec.kind != "qa":
                # 查询里提到类型词时轻微加权
                if rec.kind in q_raw_l or (rec.kind == "command" and "命令" in q_raw):
                    score = min(1.0, score + 0.04)
                    reasons.append(f"kind:{rec.kind}")

            # 老化降权：久未更新且命中少
            if self.aging_enabled and self.aging_decay > 0:
                age_days = (now - float(rec.updated_at or now)) / 86400.0
                if rec.hit_count <= self.aging_min_hits and age_days >= self.aging_days:
                    span = max(self.aging_days, 1.0)
                    factor = min(1.0, (age_days - self.aging_days) / span)
                    penalty = self.aging_decay * factor
                    score = max(0.0, score * (1.0 - penalty))
                    reasons.append(f"aging:-{penalty:.2f}")

            # 记忆强化权重
            score = min(1.0, score * (0.85 + 0.15 * rec.weight))
            if score >= thr:
                if not reasons:
                    reasons.append(f"score:{score:.2f}")
                scored.append(SearchHit(record=rec, score=score, reasons=reasons))

        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:k]

    def _record_facet(self, rec: MemoryRecord) -> str:
        return str((rec.meta or {}).get("facet") or "")

    def _find_by_feishu_tokens(
        self, *texts: str, facet: str = ""
    ) -> Optional[MemoryRecord]:
        """
        正文/链接含相同飞书 wiki/docx token 的记忆视为同一文档，合并更新。

        facet 区分同一篇文档的不同侧面（如正文 vs 评论）：只有同一侧面才合并，
        否则一条评论会把整篇正文的记录覆盖掉。
        """
        from .feishu import extract_feishu_tokens

        tokens: Set[str] = set()
        for t in texts:
            tokens |= extract_feishu_tokens(t or "")
        if not tokens:
            return None
        best: Optional[MemoryRecord] = None
        for rec in self.records:
            if self._record_facet(rec) != facet:
                continue
            blob = "\n".join(
                [
                    rec.question or "",
                    rec.answer or "",
                    facts_search_blob(rec.facts or {}),
                    " ".join(rec.tags or []),
                    " ".join((rec.meta or {}).get("aliases") or []),
                ]
            )
            if not any(tok in blob for tok in tokens):
                continue
            if best is None or float(rec.updated_at or 0) >= float(best.updated_at or 0):
                best = rec
        return best

    def _find_by_question_key(self, *texts: str) -> Optional[MemoryRecord]:
        """按原问法 / 当前问法 / 别名精确匹配（改问前定位原条目）。"""
        needles: Set[str] = set()
        for t in texts:
            c = clean_text(t or "")
            if c:
                needles.add(c.lower())
        if not needles:
            return None
        best: Optional[MemoryRecord] = None
        for rec in self.records:
            keys = [rec.question or ""]
            keys.extend((rec.meta or {}).get("aliases") or [])
            keys.append((rec.meta or {}).get("original_question") or "")
            hit = False
            for k in keys:
                ck = clean_text(k).lower()
                if ck and ck in needles:
                    hit = True
                    break
            if not hit:
                continue
            if best is None or float(rec.updated_at or 0) >= float(best.updated_at or 0):
                best = rec
        return best

    def _find_by_same_answer(self, answer: str) -> Optional[MemoryRecord]:
        """答案完全相同则视为改问更新（避免同答多问）。"""
        a = clean_text(answer or "")
        if not a or len(a) < 8:
            return None
        best: Optional[MemoryRecord] = None
        for rec in self.records:
            if clean_text(rec.answer or "") != a:
                continue
            if best is None or float(rec.updated_at or 0) >= float(best.updated_at or 0):
                best = rec
        return best

    def save_memory(
        self,
        question: str,
        answer: str,
        scene: str = "general",
        meta: Optional[dict] = None,
        vector: Optional[List[float]] = None,
        tags: Optional[Sequence[str]] = None,
        kind: Optional[str] = None,
        facts: Optional[dict] = None,
        *,
        scrub: bool = True,
        record_id: Optional[str] = None,
        original_question: Optional[str] = None,
        require_existing: bool = False,
        dedup_facet: str = "",
    ) -> MemoryRecord:
        """记忆巩固：写入前优化问题，提升后续口语/Cursor 检索命中。

        record_id：指定时原地更新该条（改「问」不会新开一条）。
        original_question：改问前的旧问法，用于定位原条目。
        require_existing：为 True 时找不到原条目则报错，绝不新建。
        dedup_facet：同一来源的不同侧面（如飞书文档的正文 vs 评论）。只在同侧面内
            去重合并，避免评论覆盖正文记录；留空即历史行为。
        """
        q_in, a_in = question, answer
        scrub_meta: Dict[str, Any] = {}
        if scrub:
            sq = scrub_text(question)
            sa = scrub_text(answer)
            q_in, a_in = sq.text, sa.text
            if sq.redacted or sa.redacted:
                scrub_meta["scrubbed"] = True
                scrub_meta["scrub_kinds"] = list(
                    dict.fromkeys((sq.kinds or []) + (sa.kinds or []))
                )

        opt = optimize_question(q_in)
        q = opt.canonical
        a = a_in.strip()
        opt_meta = opt.as_meta()
        if meta:
            opt_meta.update(meta)
        if original_question and clean_text(original_question) != clean_text(q_in):
            opt_meta.setdefault("original_question", clean_text(original_question))
        if dedup_facet:
            opt_meta["facet"] = dedup_facet
        opt_meta.update(scrub_meta)
        new_tags = normalize_tags(tags)
        new_facts = normalize_facts(facts)
        # facts 里若仍含密钥，再 scrub 一遍
        if scrub and new_facts:
            cleaned = {}
            for fk, fv in new_facts.items():
                cleaned[fk] = scrub_text(fv).text
            new_facts = cleaned
        new_kind = infer_kind(new_facts, kind)

        # 去重：显式 id > 旧问法/别名 > 同飞书 token > 同答案 > 高相似问答
        fact_blob = facts_search_blob(new_facts)

        def _facet_ok(rec: Optional[MemoryRecord]) -> bool:
            """跨侧面不合并（评论不该盖掉正文），显式指定 id 时不受此限。"""
            return rec is not None and self._record_facet(rec) == dedup_facet

        existing_id: Optional[str] = None
        rid = (record_id or "").strip()
        if rid:
            for rec in self.records:
                if rec.id == rid:
                    existing_id = rid
                    break
            if existing_id is None and require_existing:
                raise ValueError(f"未找到要更新的记忆 id={rid}")
        if existing_id is None:
            keyed = self._find_by_question_key(
                original_question or "", opt.original, q_in, q
            )
            existing_id = keyed.id if _facet_ok(keyed) else None
        if existing_id is None:
            feishu_dup = self._find_by_feishu_tokens(
                opt.original, q, a, fact_blob, " ".join(new_tags), facet=dedup_facet
            )
            existing_id = feishu_dup.id if feishu_dup else None
        # 同答案合并 / 软相似：仅在显式更新时启用（有 id / update_only）
        editing = bool(rid or require_existing)
        if existing_id is None and editing:
            same_ans = self._find_by_same_answer(a)
            existing_id = same_ans.id if _facet_ok(same_ans) else None
        if existing_id is None:
            existing = self.search_hits(opt.original, threshold=0.90, top_k=1)
            if not existing:
                existing = self.search_hits(q, threshold=0.90, top_k=1)
            # 改问场景：答案相同且相似度尚可时也合并
            if not existing and editing and a:
                soft = self.search_hits(
                    original_question or opt.original, threshold=0.55, top_k=3
                )
                for h in soft:
                    if clean_text(h.record.answer or "") == clean_text(a):
                        existing = [h]
                        break
            existing = [h for h in existing if _facet_ok(h.record)]
            existing_id = existing[0].record.id if existing else None

        if require_existing and not existing_id:
            raise ValueError("未找到要更新的原记忆（请从「已记住」点开再保存）")

        embed_src = opt.embed_text
        if new_facts:
            embed_src = (embed_src + " " + fact_blob).strip()
        vec = vector or self.embedder.embed(embed_src)
        keywords = list(
            dict.fromkeys(
                opt.keywords
                + extract_keywords(a, top_k=8)
                + extract_keywords(fact_blob, top_k=6)
            )
        )

        def _mut() -> MemoryRecord:
            nonlocal existing_id
            # 锁内再找一遍，防并发双写
            hit = None
            if existing_id:
                for rec in self.records:
                    if rec.id == existing_id:
                        hit = rec
                        break
            if hit is None:
                keyed = self._find_by_question_key(
                    original_question or "", opt.original, q_in, q
                )
                hit = keyed if _facet_ok(keyed) else None
            if hit is None:
                hit = self._find_by_feishu_tokens(
                    opt.original, q, a, fact_blob, " ".join(new_tags), facet=dedup_facet
                )
            if hit is None and editing:
                same = self._find_by_same_answer(a)
                hit = same if _facet_ok(same) else None
            if hit is None:
                # 回退：同问题精确匹配
                for rec in self.records:
                    if rec.question == q and _facet_ok(rec):
                        hit = rec
                        break

            if hit is not None:
                hit.question = q
                hit.answer = a
                hit.keywords = keywords
                hit.vector = vec
                hit.scene = scene or hit.scene
                hit.tags = merge_tags(hit.tags, new_tags)
                hit.facts = merge_facts(hit.facts, new_facts)
                hit.kind = infer_kind(hit.facts, new_kind if kind else hit.kind)
                hit.weight = min(2.0, hit.weight + self.reinforce_boost)
                hit.hit_count += 1
                hit.updated_at = time.time()
                old_aliases = list((hit.meta or {}).get("aliases") or [])
                merged = list(dict.fromkeys(old_aliases + opt.aliases + [opt.original, q]))
                hit.meta = {**(hit.meta or {}), **opt_meta, "aliases": merged[:32]}
                return hit

            rec = MemoryRecord(
                id=uuid.uuid4().hex[:12],
                question=q,
                answer=a,
                keywords=keywords,
                vector=vec,
                scene=scene or "general",
                tags=new_tags,
                kind=new_kind,
                facts=new_facts,
                meta=opt_meta,
            )
            self.records.append(rec)
            return rec

        return self._mutate_declarative(_mut)

    def forget(self, keyword: Optional[str] = None) -> int:
        def _mut() -> int:
            if keyword is None:
                n = len(self.records)
                self.records = []
                return n
            needle = keyword.strip().lower()
            before = len(self.records)
            self.records = [
                r
                for r in self.records
                if needle not in r.question.lower()
                and needle not in r.answer.lower()
                and needle not in " ".join(r.tags).lower()
            ]
            return before - len(self.records)

        return self._mutate_declarative(_mut)

    def delete_by_id(self, memory_id: str) -> Optional[MemoryRecord]:
        """按 id 删除单条陈述性记忆。"""
        mid = (memory_id or "").strip()
        if not mid:
            return None

        removed_box: Dict[str, Any] = {"rec": None}

        def _mut() -> Optional[MemoryRecord]:
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
            removed_box["rec"] = removed
            return removed

        return self._mutate_declarative(_mut)

    def reoptimize_all(self) -> int:
        """
        对已有记忆重新跑问题优化（补 aliases / 刷新向量），提升旧数据命中率。

        问法只在**看得出是被砍出来的前缀**时才用原文重算——老版本的
        optimize_question 会把「…要改什么」削成「…要改」，这里正好能补回来。
        其它情况一律沿用现有问法：飞书那类《标题》式问法是特意重写过的，
        拿 original_question 覆盖等于把它退回成粗糙的原始输入。
        """

        def _mut() -> int:
            n = 0
            for rec in self.records:
                original = (rec.meta or {}).get("original_question") or rec.question
                truncated = (
                    original != rec.question
                    and rec.question
                    and original.startswith(rec.question)
                )
                source = original if truncated else rec.question
                opt = optimize_question(source)
                aliases = list(
                    dict.fromkeys(opt.aliases + [rec.question, original, source])
                )
                rec.question = opt.canonical or rec.question
                rec.keywords = list(
                    dict.fromkeys(opt.keywords + extract_keywords(rec.answer, top_k=8))
                )
                rec.vector = self.embedder.embed(opt.embed_text)
                rec.tags = normalize_tags(rec.tags)
                rec.meta = {
                    **(rec.meta or {}),
                    **opt.as_meta(),
                    "original_question": original,
                    "aliases": [a for a in aliases if a][:32],
                }
                rec.updated_at = time.time()
                n += 1
            return n

        return self._mutate_declarative(_mut)

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
        self._atomic_write_json(out, payload)
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

        parsed = [_record_from_dict(item) for item in records]

        def _mut() -> int:
            self.records = parsed
            return len(self.records)

        return self._mutate_declarative(_mut)

    def archive_low_access(
        self,
        min_hits: Optional[int] = None,
        older_than_days: Optional[float] = None,
    ) -> int:
        """低访问记忆清理（遗忘机制）：归档到 jsonl 后从主库移除。"""
        min_hits = self.aging_min_hits if min_hits is None else int(min_hits)
        older_than_days = self.aging_days if older_than_days is None else float(older_than_days)
        now = time.time()
        archived_box: Dict[str, Any] = {"items": []}

        def _mut() -> int:
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
            archived_box["items"] = archived
            self.records = keep
            return len(archived)

        n = self._mutate_declarative(_mut)
        if n:
            archive_path = self.persist_dir / "declarative_archive.jsonl"
            with open(archive_path, "a", encoding="utf-8") as f:
                for item in archived_box["items"]:
                    item.pop("vector", None)
                    f.write(json.dumps(item, ensure_ascii=False) + "\n")
        return n

    def import_pack_records(
        self,
        pack_records: Sequence[dict],
        *,
        merge: bool = True,
        default_scene: str = "dev",
    ) -> Dict[str, int]:
        """导入知识包条目：默认合并去重；merge=False 时先清空再导入。"""
        if not merge:

            def _clear() -> int:
                n = len(self.records)
                self.records = []
                return n

            self._mutate_declarative(_clear)

        imported = 0
        for item in pack_records or []:
            q = (item.get("question") or "").strip()
            a = (item.get("answer") or "").strip()
            if not q or not a:
                continue
            self.save_memory(
                q,
                a,
                scene=(item.get("scene") or default_scene),
                tags=item.get("tags"),
                kind=item.get("kind"),
                facts=item.get("facts"),
                meta={"from_pack": True, **(item.get("meta") or {})},
            )
            imported += 1
        return {"imported": imported, "total": len(self.records)}

    def stats(self) -> dict:
        tag_set = sorted({t for r in self.records for t in (r.tags or [])})
        vw, kw, bw = self._hybrid_weights()
        return {
            "declarative_count": len(self.records),
            "procedural_count": len(self.procedural),
            "threshold": self.similarity_threshold,
            "top_k": self.top_k,
            "persist_dir": str(self.persist_dir),
            "tag_count": len(tag_set),
            "tags": tag_set[:50],
            "bm25_enabled": self.bm25_enabled,
            "hybrid_weights": {
                "vector": round(vw, 3),
                "keyword": round(kw, 3),
                "bm25": round(bw, 3),
            },
            "aging": {
                "enabled": self.aging_enabled,
                "days": self.aging_days,
                "min_hits": self.aging_min_hits,
                "decay": self.aging_decay,
            },
        }
