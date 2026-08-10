#!/usr/bin/env python3
"""知识库：分块、存储、检索、入库钩子（标准库 unittest）。"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.config import AppConfig, FeishuConfig, LLMConfig, LongTermConfig, SensoryConfig, WorkingConfig
from core.knowledge import KnowledgeBase, format_knowledge_pack, paired_backup_path
from core.knowledge_chunk import MAX_CHARS, MIN_CHARS, split_document
from core.knowledge_ingest import KnowledgeIngestWorker, ingest_url

DOC_URL = "https://bytedance.larkoffice.com/docx/AbCdEf123456"


def long_section(topic: str, n: int = 12) -> str:
    line = f"{topic}相关的正文段落，写得足够长以便撑满一个块，避免被当成碎块并进上一节。"
    return "\n\n".join(line for _ in range(n))


class ChunkTests(unittest.TestCase):
    def test_headings_become_boundaries(self):
        doc = f"## 权限管理\n\n{long_section('权限')}\n\n## 检索设置\n\n{long_section('检索')}"
        chunks = split_document(doc, title="手册")
        paths = {c.heading_path for c in chunks}
        self.assertIn("手册 / 权限管理", paths)
        self.assertIn("手册 / 检索设置", paths)

    def test_heading_path_is_in_embed_text(self):
        """标题里往往是最关键的检索词，正文可能通篇不再重复它。"""
        chunks = split_document(f"## 用户身份\n\n{long_section('身份')}", title="手册")
        self.assertTrue(chunks[0].embed_text.startswith("手册 / 用户身份"))

    def test_tiny_sections_merge_and_path_falls_back_to_ancestor(self):
        """碎块并进前一块后，路径必须退到共同祖先，否则会指着错误的小节。"""
        doc = "# 权限\n\n## 甲\n\n短短一句。\n\n## 乙\n\n也很短。\n"
        chunks = split_document(doc, title="手册")
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0].heading_path, "手册 / 权限")
        # 合并进来的小节标题要留在正文里，否则分不出哪句属于哪节
        self.assertIn("乙", chunks[0].text)

    def test_no_chunk_exceeds_hard_cap(self):
        chunks = split_document(long_section("超长", n=200), title="长文")
        self.assertTrue(chunks)
        for c in chunks:
            self.assertLessEqual(len(c.text), MAX_CHARS + MIN_CHARS)

    def test_numbered_headings_still_count(self):
        doc = f"1.1 权限管理\n\n{long_section('权限')}\n\n二、检索设置\n\n{long_section('检索')}"
        paths = {c.heading_path for c in split_document(doc, title="手册")}
        self.assertIn("手册 / 1.1 权限管理", paths)
        self.assertIn("手册 / 二、检索设置", paths)

    def test_bare_numbers_and_dates_are_not_headings(self):
        """飞书文档里满是「1031」「07.27」这种孤零零的数字，当成小节名会印出「§ 07.27」。"""
        doc = f"## 真标题\n\n1031\n\n07.27\n\n2026\n\n{long_section('正文')}"
        paths = {c.heading_path for c in split_document(doc, title="手册")}
        self.assertEqual(paths, {"手册 / 真标题"})

    def test_empty_document_yields_nothing(self):
        self.assertEqual(split_document("   \n\n  "), [])


class KnowledgeBaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kb_")
        self.kb = KnowledgeBase(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add(self, *, title="飞书接入手册", content=None, document_id="tok1"):
        return self.kb.upsert(
            url=DOC_URL,
            title=title,
            content=content if content is not None else f"## 权限管理\n\n{long_section('权限')}",
            document_id=document_id,
        )

    def test_upsert_then_search_reports_the_section(self):
        self.add()
        hits = self.kb.search_chunks("权限管理怎么配")
        self.assertTrue(hits)
        self.assertIn("权限管理", hits[0].chunk.heading_path)
        self.assertEqual(hits[0].doc.url, DOC_URL)

    def test_same_document_id_updates_instead_of_piling_up(self):
        first = self.add(title="旧标题")
        second = self.add(title="新标题", content=f"## 新内容\n\n{long_section('新')}")
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.kb.docs), 1)
        self.assertEqual(self.kb.docs[0].title, "新标题")

    def test_delete_removes_the_chunk_file_too(self):
        doc = self.add()
        path = self.kb.chunk_dir / f"{doc.id}.json"
        self.assertTrue(path.exists())
        self.assertTrue(self.kb.delete(doc.id))
        self.assertFalse(path.exists())
        self.assertEqual(self.kb.search_chunks("权限管理怎么配"), [])

    def test_failed_doc_is_kept_but_never_searched(self):
        """抓失败也要留痕，否则用户只看到「链接发过了但列表里没有」。"""
        self.kb.record_failure(url=DOC_URL, document_id="tok9", error="没有权限")
        self.assertEqual(len(self.kb.docs), 1)
        self.assertEqual(self.kb.search_chunks("权限"), [])
        self.assertFalse(self.kb.has_document("tok9"))

    def test_one_doc_cannot_flood_the_results(self):
        body = "\n\n".join(f"## 第{i}节\n\n{long_section(f'第{i}节权限')}" for i in range(6))
        self.add(content=body)
        hits = self.kb.search_chunks("权限", top_k=5, per_doc=1)
        self.assertEqual(len(hits), 1)

    def test_second_process_sees_the_write(self):
        """App / MCP / bot 是三个进程，写完彼此要看得见。"""
        self.add()
        other = KnowledgeBase(self.tmp)
        self.assertEqual(len(other.docs), 1)


class KnowledgePackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kb_pack_")
        self.kb = KnowledgeBase(self.tmp)
        self.kb.upsert(
            url=DOC_URL,
            title="飞书接入手册",
            content=f"## 权限管理\n\n{long_section('权限')}",
            document_id="tok1",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_pack_tells_the_agent_not_to_memory_update(self):
        """块 id 不是记忆 id，混着说 Agent 会拿它去改一条不存在的记忆。"""
        pack = format_knowledge_pack(self.kb.search_chunks("权限管理"))
        self.assertIn("知识库原文", pack)
        self.assertIn("不要对它们调 memory_update", pack)

    def test_pack_names_the_document_and_link(self):
        pack = format_knowledge_pack(self.kb.search_chunks("权限管理"))
        self.assertIn("飞书接入手册", pack)
        self.assertIn(DOC_URL, pack)

    def test_no_hits_no_pack(self):
        self.assertEqual(format_knowledge_pack([]), "")


class FakeFetch:
    """替掉 fetch_feishu_document：单测不许真的连飞书。"""

    def __init__(self, *, ok=True, title="飞书接入手册", content=None, error="", boom=False):
        self.ok = ok
        self.title = title
        self.content = content if content is not None else f"## 权限管理\n\n{long_section('权限')}"
        self.error = error
        self.boom = boom
        self.calls = 0

    def __call__(self, cfg, ref, *, config_path=None, **kw):
        self.calls += 1
        if self.boom:
            raise RuntimeError("网络炸了")
        return SimpleNamespace(
            ok=self.ok,
            url=ref.url,
            title=self.title,
            content=self.content,
            error=self.error,
            document_id=ref.token,
        )


class IngestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kb_ingest_")
        self.kb = KnowledgeBase(self.tmp)
        self.cfg = FeishuConfig()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_ingest_stores_the_document(self):
        res = ingest_url(self.kb, self.cfg, DOC_URL, fetcher=FakeFetch())
        self.assertTrue(res.ok)
        self.assertEqual(len(self.kb.docs), 1)
        self.assertTrue(self.kb.search_chunks("权限管理"))

    def test_non_feishu_link_is_refused_early(self):
        res = ingest_url(self.kb, self.cfg, "https://example.com/a", fetcher=FakeFetch())
        self.assertFalse(res.ok)
        self.assertEqual(len(self.kb.docs), 0)

    def test_fetch_failure_is_recorded_not_raised(self):
        res = ingest_url(self.kb, self.cfg, DOC_URL, fetcher=FakeFetch(ok=False, error="没有权限"))
        self.assertFalse(res.ok)
        self.assertEqual(self.kb.docs[0].last_error, "没有权限")

    def test_a_crashing_fetcher_is_recorded_too(self):
        res = ingest_url(self.kb, self.cfg, DOC_URL, fetcher=FakeFetch(boom=True))
        self.assertFalse(res.ok)
        self.assertIn("网络炸了", self.kb.docs[0].last_error)

    def test_a_wiki_link_is_recognised_as_stored_next_time(self):
        """wiki 链接的 token 与它解析出的 docx document_id 不同。

        只记后者的话，每次补录都会把所有 wiki 文档重抓一遍。
        """
        wiki = "https://bytedance.larkoffice.com/wiki/WikiTok123"

        class Resolving(FakeFetch):
            def __call__(self, cfg, ref, **kw):
                res = super().__call__(cfg, ref, **kw)
                res.document_id = "RealDocxTok"  # 飞书把 wiki token 解析成真实 docx
                return res

        ingest_url(self.kb, self.cfg, wiki, fetcher=Resolving())
        self.assertTrue(self.kb.has_document("WikiTok123"))
        self.assertTrue(self.kb.has_document("RealDocxTok"))

    def test_wiki_and_docx_links_do_not_store_twice(self):
        class Resolving(FakeFetch):
            def __call__(self, cfg, ref, **kw):
                res = super().__call__(cfg, ref, **kw)
                res.document_id = "RealDocxTok"
                return res

        ingest_url(self.kb, self.cfg, "https://x.larkoffice.com/wiki/WikiTok123", fetcher=Resolving())
        ingest_url(self.kb, self.cfg, "https://x.larkoffice.com/docx/RealDocxTok", fetcher=Resolving())
        self.assertEqual(len(self.kb.docs), 1)

    def test_fresh_document_is_not_refetched(self):
        fetcher = FakeFetch()
        ingest_url(self.kb, self.cfg, DOC_URL, fetcher=fetcher)
        res = ingest_url(self.kb, self.cfg, DOC_URL, fetcher=fetcher, fresh_within=3600)
        self.assertTrue(res.skipped)
        # 抓一次是为了拿 document_id（wiki 链接要解析），但没有重新入库
        self.assertEqual(len(self.kb.docs), 1)

    def test_a_token_without_a_link_can_still_be_ingested(self):
        """文档评论事件只给 file_token：doc_host 没配时 docx_url() 返回空串，
        没有链接可解析。这条路断了就等于机器人回过话的文档一篇都进不来。"""
        from core.feishu import FeishuDocRef

        ref = FeishuDocRef(url="", kind="docx", token="OnlyTok123")
        meta = lambda cfg, token, **kw: SimpleNamespace(  # noqa: E731
            ok=True, url=f"https://bytedance.sg.larkoffice.com/docx/{token}", title=""
        )
        res = ingest_url(self.kb, self.cfg, "", ref=ref, fetcher=FakeFetch(), meta_fetcher=meta)
        self.assertTrue(res.ok)
        self.assertTrue(self.kb.has_document("OnlyTok123"))
        # 链接是跟飞书要来的，不是按 doc_host 猜的——同一租户的文档可能在别的区域域名下
        self.assertEqual(self.kb.docs[0].url, "https://bytedance.sg.larkoffice.com/docx/OnlyTok123")

    def test_a_missing_link_does_not_block_the_ingest(self):
        """要不到链接也要入库：正文能被召回是主目的，点不开原文只是体验降级。"""
        from core.feishu import FeishuDocRef

        ref = FeishuDocRef(url="", kind="docx", token="OnlyTok123")
        meta = lambda cfg, token, **kw: SimpleNamespace(ok=False, url="", error="没权限")  # noqa: E731
        res = ingest_url(self.kb, self.cfg, "", ref=ref, fetcher=FakeFetch(), meta_fetcher=meta)
        self.assertTrue(res.ok)
        self.assertEqual(self.kb.docs[0].url, "")
        self.assertTrue(self.kb.search_chunks("权限管理"))


class IngestWorkerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kb_worker_")
        self.kb = KnowledgeBase(self.tmp)
        self.seen: list = []

        def fake_ingest(kb, cfg, url, **kw):
            self.seen.append(url)
            return ingest_url(kb, cfg, url, fetcher=FakeFetch(), **kw)

        self.worker = KnowledgeIngestWorker(self.kb, FeishuConfig(), ingest=fake_ingest)

    def tearDown(self):
        self.worker.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_submit_text_queues_every_feishu_link(self):
        self.assertEqual(
            self.worker.submit_text(f"见 {DOC_URL} 和 https://example.com/x"),
            [DOC_URL],
        )
        self.worker.join(timeout=3)
        self.assertEqual(self.seen, [DOC_URL])
        self.assertEqual(len(self.kb.docs), 1)

    def test_same_link_twice_in_one_text_queues_once(self):
        queued = self.worker.submit_text(f"{DOC_URL} 又见 {DOC_URL}")
        self.assertEqual(len(queued), 1)

    def test_already_stored_link_is_skipped(self):
        self.worker.submit_text(f"见 {DOC_URL}")
        self.worker.join(timeout=3)
        self.assertEqual(self.worker.submit_text(f"又见 {DOC_URL}"), [])


class SandboxKnowledgeTests(unittest.TestCase):
    """记忆写入 → 自动入库这条链路。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kb_sandbox_")
        self.sb = MemorySandbox(
            config=AppConfig(
                sensory=SensoryConfig(ttl=5.0),
                working=WorkingConfig(chunk_size=7),
                long_term=LongTermConfig(persist_dir=self.tmp, similarity_threshold=0.65),
                llm=LLMConfig(enabled=False, provider="mock"),
                feishu=FeishuConfig(enabled=True, app_id="cli_x", app_secret="secret"),
            )
        )

    def tearDown(self):
        worker = self.sb._knowledge_worker
        if worker is not None:
            worker.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_remember_queues_the_link_in_the_text(self):
        worker = self.sb.knowledge_worker()
        self.assertIsNotNone(worker)
        queued: list = []
        worker.submit_text = lambda text, **kw: queued.append(text) or []  # type: ignore[assignment]
        self.sb.remember("接入手册在哪", f"看这篇 {DOC_URL}")
        self.assertTrue(any(DOC_URL in t for t in queued))

    def test_remember_survives_a_broken_knowledge_layer(self):
        """入库是附加能力，坏了也不能连累写记忆。"""
        def boom(*a, **kw):
            raise RuntimeError("知识库炸了")

        self.sb.knowledge_worker = boom  # type: ignore[assignment]
        msg = self.sb.remember("接入手册在哪", f"看这篇 {DOC_URL}")
        self.assertIn("已写入长时记忆", msg)

    def test_feishu_switched_off_means_no_worker(self):
        """没配飞书就别起线程：每次记忆都白排一次队。"""
        self.sb.config.feishu = FeishuConfig(enabled=False)
        self.assertIsNone(self.sb.knowledge_worker())

    def test_reference_pack_carries_knowledge_in_its_own_section(self):
        self.sb.knowledge.upsert(
            url=DOC_URL,
            title="飞书接入手册",
            content=f"## 权限管理\n\n{long_section('权限')}",
            document_id="tok1",
        )
        pack = self.sb.build_reference_pack("权限管理怎么配")
        self.assertTrue(pack["knowledge"])
        self.assertIn("知识库原文", pack["context_pack"])
        # references 是拿来 memory_update 的，文档块不能混进去
        self.assertEqual(pack["references"], [])

    def test_broken_knowledge_search_does_not_break_recall(self):
        def boom(*a, **kw):
            raise RuntimeError("读盘炸了")

        self.sb.knowledge.search_chunks = boom  # type: ignore[assignment]
        self.assertEqual(self.sb.collect_knowledge("随便问问"), [])

    def test_status_reports_knowledge(self):
        st = self.sb.status()
        self.assertIn("knowledge", st)
        self.assertEqual(st["knowledge"]["doc_count"], 0)


class BackfillTests(unittest.TestCase):
    """存量补录：启用知识库之前写的记忆，里面的链接一条都没抓过。"""

    OTHER_URL = "https://bytedance.larkoffice.com/wiki/WkiTok654321"

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kb_backfill_")
        self.sb = MemorySandbox(
            config=AppConfig(
                sensory=SensoryConfig(ttl=5.0),
                working=WorkingConfig(chunk_size=7),
                long_term=LongTermConfig(persist_dir=self.tmp, similarity_threshold=0.65),
                llm=LLMConfig(enabled=False, provider="mock"),
                feishu=FeishuConfig(enabled=True, app_id="cli_x", app_secret="secret"),
            )
        )
        # 先把自动入库挡掉，模拟「这些记忆是启用知识库之前写的」
        self.sb.queue_knowledge_from_text = lambda *a, **kw: []  # type: ignore[assignment]
        self.sb.remember("接入手册在哪", f"看这篇 {DOC_URL}")
        self.sb.remember("发版流程", f"见 {self.OTHER_URL}")
        self.sb.remember("跟文档无关的一条", "本地 mock 端口 8899")
        self.fetcher = FakeFetch()
        self.sb.add_knowledge = lambda url, **kw: self._fake_add(url, **kw)  # type: ignore[assignment]

    def _fake_add(self, url, **kw):
        res = ingest_url(
            self.sb.knowledge, self.sb.config.feishu, url, fetcher=self.fetcher, **kw
        )
        return {
            "ok": res.ok,
            "error": res.error,
            "skipped": res.skipped,
            "doc": res.doc.as_dict() if res.doc else None,
        }

    def tearDown(self):
        worker = self.sb._knowledge_worker
        if worker is not None:
            worker.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_finds_every_distinct_document(self):
        found = {c["token"] for c in self.sb.scan_memory_links()}
        self.assertEqual(found, {"AbCdEf123456", "WkiTok654321"})

    def test_scan_reports_which_memory_it_came_from(self):
        """tab 与 dry-run 里要看得出这篇是跟着哪条记忆进来的。"""
        item = next(c for c in self.sb.scan_memory_links() if c["token"] == "AbCdEf123456")
        self.assertIn("接入手册在哪", item["question"])
        self.assertTrue(item["memory_id"])

    def test_scan_skips_documents_already_stored(self):
        self.sb.knowledge.upsert(
            url=DOC_URL, title="已有的", content=long_section("已有"), document_id="AbCdEf123456"
        )
        self.assertEqual([c["token"] for c in self.sb.scan_memory_links()], ["WkiTok654321"])

    def test_refresh_lists_stored_documents_again(self):
        self.sb.knowledge.upsert(
            url=DOC_URL, title="已有的", content=long_section("已有"), document_id="AbCdEf123456"
        )
        found = {c["token"] for c in self.sb.scan_memory_links(refresh=True)}
        self.assertEqual(found, {"AbCdEf123456", "WkiTok654321"})

    def test_backfill_stores_them_all(self):
        res = self.sb.backfill_knowledge()
        self.assertEqual(len(res["done"]), 2)
        self.assertEqual(self.sb.knowledge.stats()["doc_count"], 2)

    def test_backfill_records_where_each_doc_came_from(self):
        self.sb.backfill_knowledge()
        self.assertTrue(all(d.origin.startswith("memory:") for d in self.sb.knowledge.docs))

    def test_one_bad_document_does_not_abort_the_rest(self):
        calls = {"n": 0}
        real = self.fetcher

        def flaky(cfg, ref, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("这篇没权限")
            return real(cfg, ref, **kw)

        self.fetcher = flaky  # type: ignore[assignment]
        res = self.sb.backfill_knowledge()
        self.assertEqual(len(res["failed"]), 1)
        self.assertEqual(len(res["done"]), 1)

    def test_limit_caps_the_run(self):
        res = self.sb.backfill_knowledge(limit=1)
        self.assertEqual(res["candidates"], 1)
        self.assertEqual(len(res["done"]), 1)

    def test_nothing_to_do_is_not_an_error(self):
        self.sb.backfill_knowledge()
        res = self.sb.backfill_knowledge()
        self.assertEqual(res["candidates"], 0)
        self.assertEqual(res["done"], [])

    def test_links_to_deleted_documents_stop_being_retried(self):
        """文档在飞书侧被删了，重试多少次都一样，还会把用户删掉的红条目建回来。"""
        self.sb.knowledge.record_failure(
            url=DOC_URL,
            document_id="AbCdEf123456",
            error='HTTP 400: {"code":1770003,"msg":"resource deleted"}',
        )
        self.assertEqual([c["token"] for c in self.sb.scan_memory_links()], ["WkiTok654321"])

    def test_a_merely_failed_fetch_is_retried(self):
        """没权限、网络抖动这类是暂时的，下次还要试。"""
        self.sb.knowledge.record_failure(
            url=DOC_URL, document_id="AbCdEf123456", error="HTTP 403: 无权限"
        )
        self.assertIn("AbCdEf123456", [c["token"] for c in self.sb.scan_memory_links()])

    def test_queue_variant_hands_everything_to_the_worker(self):
        """App 那侧走队列：十几篇要抓一分钟，HTTP 不能干等。"""
        worker = self.sb.knowledge_worker()
        assert worker is not None
        worker.stop()
        submitted: list = []
        worker.submit = lambda url, **kw: submitted.append((url, kw)) or True  # type: ignore[assignment]
        res = self.sb.queue_backfill_knowledge()
        self.assertTrue(res["ok"])
        self.assertEqual(res["queued"], 2)
        self.assertTrue(all(kw["origin"].startswith("memory:") for _, kw in submitted))

    def test_queue_variant_says_so_when_feishu_is_off(self):
        self.sb.config.feishu = FeishuConfig(enabled=False)
        self.sb._knowledge_worker = None
        res = self.sb.queue_backfill_knowledge()
        self.assertFalse(res["ok"])
        self.assertIn("飞书", res["error"])


class KnowledgeBackupTests(unittest.TestCase):
    """备份要连知识库一起，否则恢复出来的库是残的。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="kb_backup_")
        self.sb = MemorySandbox(
            config=AppConfig(
                sensory=SensoryConfig(ttl=5.0),
                working=WorkingConfig(chunk_size=7),
                long_term=LongTermConfig(persist_dir=self.tmp, similarity_threshold=0.65),
                llm=LLMConfig(enabled=False, provider="mock"),
            )
        )
        self.sb.knowledge.upsert(
            url=DOC_URL,
            title="飞书接入手册",
            content=f"## 权限管理\n\n{long_section('权限')}",
            document_id="tok1",
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_backup_writes_a_paired_knowledge_snapshot(self):
        msg = self.sb.backup_long_term()
        self.assertIn("知识库 1 篇", msg)
        self.assertEqual(len(self.sb.knowledge.list_backups()), 1)

    def test_the_pair_shares_one_timestamp(self):
        self.sb.backup_long_term()
        decl = self.sb.long_term.list_backups()[0]
        self.assertTrue(paired_backup_path(decl).is_file())

    def test_knowledge_snapshot_is_not_mistaken_for_a_memory_backup(self):
        """两种备份同处一个目录，命名撞车就会把快照当成记忆备份列出来。"""
        self.sb.backup_long_term()
        for p in self.sb.long_term.list_backups():
            self.assertNotIn("knowledge", p.name)

    def test_restore_brings_the_documents_back(self):
        self.sb.backup_long_term()
        doc_id = self.sb.knowledge.docs[0].id
        self.sb.knowledge.delete(doc_id)
        self.assertEqual(self.sb.knowledge.stats()["doc_count"], 0)
        msg = self.sb.restore_long_term()
        self.assertIn("知识库 1 篇", msg)
        self.assertEqual(self.sb.knowledge.stats()["doc_count"], 1)

    def test_restored_chunks_are_searchable_again(self):
        """快照不存向量，恢复时要按正文重算，否则库回来了却搜不到。"""
        self.sb.backup_long_term()
        self.sb.knowledge.delete(self.sb.knowledge.docs[0].id)
        self.sb.restore_long_term()
        self.assertTrue(self.sb.knowledge.search_chunks("权限管理怎么配"))

    def test_snapshot_carries_no_vectors(self):
        path = self.sb.knowledge.backup()
        data = json.loads(path.read_text(encoding="utf-8"))
        for chunk in data["docs"][0]["chunks"]:
            self.assertNotIn("vector", chunk)

    def test_restore_drops_documents_absent_from_the_snapshot(self):
        """恢复是覆盖不是合并：删掉过的文档不能每次恢复又冒出来。"""
        self.sb.backup_long_term()
        self.sb.knowledge.upsert(
            url="https://x.larkoffice.com/docx/T2",
            title="后加的",
            content=f"## 别的\n\n{long_section('别的')}",
            document_id="tok2",
        )
        self.sb.restore_long_term()
        self.assertEqual([d.title for d in self.sb.knowledge.docs], ["飞书接入手册"])
        self.assertEqual(list(self.sb.knowledge.chunk_dir.glob("*.json")).__len__(), 1)

    def test_an_old_backup_leaves_the_knowledge_base_alone(self):
        """启用知识库之前的备份没有配对快照，不能因此把现有文档清空。"""
        decl = self.sb.long_term.backup_declarative()
        paired_backup_path(decl).unlink(missing_ok=True)
        msg = self.sb.restore_long_term(str(decl))
        self.assertIn("没有知识库快照", msg)
        self.assertEqual(self.sb.knowledge.stats()["doc_count"], 1)

    def test_a_broken_knowledge_layer_does_not_fail_the_memory_backup(self):
        def boom(*a, **kw):
            raise RuntimeError("写盘炸了")

        self.sb.knowledge.backup = boom  # type: ignore[assignment]
        msg = self.sb.backup_long_term()
        self.assertIn("已备份长时记忆", msg)
        self.assertIn("知识库快照失败", msg)

    def test_clearing_with_backup_first_snapshots_the_knowledge_base(self):
        msg = self.sb.clear_long_term(backup_first=True)
        self.assertIn("知识库 1 篇", msg)


class KnowledgeMcpToolTests(unittest.TestCase):
    def _tool(self, name):
        import mcp_server

        return next((t for t in mcp_server.TOOLS if t["name"] == name), None)

    def test_the_knowledge_tools_are_registered(self):
        for name in ("memory_knowledge_add", "memory_knowledge_list"):
            self.assertIsNotNone(self._tool(name), name)

    def test_add_requires_a_url(self):
        self.assertEqual(self._tool("memory_knowledge_add")["inputSchema"]["required"], ["url"])

    def test_description_separates_it_from_the_neighbouring_tools(self):
        """三个飞书读取工具长得像，说明里不写清区别就会被随手挑错一个。"""
        desc = self._tool("memory_knowledge_add")["description"]
        self.assertIn("memory_feishu_bookmark", desc)
        self.assertIn("memory_feishu_read", desc)


if __name__ == "__main__":
    unittest.main()
