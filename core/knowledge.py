"""知识库：整篇文档按小节切块入库，与长时记忆平级的一层存储。

为什么不塞进 declarative.json：那份是「一问一答 + 一条向量」，而一篇飞书文档
几千到几万字，整篇一条向量等于没有向量；几百个文档块还会淹掉侧栏的「已记住」
列表和条数统计。所以单开一层，共用同一个 LocalHasherEmbedder（同维同算法，
向量可比），在软召回阶段与记忆命中汇合。

落盘布局（{persist_dir}/knowledge/）：
  docs.json              每篇一条元数据
  chunks/<doc_id>.json   该篇的块（含向量）。一篇一个文件，删除即删文件，
                         重新拉取即整文件覆盖，不必在一个大数组里做增删
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence

from .embedding import LocalHasherEmbedder
from .knowledge_chunk import Chunk, split_document
from .utils import clean_text, cosine_similarity, extract_keywords

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None  # type: ignore


@dataclass
class KnowledgeDoc:
    """一篇入库的文档。正文不存在这里，存在 chunks/<id>.json。"""

    id: str
    url: str
    title: str
    document_id: str = ""  # 飞书解析出来的真实 docx token，去重主键
    # 指向这篇的其它 token。wiki 链接的 token 与它解析出的 docx document_id 不是
    # 一回事，只记后者的话，下次拿同一个 wiki 链接来查会认不出「已经入过库」，
    # 于是每次补录都把所有 wiki 文档重抓一遍
    source_tokens: List[str] = field(default_factory=list)
    source: str = "feishu"
    origin: str = "manual"  # manual / memory:<记忆id>
    scene: str = "general"
    tags: List[str] = field(default_factory=list)
    char_count: int = 0
    chunk_count: int = 0
    fetched_at: float = 0.0
    updated_at: float = field(default_factory=time.time)
    last_error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class KnowledgeChunk:
    id: str
    doc_id: str
    seq: int
    heading_path: str
    text: str
    vector: List[float] = field(default_factory=list)


@dataclass
class ChunkHit:
    """知识库命中。刻意不复用 SearchHit：它带 record/hit_count/weight 那一套
    记忆语义，混用会让调用方以为这条能 memory_update。"""

    chunk: KnowledgeChunk
    doc: KnowledgeDoc
    score: float
    reasons: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "chunk_id": self.chunk.id,
            "doc_id": self.doc.id,
            "title": self.doc.title,
            "url": self.doc.url,
            "heading_path": self.chunk.heading_path,
            "text": self.chunk.text,
            "score": round(self.score, 4),
            "reasons": list(self.reasons),
        }


# 飞书对「文档已删除」「找不到」给的错误码。这类链接重试多少次都没用，
# 补录时要跳过——否则每轮全量扫描都白抓一遍，还会把用户刚删掉的失败条目又建回来
_DEAD_LINK_MARKERS = ("1770003", "resource deleted", "131005", "not found")


def is_dead_link_error(error: str) -> bool:
    low = (error or "").lower()
    return any(m in low for m in _DEAD_LINK_MARKERS)


def _doc_from_dict(item: dict) -> KnowledgeDoc:
    known = {f.name for f in fields(KnowledgeDoc)}
    data = {k: v for k, v in (item or {}).items() if k in known}
    data.setdefault("id", uuid.uuid4().hex[:12])
    data.setdefault("url", "")
    data.setdefault("title", "")
    if not isinstance(data.get("tags"), list):
        data["tags"] = []
    if not isinstance(data.get("source_tokens"), list):
        data["source_tokens"] = []
    return KnowledgeDoc(**data)


def paired_backup_path(declarative_backup: str | Path) -> Path:
    """长时记忆备份文件 → 与之配对的知识库快照路径。

    一次「备份」要落两个文件（记忆一份、知识库一份），靠文件名配对而不是塞进
    同一个文件：长时记忆那层不该知道知识库的存在，塞在一起就得让它认识知识库格式。

    `declarative_20260806_154500.json` → `knowledge_20260806_154500.json`；
    自定义文件名则退化成 `<原名>_knowledge.json`。注意不能生成
    `declarative_..._knowledge.json` 这种名字——`LongTermMemory.list_backups`
    按 `declarative_*.json` 匹配，会把知识库快照当成记忆备份列出来。
    """
    p = Path(declarative_backup)
    if p.stem.startswith("declarative_"):
        return p.with_name("knowledge_" + p.stem[len("declarative_"):] + p.suffix)
    return p.with_name(f"{p.stem}_knowledge{p.suffix}")


def _query_coverage(query_keywords: Sequence[str], blob: str) -> float:
    """查询里有多少词在这一块里出现过。

    这里不能用长时记忆那套 `keyword_overlap`（Jaccard）：那是问法对问法，两边
    长度相当；而这里是十来个字的问题对上八百字的块，并集被块的词撑爆，再相关
    也只有 0.0x，阈值根本没法定。改成「查询被覆盖了多少」就与块长无关了。

    只数长度 ≥2 的词：中文单字（「的」「权」）在长文里几乎必然出现，全算进来
    会把无关文档也顶过阈值。
    """
    words = [k for k in query_keywords if len(k) >= 2] or list(query_keywords)
    if not words:
        return 0.0
    return sum(1 for w in words if w in blob) / len(words)


class KnowledgeBase:
    """文档存储 + 块检索。线程/进程安全靠文件锁 + 原子写，与长时记忆同一套做法。"""

    VECTOR_WEIGHT = 0.45
    COVERAGE_WEIGHT = 0.55

    def __init__(
        self,
        persist_dir: str | Path,
        *,
        embedder: Optional[LocalHasherEmbedder] = None,
        # 实测：问到点子上的查询落在 0.17~0.52，问错文档或跟库里内容无关的
        # 都在 0.06 以下，取 0.15 两边都有余量
        threshold: float = 0.15,
    ) -> None:
        self.root = Path(persist_dir) / "knowledge"
        self.chunk_dir = self.root / "chunks"
        self.chunk_dir.mkdir(parents=True, exist_ok=True)
        self.docs_path = self.root / "docs.json"
        self._lock_path = self.root / "docs.json.lock"
        self.embedder = embedder or LocalHasherEmbedder()
        self.threshold = float(threshold)
        self.docs: List[KnowledgeDoc] = []
        self._stamp: Optional[tuple] = None
        self._chunk_cache: Dict[str, List[KnowledgeChunk]] = {}
        self.reload()

    # ---------- 落盘 ----------
    @contextmanager
    def _file_lock(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        fh = open(self._lock_path, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
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
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
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

    def _stamp_now(self) -> Optional[tuple]:
        try:
            st = self.docs_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def reload(self, *, force: bool = False) -> None:
        """从磁盘刷新。App / MCP / bot 是三个进程，写完彼此要看得见。"""
        stamp = self._stamp_now()
        if not force and stamp is not None and stamp == self._stamp:
            return
        self._stamp = stamp
        self._chunk_cache.clear()
        if not self.docs_path.exists():
            self.docs = []
            return
        try:
            raw = json.loads(self.docs_path.read_text(encoding="utf-8") or "[]")
        except (OSError, ValueError):
            self.docs = []
            return
        self.docs = [_doc_from_dict(x) for x in raw if isinstance(x, dict)]

    def revision(self) -> str:
        """docs.json 的变更标记（mtime_ns:size），供前端轮询后台抓取有没有落库。"""
        stamp = self._stamp_now()
        return "" if stamp is None else f"{stamp[0]}:{stamp[1]}"

    def _write_docs(self) -> None:
        self._atomic_write_json(self.docs_path, [d.as_dict() for d in self.docs])
        self._stamp = self._stamp_now()

    def _chunk_path(self, doc_id: str) -> Path:
        return self.chunk_dir / f"{doc_id}.json"

    def load_chunks(self, doc_id: str) -> List[KnowledgeChunk]:
        cached = self._chunk_cache.get(doc_id)
        if cached is not None:
            return cached
        path = self._chunk_path(doc_id)
        if not path.exists():
            self._chunk_cache[doc_id] = []
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8") or "[]")
        except (OSError, ValueError):
            raw = []
        out = [
            KnowledgeChunk(
                id=str(x.get("id") or ""),
                doc_id=doc_id,
                seq=int(x.get("seq") or 0),
                heading_path=str(x.get("heading_path") or ""),
                text=str(x.get("text") or ""),
                vector=list(x.get("vector") or []),
            )
            for x in raw
            if isinstance(x, dict)
        ]
        self._chunk_cache[doc_id] = out
        return out

    # ---------- 写入 ----------
    def find_by_document_id(self, document_id: str) -> Optional[KnowledgeDoc]:
        """按 token 找已入库的文档。wiki token 与 docx token 都认。"""
        token = (document_id or "").strip()
        if not token:
            return None
        return next(
            (d for d in self.docs if d.document_id == token or token in (d.source_tokens or [])),
            None,
        )

    def find(self, doc_id: str) -> Optional[KnowledgeDoc]:
        return next((d for d in self.docs if d.id == doc_id), None)

    def has_document(self, document_id: str, *, fresh_within: float = 0.0) -> bool:
        """库里已有这篇、且（可选）足够新。用来决定要不要再抓一次。"""
        doc = self.find_by_document_id(document_id)
        if doc is None or doc.last_error:
            return False
        if fresh_within <= 0:
            return True
        return (time.time() - float(doc.fetched_at or 0)) < fresh_within

    def upsert(
        self,
        *,
        url: str,
        title: str,
        content: str,
        document_id: str = "",
        source_tokens: Optional[Sequence[str]] = None,
        origin: str = "manual",
        scene: str = "general",
        tags: Optional[Sequence[str]] = None,
    ) -> KnowledgeDoc:
        """按 document_id 去重：同一篇反复录入是更新而不是堆积。"""
        with self._file_lock():
            self.reload(force=True)
            existing = self.find_by_document_id(document_id) if document_id else None
            doc_id = existing.id if existing else uuid.uuid4().hex[:12]
            chunks = self._build_chunks(doc_id, content, title=title)
            # 同一篇可能既被 wiki 链接引到、也被 docx 链接引到，两个都要记下
            known = list(existing.source_tokens) if existing else []
            for tok in list(source_tokens or []) + ([existing.document_id] if existing else []):
                if tok and tok != document_id and tok not in known:
                    known.append(tok)
            doc = KnowledgeDoc(
                id=doc_id,
                url=url or (existing.url if existing else ""),
                title=title or (existing.title if existing else url),
                document_id=document_id,
                source_tokens=known,
                origin=(existing.origin if existing else origin),
                scene=scene,
                tags=list(tags or (existing.tags if existing else [])),
                char_count=len(content or ""),
                chunk_count=len(chunks),
                fetched_at=time.time(),
                updated_at=time.time(),
                last_error="",
            )
            self._atomic_write_json(
                self._chunk_path(doc_id),
                [
                    {
                        "id": c.id,
                        "seq": c.seq,
                        "heading_path": c.heading_path,
                        "text": c.text,
                        "vector": c.vector,
                    }
                    for c in chunks
                ],
            )
            self.docs = [d for d in self.docs if d.id != doc_id]
            self.docs.append(doc)
            self._write_docs()
            self._chunk_cache[doc_id] = chunks
            return doc

    def record_failure(
        self,
        *,
        url: str,
        document_id: str,
        error: str,
        origin: str = "manual",
        source_tokens: Optional[Sequence[str]] = None,
    ) -> KnowledgeDoc:
        """抓取失败也要留痕，否则用户只会看到「链接发过了但列表里没有」。"""
        with self._file_lock():
            self.reload(force=True)
            existing = self.find_by_document_id(document_id) if document_id else None
            if existing is not None:
                existing.last_error = error
                existing.updated_at = time.time()
                doc = existing
            else:
                doc = KnowledgeDoc(
                    id=uuid.uuid4().hex[:12],
                    url=url,
                    title=url,
                    document_id=document_id,
                    source_tokens=[t for t in (source_tokens or []) if t and t != document_id],
                    origin=origin,
                    last_error=error,
                )
                self.docs.append(doc)
            self._write_docs()
            return doc

    def delete(self, doc_id: str) -> bool:
        with self._file_lock():
            self.reload(force=True)
            if not any(d.id == doc_id for d in self.docs):
                return False
            self.docs = [d for d in self.docs if d.id != doc_id]
            self._write_docs()
            try:
                self._chunk_path(doc_id).unlink()
            except OSError:
                pass
            self._chunk_cache.pop(doc_id, None)
            return True

    def _build_chunks(self, doc_id: str, content: str, *, title: str) -> List[KnowledgeChunk]:
        out: List[KnowledgeChunk] = []
        for ch in split_document(content, title=title):
            out.append(
                KnowledgeChunk(
                    id=f"{doc_id}-{ch.seq:03d}",
                    doc_id=doc_id,
                    seq=ch.seq,
                    heading_path=ch.heading_path,
                    text=ch.text,
                    vector=self.embedder.embed(ch.embed_text),
                )
            )
        return out

    # ---------- 检索 ----------
    def search_chunks(
        self,
        query: str,
        *,
        top_k: int = 3,
        threshold: Optional[float] = None,
        per_doc: int = 1,
    ) -> List[ChunkHit]:
        """
        块级软召回。`per_doc` 限制同一篇文档最多贡献几块——一篇长文里
        相邻几块往往互相重复，全放进参考包只会挤掉别的文档。
        """
        self.reload()
        if not self.docs:
            return []
        q = clean_text(query)
        if not q:
            return []
        thr = self.threshold if threshold is None else float(threshold)
        qvec = self.embedder.embed(q)
        qkw = extract_keywords(q)

        hits: List[ChunkHit] = []
        for doc in self.docs:
            if doc.last_error:
                continue
            for chunk in self.load_chunks(doc.id):
                vscore = cosine_similarity(qvec, chunk.vector)
                cscore = _query_coverage(qkw, f"{chunk.heading_path} {chunk.text}".lower())
                score = self.VECTOR_WEIGHT * vscore + self.COVERAGE_WEIGHT * cscore
                if score < thr:
                    continue
                reasons = []
                if vscore >= 0.2:
                    reasons.append(f"vector:{vscore:.2f}")
                if cscore >= 0.2:
                    reasons.append(f"coverage:{cscore:.2f}")
                hits.append(ChunkHit(chunk=chunk, doc=doc, score=score, reasons=reasons))

        hits.sort(key=lambda h: h.score, reverse=True)
        if per_doc > 0:
            kept: List[ChunkHit] = []
            seen: Dict[str, int] = {}
            for h in hits:
                n = seen.get(h.doc.id, 0)
                if n >= per_doc:
                    continue
                seen[h.doc.id] = n + 1
                kept.append(h)
            hits = kept
        return hits[: max(0, top_k)]

    # ---------- 备份 / 恢复 ----------
    def backup_dir(self) -> Path:
        """与长时记忆共用 `{persist_dir}/backups/`：一次备份的东西要放一起才找得到。"""
        path = self.root.parent / "backups"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def backup(self, dest: Optional[str | Path] = None) -> Path:
        """
        导出一份知识库快照。

        **不含向量**：一个块的 256 维向量写成 JSON 比它的正文还长七八倍，
        而向量完全可以从正文重算（本地哈希 embedder，没有模型下载）。
        少存这一份还顺带解决了换 embedding 维度后老备份没法恢复的问题。
        """
        self.reload()
        if dest:
            out = Path(dest).expanduser()
            if out.is_dir() or str(dest).endswith(("/", "\\")):
                out.mkdir(parents=True, exist_ok=True)
                out = out / f"knowledge_{datetime.now():%Y%m%d_%H%M%S}.json"
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
        else:
            out = self.backup_dir() / f"knowledge_{datetime.now():%Y%m%d_%H%M%S}.json"

        docs = []
        for doc in self.docs:
            item = doc.as_dict()
            item["chunks"] = [
                {"seq": c.seq, "heading_path": c.heading_path, "text": c.text}
                for c in self.load_chunks(doc.id)
            ]
            docs.append(item)
        self._atomic_write_json(
            out,
            {
                "version": 1,
                "kind": "knowledge_backup",
                "created_at": time.time(),
                "created_at_iso": datetime.now().isoformat(timespec="seconds"),
                "doc_count": len(docs),
                "chunk_count": sum(len(d["chunks"]) for d in docs),
                "docs": docs,
            },
        )
        return out

    def list_backups(self) -> List[Path]:
        d = self.backup_dir()
        return sorted(d.glob("knowledge_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    def restore(self, path: Optional[str | Path] = None) -> int:
        """从快照恢复（覆盖当前知识库），返回恢复的文档数。向量按正文重算。"""
        if path:
            src = Path(path).expanduser()
        else:
            backups = self.list_backups()
            if not backups:
                raise FileNotFoundError("没有可用的知识库备份")
            src = backups[0]
        if not src.is_file():
            raise FileNotFoundError(f"备份文件不存在：{src}")

        data = json.loads(src.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("docs"), list):
            raise ValueError("无法识别的知识库备份格式")

        with self._file_lock():
            docs: List[KnowledgeDoc] = []
            chunks_by_doc: Dict[str, List[KnowledgeChunk]] = {}
            for item in data["docs"]:
                if not isinstance(item, dict):
                    continue
                doc = _doc_from_dict(item)
                rebuilt: List[KnowledgeChunk] = []
                for raw in item.get("chunks") or []:
                    if not isinstance(raw, dict):
                        continue
                    seq = int(raw.get("seq") or len(rebuilt))
                    heading = str(raw.get("heading_path") or "")
                    text = str(raw.get("text") or "")
                    embed_text = f"{heading}\n{text}" if heading else text
                    rebuilt.append(
                        KnowledgeChunk(
                            id=f"{doc.id}-{seq:03d}",
                            doc_id=doc.id,
                            seq=seq,
                            heading_path=heading,
                            text=text,
                            vector=self.embedder.embed(embed_text),
                        )
                    )
                doc.chunk_count = len(rebuilt)
                docs.append(doc)
                chunks_by_doc[doc.id] = rebuilt

            # 先清掉现有块文件：备份里没有的文档不能留在库里，否则「恢复」
            # 变成「合并」，删掉过的文档会随着每次恢复重新冒出来
            for stale in self.chunk_dir.glob("*.json"):
                try:
                    stale.unlink()
                except OSError:
                    pass
            for doc_id, chunks in chunks_by_doc.items():
                self._atomic_write_json(
                    self._chunk_path(doc_id),
                    [
                        {
                            "id": c.id,
                            "seq": c.seq,
                            "heading_path": c.heading_path,
                            "text": c.text,
                            "vector": c.vector,
                        }
                        for c in chunks
                    ],
                )
            self.docs = docs
            self._write_docs()
            self._chunk_cache = dict(chunks_by_doc)
            return len(docs)

    def stats(self) -> dict:
        self.reload()
        return {
            "doc_count": len(self.docs),
            "chunk_count": sum(int(d.chunk_count or 0) for d in self.docs),
            "failed_count": sum(1 for d in self.docs if d.last_error),
            "persist_dir": str(self.root),
        }

    def list_docs(self) -> List[dict]:
        self.reload()
        return [
            d.as_dict()
            for d in sorted(self.docs, key=lambda x: float(x.updated_at or 0), reverse=True)
        ]

    def read_doc(self, doc_id: str) -> Optional[dict]:
        self.reload()
        doc = self.find(doc_id)
        if doc is None:
            return None
        data = doc.as_dict()
        data["chunks"] = [
            {"id": c.id, "seq": c.seq, "heading_path": c.heading_path, "text": c.text}
            for c in self.load_chunks(doc_id)
        ]
        return data


def format_knowledge_pack(hits: Sequence[ChunkHit], *, max_chars: int = 900) -> str:
    """
    知识库片段拼成给外部 Agent 的文本块。

    必须与记忆的 context_pack 分开成节：那段开场白要求「某条与现状矛盾就用它的 id
    调 memory_update 修正」，而这里的 id 是文档块、根本不是记忆，混在一起 Agent
    会拿着块 id 去 update，必然报错。所以这里明说「要改去改原文档」。
    """
    if not hits:
        return ""
    blocks: List[str] = [
        "【记忆沙箱 · 知识库原文】以下为已入库文档的原文摘录，不是记忆条目："
        "不要对它们调 memory_update / memory_delete（那两个只认记忆 id）；"
        "内容有误请直接改原文档，改完可重新入库。"
    ]
    for i, hit in enumerate(hits, 1):
        text = (hit.chunk.text or "").strip()
        if max_chars > 0 and len(text) > max_chars:
            text = text[:max_chars].rstrip() + "…"
        where = hit.chunk.heading_path or hit.doc.title
        blocks.append(
            f"{i}. 《{hit.doc.title}》 § {where}\n"
            f"   {hit.doc.url}\n"
            f"   {text}"
        )
    return "\n".join(blocks)
