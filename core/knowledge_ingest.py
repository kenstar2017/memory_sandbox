"""把飞书文档抓回来塞进知识库：同步入口 + 后台队列。

为什么要后台：`fetch_feishu_document` 要刷 user token、拉全文，几秒起步；而
`MemorySandbox.remember()` 会被 MCP 工具和飞书机器人的长连接回调同步调用，
卡在那里会让每次记忆都慢几秒，离线时还会直接失败。所以记忆里带的链接只入队，
抓取在后台线程做，失败只写进文档的 last_error，绝不往 remember 里抛。
"""

from __future__ import annotations

import queue
import threading
from dataclasses import dataclass
from typing import Any, Callable, List, Optional, Sequence

from .knowledge import KnowledgeBase, KnowledgeDoc

# 同一篇文档多久之内不重复抓。记忆里反复引用同一个链接很常见，
# 每次都重抓既慢又会把飞书接口打爆
DEFAULT_FRESH_WINDOW = 6 * 3600.0


@dataclass
class IngestResult:
    ok: bool
    url: str
    doc: Optional[KnowledgeDoc] = None
    error: str = ""
    skipped: bool = False  # 库里已有且足够新


def ingest_url(
    kb: KnowledgeBase,
    feishu_cfg: Any,
    url: str,
    *,
    origin: str = "manual",
    scene: str = "general",
    tags: Optional[Sequence[str]] = None,
    config_path: Optional[str] = None,
    fresh_within: float = 0.0,
    fetcher: Optional[Callable[..., Any]] = None,
) -> IngestResult:
    """抓一篇飞书文档并入库（同步）。`fetcher` 只为单测注入。"""
    from .feishu import extract_feishu_urls, fetch_feishu_document

    refs = extract_feishu_urls(url or "")
    if not refs:
        return IngestResult(ok=False, url=url, error="不是可识别的飞书文档链接")
    ref = refs[0]

    fetch = fetcher or fetch_feishu_document
    try:
        result = fetch(feishu_cfg, ref, config_path=config_path)
    except Exception as exc:  # noqa: BLE001 - 网络/鉴权什么都可能炸，统一记成失败
        doc = kb.record_failure(url=ref.url, document_id=ref.token, error=str(exc), origin=origin)
        return IngestResult(ok=False, url=ref.url, doc=doc, error=str(exc))

    if not getattr(result, "ok", False):
        err = getattr(result, "error", "") or "读取失败"
        doc = kb.record_failure(
            url=ref.url,
            document_id=getattr(result, "document_id", "") or ref.token,
            error=err,
            origin=origin,
            source_tokens=[ref.token],
        )
        return IngestResult(ok=False, url=ref.url, doc=doc, error=err)

    # 用飞书返回的 document_id（wiki 链接会解析成真正的 docx token），
    # 这样同一篇文档的 wiki 链接与 docx 链接不会各存一份
    document_id = getattr(result, "document_id", "") or ref.token
    if fresh_within > 0 and kb.has_document(document_id, fresh_within=fresh_within):
        return IngestResult(ok=True, url=ref.url, doc=kb.find_by_document_id(document_id), skipped=True)

    doc = kb.upsert(
        url=getattr(result, "url", "") or ref.url,
        title=getattr(result, "title", "") or ref.url,
        content=getattr(result, "content", "") or "",
        document_id=document_id,
        # 记下链接里那个 token：wiki 链接下它与 document_id 不同，
        # 不留着的话下次拿同一个 wiki 链接来查会认不出已经入过库
        source_tokens=[ref.token],
        origin=origin,
        scene=scene,
        tags=tags,
    )
    return IngestResult(ok=True, url=doc.url, doc=doc)


class KnowledgeIngestWorker:
    """单线程后台抓取队列。

    刻意不用线程池：飞书接口有频控，串行抓既够用又不会因为一次记忆里贴了十个
    链接就并发打十次。队列满了直接丢弃——知识库是增量能力，丢一次入库远好过
    把 remember 卡住。
    """

    def __init__(
        self,
        kb: KnowledgeBase,
        feishu_cfg: Any,
        *,
        config_path: Optional[str] = None,
        # 够装下一次全量补录（几百条记忆里的飞书文档去重后也就几十篇）。
        # 队列里只放 url，占不了多少内存
        max_queue: int = 256,
        fresh_within: float = DEFAULT_FRESH_WINDOW,
        ingest: Optional[Callable[..., IngestResult]] = None,
    ) -> None:
        self.kb = kb
        self.feishu_cfg = feishu_cfg
        self.config_path = config_path
        self.fresh_within = fresh_within
        self._ingest = ingest or ingest_url
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # 已入队的 token：同一轮里同一篇不重复排队
        self._inflight: set = set()
        self._lock = threading.Lock()
        self.last_error = ""

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="knowledge-ingest", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        t = self._thread
        if t is not None:
            t.join(timeout=timeout)
        self._thread = None

    def submit(
        self,
        url: str,
        *,
        origin: str = "manual",
        scene: str = "general",
        force: bool = False,
    ) -> bool:
        """入队一个链接。返回是否真的排上队。`force` 跳过刷新窗口，强制重抓。"""
        from .feishu import extract_feishu_urls

        refs = extract_feishu_urls(url or "")
        if not refs:
            return False
        token = refs[0].token
        with self._lock:
            if token in self._inflight:
                return False
            self._inflight.add(token)
        try:
            self._queue.put_nowait((refs[0].url, origin, scene, force))
        except queue.Full:
            with self._lock:
                self._inflight.discard(token)
            return False
        self.start()
        return True

    def submit_text(self, text: str, *, origin: str = "manual", scene: str = "general") -> List[str]:
        """扫一段文本里的所有飞书链接并入队，返回真正排上队的 url。"""
        from .feishu import extract_feishu_urls

        queued: List[str] = []
        for ref in extract_feishu_urls(text or ""):
            if self.kb.has_document(ref.token, fresh_within=self.fresh_within):
                continue
            if self.submit(ref.url, origin=origin, scene=scene):
                queued.append(ref.url)
        return queued

    def join(self, timeout: Optional[float] = None) -> None:
        """等队列跑空。给单测和「抓完再刷新列表」用。"""
        if timeout is None:
            self._queue.join()
            return
        # Queue.join 不支持超时，退化成轮询，免得单测挂死
        import time

        deadline = time.time() + timeout
        while time.time() < deadline and self._queue.unfinished_tasks:
            time.sleep(0.01)

    def _run(self) -> None:
        from .feishu import extract_feishu_tokens

        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                self._queue.task_done()
                break
            url, origin, scene, force = item
            try:
                self._ingest(
                    self.kb,
                    self.feishu_cfg,
                    url,
                    origin=origin,
                    scene=scene,
                    config_path=self.config_path,
                    fresh_within=0.0 if force else self.fresh_within,
                )
            except Exception as exc:  # noqa: BLE001 - 后台线程炸了不能带走整个进程
                self.last_error = str(exc)
            finally:
                for token in extract_feishu_tokens(url):
                    with self._lock:
                        self._inflight.discard(token)
                self._queue.task_done()
