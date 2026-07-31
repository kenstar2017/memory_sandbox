"""飞书链接解析与工作记忆复用单测（无网络）。"""

import shutil
import tempfile
import unittest
from unittest import mock

from core import MemorySandbox
from core.config import (
    AppConfig,
    FeishuConfig,
    LLMConfig,
    LongTermConfig,
    SensoryConfig,
    WorkingConfig,
)
from core.feishu import (
    FeishuFetchResult,
    extract_feishu_tokens,
    extract_feishu_urls,
    record_matches_feishu_tokens,
)
from core.feishu_question import rewrite_feishu_memory_question
from core.working import WorkingMemory, is_non_reusable_answer


class FeishuUrlTests(unittest.TestCase):
    def test_wiki_and_docx(self):
        text = (
            "看这个 https://bytedance.larkoffice.com/wiki/QZJowpLhBiY58xkChx0chzTZn4f ，"
            "还有 https://foo.feishu.cn/docx/AbCdEfGh1234567 分析一下"
        )
        refs = extract_feishu_urls(text)
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0].kind, "wiki")
        self.assertEqual(refs[0].token, "QZJowpLhBiY58xkChx0chzTZn4f")
        self.assertEqual(refs[1].kind, "docx")

    def test_dedupe(self):
        u = "https://bytedance.larkoffice.com/wiki/TokenAAA"
        refs = extract_feishu_urls(f"{u} {u}")
        self.assertEqual(len(refs), 1)

    def test_token_match_helper(self):
        tokens = extract_feishu_tokens(
            "https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh 客服文档"
        )
        self.assertEqual(tokens, {"RCOgwKC7KIGGdHkkZT2cOEA6nLh"})
        self.assertFalse(
            record_matches_feishu_tokens(
                ["https://bytedance.larkoffice.com/wiki/CJOywERBuimG7Ak3Co1cdLTJnoe 客服"],
                tokens,
            )
        )
        self.assertTrue(
            record_matches_feishu_tokens(
                ["wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh 客服二期"],
                tokens,
            )
        )


class FeishuLongTermFilterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_feishu_tok_")
        self.cfg = AppConfig(
            sensory=SensoryConfig(ttl=5.0),
            working=WorkingConfig(chunk_size=7),
            long_term=LongTermConfig(
                persist_dir=self.tmp,
                similarity_threshold=0.55,
                top_k=3,
                bm25_enabled=True,
            ),
            llm=LLMConfig(enabled=True, provider="mock"),
        )
        self.sb = MemorySandbox(config=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_different_wiki_token_not_hit(self):
        self.sb.remember(
            "https://bytedance.larkoffice.com/wiki/CJOywERBuimG7Ak3Co1cdLTJnoe，"
            "客服前端技术文档，整理归纳技术细节，为后续开发迭代做为技术储备",
            "旧文档摘要：IM 接入工单",
            scene="general",
            tags=["feishu", "frontend"],
        )
        q = (
            "https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh, "
            "这是另一个关于客服的前端技术文档，同样整理归纳技术细节，为后续开发迭代做为技术储备"
        )
        hits = self.sb.long_term.search_hits(q, scene="general", threshold=0.3, top_k=5)
        self.assertFalse(hits)
        r = self.sb.ask_local(q)
        self.assertEqual(r.source, "miss")

    def test_remember_rewrites_feishu_question(self):
        url = "https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh"
        q = f"{url}，读取记录该前端技术文档，整理归纳技术细节"
        msg = self.sb.remember(
            q,
            f"### 飞书文档：客服二期前端技术文档\n{url}\n\n要点：IM",
            scene="general",
        )
        self.assertIn("已优化", msg)
        rec = self.sb.last_remembered
        self.assertIsNotNone(rec)
        self.assertIn("客服二期", rec.question)
        blob = rec.question + rec.answer + str(rec.facts)
        self.assertIn("RCOgwKC7KIGGdHkkZT2cOEA6nLh", blob)

    def test_same_wiki_token_merges_not_duplicates(self):
        url = "https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh"
        self.sb.remember(
            f"飞书文档：这是另一个关于客服的前端技术文档 {url}",
            "旧摘要 v1",
            scene="general",
            tags=["feishu"],
        )
        n_before = len(self.sb.long_term.records)
        self.sb.remember(
            f"《公会客服翻译工具》技术要点与储备 {url}",
            "新摘要 v2：BatchGetTranslation",
            scene="general",
            tags=["feishu", "frontend"],
        )
        n_after = len(self.sb.long_term.records)
        self.assertEqual(n_before, n_after)
        tok_recs = [
            r
            for r in self.sb.long_term.records
            if "RCOgwKC7KIGGdHkkZT2cOEA6nLh" in (r.question + r.answer + str(r.facts))
        ]
        self.assertEqual(len(tok_recs), 1)
        self.assertIn("v2", tok_recs[0].answer)
        self.assertIn("公会客服翻译工具", tok_recs[0].question)

    def test_edit_question_by_id_does_not_create_new(self):
        self.sb.remember("原始问法", "答案不变", scene="general", tags=["t"])
        rid = self.sb.last_remembered.id
        n = len(self.sb.long_term.records)
        self.sb.remember(
            "我只是改一下问",
            "答案不变",
            scene="general",
            tags=["t"],
            memory_id=rid,
        )
        self.assertEqual(len(self.sb.long_term.records), n)
        self.assertEqual(self.sb.last_remembered.id, rid)
        self.assertTrue(self.sb.last_remembered_updated)
        # question_optimize 可能压缩口语尾巴，但须仍是原 id 且问法已变
        self.assertNotEqual(self.sb.last_remembered.question, "原始问法")
        self.assertTrue(
            "改" in self.sb.last_remembered.question
            or "我只是" in self.sb.last_remembered.question
        )

    def test_edit_question_by_original_question_no_new(self):
        self.sb.remember("打开弹窗时的问法", "同一答案正文", scene="general")
        rid = self.sb.last_remembered.id
        n = len(self.sb.long_term.records)
        self.sb.remember(
            "用户改过的新问法",
            "同一答案正文",
            scene="general",
            original_question="打开弹窗时的问法",
            update_only=True,
        )
        self.assertEqual(len(self.sb.long_term.records), n)
        self.assertEqual(self.sb.last_remembered.id, rid)
        self.assertTrue(self.sb.last_remembered_updated)

    def test_update_only_missing_raises(self):
        with self.assertRaises(ValueError):
            self.sb.remember(
                "不存在的旧问",
                "答",
                scene="general",
                original_question="根本没有这条",
                update_only=True,
            )
        self.assertEqual(len(self.sb.long_term.records), 0)

    def test_new_remember_still_creates(self):
        self.sb.remember("全新问题甲", "答案甲", scene="general")
        self.sb.remember("全新问题乙", "答案乙", scene="general")
        self.assertEqual(len(self.sb.long_term.records), 2)
        self.assertFalse(self.sb.last_remembered_updated)

    def test_feishu_chat_awaits_confirm_no_auto_long_term(self):
        url = "https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh"
        self.sb.config.feishu = FeishuConfig(
            enabled=True,
            app_id="cli_test",
            app_secret="secret",
            user_access_token="tok",
        )
        doc = FeishuFetchResult(
            url=url,
            ok=True,
            title="公会客服翻译工具",
            content="BatchGetTranslation 节流",
            document_id="docx1",
        )
        n_before = len(self.sb.long_term.records)
        with mock.patch(
            "core.feishu.fetch_feishu_docs_for_text",
            return_value=([doc], "正文"),
        ), mock.patch(
            "core.feishu.feishu_configured",
            return_value=True,
        ):
            r = self.sb.chat(f"{url} 整理技术要点")
        self.assertEqual(r.source, "llm")
        self.assertTrue((r.meta or {}).get("awaiting_confirm"))
        self.assertIn("公会客服翻译工具", (r.meta or {}).get("pending_question") or "")
        self.assertEqual(len(self.sb.long_term.records), n_before)


class WorkingReuseTests(unittest.TestCase):
    def test_feishu_url_skips_working_reuse(self):
        wm = WorkingMemory(chunk_size=7)
        q = "https://bytedance.larkoffice.com/wiki/QZJowpLhBiY58xkChx0chzTZn4f 分析文档"
        bad = "读不到该文档正文，无法提炼前端工作。user_access_token Invalid access token 99991668"
        wm.add_context(q, ["分析", "文档"], role="user")
        wm.add_context(bad, ["读不到"], role="assistant")
        self.assertTrue(is_non_reusable_answer(bad))
        self.assertIsNone(wm.local_match(q))


class FeishuQuestionRewriteTests(unittest.TestCase):
    def test_rewrite_uses_title_and_keeps_token(self):
        url = "https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh"
        q = (
            f"{url}, 读取记录该前端技术文档，该文档为客服的技术文档，"
            "整理归纳技术细节，为后续开发迭代做为技术储备"
        )
        doc = FeishuFetchResult(
            url=url,
            ok=True,
            title="客服二期前端技术文档",
            content="# 客服二期\nIM 接入",
            document_id="docxABC",
        )
        out = rewrite_feishu_memory_question(q, [doc])
        self.assertIn("客服二期前端技术文档", out)
        self.assertIn("RCOgwKC7KIGGdHkkZT2cOEA6nLh", out)
        self.assertNotIn("读取记录该前端技术文档", out)

    def test_placeholder_feishu_prefix_can_be_upgraded(self):
        """「飞书文档：口语」不得锁死；有真实标题时应升级为《标题》…"""
        url = "https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh"
        bad = f"飞书文档：这是另一个关于客服的前端技术文档 {url}"
        doc = FeishuFetchResult(
            url=url,
            ok=True,
            title="公会客服翻译工具",
            content="# 公会客服翻译工具\nBatchGetTranslation",
            document_id="docxXYZ",
        )
        out = rewrite_feishu_memory_question(bad, [doc], force=True)
        self.assertIn("《公会客服翻译工具》", out)
        self.assertNotIn("飞书文档：这是另一个", out)
        self.assertIn("RCOgwKC7KIGGdHkkZT2cOEA6nLh", out)

    def test_force_without_docs_keeps_bracket_title(self):
        url = "https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh"
        good = f"《[Ageny][FE Tech] 公会客服翻译工具》技术要点与储备 {url}"
        out = rewrite_feishu_memory_question(good, force=True)
        self.assertIn("公会客服翻译工具", out)
        self.assertNotIn("飞书文档：技术要点", out)


if __name__ == "__main__":
    unittest.main()
