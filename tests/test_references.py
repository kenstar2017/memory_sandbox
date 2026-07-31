"""软召回 references / context_pack 单测。"""

import shutil
import tempfile
import unittest

from core import MemorySandbox
from core.config import AppConfig, LLMConfig, LongTermConfig, SensoryConfig, WorkingConfig


class ReferencePackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_refs_")
        self.cfg = AppConfig(
            sensory=SensoryConfig(ttl=5.0),
            working=WorkingConfig(chunk_size=7),
            long_term=LongTermConfig(
                persist_dir=self.tmp,
                similarity_threshold=0.85,
                top_k=3,
                bm25_enabled=True,
                vector_weight=0.45,
                keyword_weight=0.20,
                bm25_weight=0.35,
            ),
            llm=LLMConfig(enabled=True, provider="mock"),
        )
        self.sb = MemorySandbox(config=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_soft_threshold(self):
        # hard 0.85 → soft max(0.35, 0.60) = 0.60
        self.assertAlmostEqual(self.sb.long_term.soft_threshold(), 0.60)
        self.assertAlmostEqual(self.sb.long_term.soft_threshold(0.50), 0.35)

    def test_collect_references_returns_related(self):
        self.sb.remember(
            "客服前端如何配置 IM 接入",
            "在 config 里打开 im.enabled，并配置 app_id。",
            scene="general",
            tags=["frontend", "im"],
        )
        self.sb.remember(
            "客服前端工单列表分页怎么做",
            "使用 cursor 分页，pageSize 默认 20。",
            scene="general",
            tags=["frontend", "ticket"],
        )
        self.sb.remember(
            "完全无关的数据库备份命令",
            "mysqldump -u root db > backup.sql",
            scene="general",
            tags=["db"],
        )
        hits = self.sb.collect_references("客服前端 IM 接入配置", top_k=5)
        self.assertGreaterEqual(len(hits), 1)
        blob = " ".join(
            (h.record.question or "")
            + " "
            + (h.record.answer or "")
            + " "
            + " ".join(h.record.tags or [])
            for h in hits
        )
        self.assertTrue(
            "客服" in blob or "im" in blob.lower() or "IM" in blob,
            blob,
        )
        # 不应把完全无关的 mysqldump 排在唯一结果里冒充相关
        pack = self.sb.build_reference_pack("客服前端 IM", top_k=5)
        self.assertTrue(pack["references"])
        self.assertIn("参考问答", pack["context_pack"])
        self.assertIn("问：", pack["context_pack"])
        top_blob = (pack["references"][0].get("question") or "") + (
            pack["references"][0].get("answer") or ""
        )
        self.assertNotIn("mysqldump", top_blob)

    def test_hard_hit_still_has_references(self):
        self.sb.remember(
            "mock 服务端口是多少",
            "本地 mock 端口为 3921。",
            scene="general",
        )
        r = self.sb.ask_local("mock 服务端口是多少")
        self.assertNotEqual(r.source, "miss")
        pack = self.sb.build_reference_pack("mock 服务端口是多少", top_k=5)
        self.assertTrue(pack["references"])
        self.assertIn("3921", pack["context_pack"])

    def test_feishu_token_filters_soft_refs(self):
        self.sb.remember(
            "https://bytedance.larkoffice.com/wiki/CJOywERBuimG7Ak3Co1cdLTJnoe "
            "客服前端技术文档要点",
            "旧文档：IM 接入工单",
            scene="general",
            tags=["feishu"],
        )
        q = (
            "https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh "
            "客服前端技术文档要点"
        )
        hits = self.sb.collect_references(q, top_k=5, threshold=0.3)
        for h in hits:
            blob = (h.record.question or "") + (h.record.answer or "")
            self.assertNotIn("CJOywERBuimG7Ak3Co1cdLTJnoe", blob)

    def test_context_pack_truncates_long_answer(self):
        long_ans = "A" * 1200
        self.sb.remember("很长答案测试问", long_ans, scene="general")
        hits = self.sb.collect_references("很长答案测试问", top_k=3, threshold=0.3)
        text = self.sb.long_term.format_context_pack(hits, max_answer_chars=800)
        self.assertIn("…", text)
        # 正文不应把 1200 个 A 全塞进去
        self.assertLess(text.count("A"), 1200)


class McpReferenceHelperTests(unittest.TestCase):
    def test_context_pack_from_dicts(self):
        from mcp_server import _context_pack_from_dicts, _hint_with_references

        text = _context_pack_from_dicts(
            [
                {
                    "question": "Q1",
                    "answer": "A1",
                    "score": 0.7,
                    "tags": ["dev"],
                    "reasons": ["bm25:0.9"],
                }
            ]
        )
        self.assertIn("参考问答 1", text)
        self.assertIn("问：Q1", text)
        hint = _hint_with_references(
            hit_local=False, has_refs=True, assembled="foo，记录到长期记忆。"
        )
        self.assertIn("context_pack", hint)
        self.assertIn("仓库", hint)


if __name__ == "__main__":
    unittest.main()
