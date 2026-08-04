"""飞书链接解析与工作记忆复用单测（无网络）。"""

import shutil
import tempfile
import unittest
import urllib.parse
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
    FeishuDocRef,
    FeishuFetchResult,
    create_docx_document,
    extract_feishu_tokens,
    extract_feishu_urls,
    markdown_to_docx_blocks,
    preview_docx_body,
    record_matches_feishu_tokens,
    update_docx_body,
)
from core.feishu_question import _compress_intent, rewrite_feishu_memory_question
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

    def test_troubleshooting_note_stays_reusable(self):
        """排障笔记会引用错误码，但带修复结论，不能按失败回显丢弃。"""
        note = (
            "根因：core/feishu.py 的强刷条件只匹配 99991668/Invalid access token，"
            "漏了 99991677 token expired，所以过期时兜底重试不触发。"
            "修法：抽出 _is_user_token_error() 同时匹配两类错误码与 token expired 文本，"
            "并把续期失败原因并进最终 error，避免 except Exception: pass 吞掉线索。"
        )
        self.assertFalse(is_non_reusable_answer(note))

    def test_long_error_dump_still_non_reusable(self):
        """长错误回显没有修复结论，仍应判为不可复用。"""
        dump = (
            "飞书文档：读取失败：user_access_token: HTTP 401: "
            '{"code":99991677,"msg":"Authentication token expired. Please request a new one.",'
            '"error":{"log_id":"20260804114539C660ED7CAB28E517226C"}}；'
            "tenant_access_token: HTTP 400: 读不到该文档；"
            "个人文档请运行 python3 scripts/feishu_login.py 重新授权"
        )
        self.assertTrue(is_non_reusable_answer(dump))


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


class FeishuWriteScopeTests(unittest.TestCase):
    def test_merged_scopes_adds_write_scope_to_stale_config(self):
        """旧配置会覆盖 DEFAULT_SCOPES，必须取并集才能带上新增写权限。"""
        from core.feishu_oauth import _merged_scopes

        cfg = FeishuConfig(
            app_id="cli_x",
            oauth_scope="offline_access docs:document.content:read wiki:node:read",
        )
        merged = _merged_scopes(cfg).split()
        self.assertIn("wiki:node:update", merged)
        self.assertIn("offline_access", merged)
        # 并集不应出现重复项
        self.assertEqual(len(merged), len(set(merged)))

    def test_authorize_url_carries_write_scope(self):
        from core.feishu_oauth import build_authorize_url

        cfg = FeishuConfig(app_id="cli_x", oauth_scope="wiki:node:read")
        self.assertIn("wiki%3Anode%3Aupdate", build_authorize_url(cfg))

    def test_docx_scopes_are_the_granular_ones(self):
        """后台只有 create/readonly/write_only 三项，请求聚合名 docx:document 会报 20027。"""
        from core.feishu_oauth import _merged_scopes

        merged = _merged_scopes(FeishuConfig(app_id="cli_x")).split()
        self.assertIn("docx:document:create", merged)
        self.assertIn("docx:document:readonly", merged)
        self.assertIn("docx:document:write_only", merged)
        self.assertNotIn("docx:document", merged)

    def test_retired_scope_dropped_from_stale_config(self):
        """旧配置里残留的 docx:document 会让整个授权页失败，必须剔掉而不是并进去。"""
        from core.feishu_oauth import _merged_scopes

        cfg = FeishuConfig(
            app_id="cli_x", oauth_scope="offline_access docx:document"
        )
        merged = _merged_scopes(cfg).split()
        self.assertNotIn("docx:document", merged)
        self.assertIn("docx:document:write_only", merged)

    def test_authorize_url_has_no_retired_scope(self):
        from core.feishu_oauth import build_authorize_url

        cfg = FeishuConfig(app_id="cli_x", oauth_scope="docx:document")
        url = build_authorize_url(cfg)
        scope = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["scope"][0]
        self.assertNotIn("docx:document", scope.split())
        self.assertIn("docx:document:create", scope.split())


class FeishuTitleUpdateTests(unittest.TestCase):
    def _cfg(self):
        return FeishuConfig(enabled=True, app_id="cli_x", app_secret="s")

    def _wiki_ref(self):
        return FeishuDocRef(
            url="https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh",
            kind="wiki",
            token="RCOgwKC7KIGGdHkkZT2cOEA6nLh",
        )

    def test_refuses_without_explicit_confirmation(self):
        """约定：改飞书文档必须本人逐次确认，未确认时默认拒绝、不发请求。"""
        from core.feishu import update_wiki_node_title

        with mock.patch("core.feishu._http_json") as http:
            res = update_wiki_node_title(self._cfg(), self._wiki_ref(), "新标题")
        self.assertFalse(res.ok)
        self.assertIn("确认", res.error)
        http.assert_not_called()

    def test_rejects_non_wiki_link(self):
        """docx 直链没有 space_id/node_token，改标题走不通，应提前拦住。"""
        from core.feishu import update_wiki_node_title

        ref = FeishuDocRef(
            url="https://foo.feishu.cn/docx/AbCdEfGh1234567", kind="docx", token="AbCdEfGh1234567"
        )
        res = update_wiki_node_title(self._cfg(), ref, "新标题", confirmed=True)
        self.assertFalse(res.ok)
        self.assertIn("wiki", res.error)

    def test_rejects_empty_title(self):
        from core.feishu import update_wiki_node_title

        res = update_wiki_node_title(self._cfg(), self._wiki_ref(), "   ", confirmed=True)
        self.assertFalse(res.ok)
        self.assertIn("不能为空", res.error)


class MarkdownToBlocksTests(unittest.TestCase):
    def _kinds(self, blocks):
        return [b["block_type"] for b in blocks]

    def _content(self, block):
        field = [k for k in block if k != "block_type"][0]
        return block[field]["elements"][0]["text_run"]["content"]

    def test_headings_lists_and_paragraph(self):
        blocks = markdown_to_docx_blocks(
            "# 标题一\n## 标题二\n普通段落\n- 无序项\n1. 有序项\n> 引用\n---"
        )
        # 3/4=heading1/2，2=text，12=bullet，13=ordered，15=quote，22=divider
        self.assertEqual(self._kinds(blocks), [3, 4, 2, 12, 13, 15, 22])
        self.assertEqual(self._content(blocks[0]), "标题一")
        self.assertEqual(self._content(blocks[3]), "无序项")
        self.assertEqual(self._content(blocks[5]), "引用")

    def test_block_field_matches_type(self):
        """block_type 与 BlockData 字段名必须对应，否则接口报 1770006。"""
        blocks = markdown_to_docx_blocks("### 三级标题")
        self.assertEqual(blocks[0]["block_type"], 5)
        self.assertIn("heading3", blocks[0])

    def test_code_fence_keeps_indent_as_one_block(self):
        blocks = markdown_to_docx_blocks("说明\n```python\ndef f():\n    return 1\n```")
        self.assertEqual(self._kinds(blocks), [2, 14])
        self.assertEqual(self._content(blocks[1]), "def f():\n    return 1")

    def test_unclosed_fence_still_keeps_content(self):
        blocks = markdown_to_docx_blocks("```\nls -al")
        self.assertEqual(self._kinds(blocks), [14])
        self.assertEqual(self._content(blocks[0]), "ls -al")

    def test_blank_lines_dropped(self):
        self.assertEqual(markdown_to_docx_blocks("\n\n  \n"), [])


class FeishuCreateDocTests(unittest.TestCase):
    def _cfg(self, **kw):
        return FeishuConfig(enabled=True, app_id="cli_x", app_secret="s", **kw)

    def test_refuses_without_explicit_confirmation(self):
        """约定同改标题：新建文档也必须本人逐次确认，未确认时不发请求。"""
        with mock.patch("core.feishu._http_json") as http:
            res = create_docx_document(self._cfg(), "新文档")
        self.assertFalse(res.ok)
        self.assertIn("确认", res.error)
        http.assert_not_called()

    def test_rejects_empty_title(self):
        with mock.patch("core.feishu._http_json") as http:
            res = create_docx_document(self._cfg(), "  ", confirmed=True)
        self.assertFalse(res.ok)
        self.assertIn("不能为空", res.error)
        http.assert_not_called()

    def test_creates_and_writes_body_in_batches(self):
        """写块接口单次上限 50，超出必须分批，否则整批被拒。"""
        calls = []

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            calls.append((url, body))
            if url.endswith("/docx/v1/documents"):
                return {"code": 0, "data": {"document": {"document_id": "doc123"}}}
            return {"code": 0, "data": {}}

        content = "\n".join(f"第 {i} 行" for i in range(120))
        with mock.patch("core.feishu._http_json", side_effect=fake_http), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-tok"
        ), mock.patch("core.feishu.time.sleep"):
            res = create_docx_document(
                self._cfg(doc_host="bytedance.larkoffice.com"),
                "标题",
                content=content,
                confirmed=True,
            )

        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.blocks_written, 120)
        self.assertEqual(res.url, "https://bytedance.larkoffice.com/docx/doc123")
        child_calls = [b for u, b in calls if "/children" in u]
        self.assertEqual([len(c["children"]) for c in child_calls], [50, 50, 20])
        self.assertTrue(all(c["index"] == -1 for c in child_calls))

    def test_body_failure_still_reports_created_doc(self):
        """正文写挂了文档已经建出来，必须把 id 带回去，否则用户不知道要清理。"""

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            if url.endswith("/docx/v1/documents"):
                return {"code": 0, "data": {"document": {"document_id": "doc999"}}}
            return {"code": 1770040, "msg": "no folder permission"}

        with mock.patch("core.feishu._http_json", side_effect=fake_http), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-tok"
        ):
            res = create_docx_document(
                self._cfg(), "标题", content="正文", confirmed=True
            )

        self.assertFalse(res.ok)
        self.assertEqual(res.document_id, "doc999")
        self.assertIn("已创建", res.error)
        self.assertIn("docx:document:write_only", res.error)


class FeishuEditBodyTests(unittest.TestCase):
    def _cfg(self):
        return FeishuConfig(enabled=True, app_id="cli_x", app_secret="s")

    def _docx_ref(self):
        return FeishuDocRef(
            url="https://foo.feishu.cn/docx/AbCdEfGh1234567",
            kind="docx",
            token="AbCdEfGh1234567",
        )

    def _wiki_ref(self):
        return FeishuDocRef(
            url="https://bytedance.larkoffice.com/wiki/RCOgwKC7KIGGdHkkZT2cOEA6nLh",
            kind="wiki",
            token="RCOgwKC7KIGGdHkkZT2cOEA6nLh",
        )

    def _fake_http(self, calls, *, children=0):
        """假 docx 接口：children 决定文档现有块数。"""

        def handler(method, url, *, headers=None, body=None, timeout=30.0):
            calls.append((method, url, body))
            if "wiki/v2/spaces/get_node" in url:
                return {
                    "code": 0,
                    "data": {"node": {"obj_token": "docFromWiki", "title": "原标题"}},
                }
            if method == "GET" and url.endswith("/documents/AbCdEfGh1234567"):
                return {"code": 0, "data": {"document": {"title": "原标题"}}}
            if method == "GET" and "/children?" in url:
                items = [{"block_id": f"b{i}"} for i in range(children)]
                return {"code": 0, "data": {"items": items, "has_more": False}}
            return {"code": 0, "data": {}}

        return handler

    def test_refuses_without_explicit_confirmation(self):
        """改正文同样默认拒绝，未确认时不发请求。"""
        with mock.patch("core.feishu._http_json") as http:
            res = update_docx_body(self._cfg(), self._docx_ref(), "新正文")
        self.assertFalse(res.ok)
        self.assertIn("确认", res.error)
        http.assert_not_called()

    def test_rejects_empty_content_even_in_replace(self):
        """replace 传空会清空文档，这种破坏不该由「正文恰好为空」触发。"""
        with mock.patch("core.feishu._http_json") as http:
            res = update_docx_body(
                self._cfg(), self._docx_ref(), "   \n\n", mode="replace", confirmed=True
            )
        self.assertFalse(res.ok)
        self.assertIn("为空", res.error)
        http.assert_not_called()

    def test_rejects_unknown_mode(self):
        with mock.patch("core.feishu._http_json") as http:
            res = update_docx_body(
                self._cfg(), self._docx_ref(), "正文", mode="overwrite", confirmed=True
            )
        self.assertFalse(res.ok)
        self.assertIn("overwrite", res.error)
        http.assert_not_called()

    def test_append_does_not_delete(self):
        calls = []
        with mock.patch(
            "core.feishu._http_json", side_effect=self._fake_http(calls, children=7)
        ), mock.patch("core.feishu_oauth.ensure_user_access_token", return_value="u-t"):
            res = update_docx_body(
                self._cfg(), self._docx_ref(), "# 标题\n段落", confirmed=True
            )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.blocks_written, 2)
        self.assertEqual(res.blocks_deleted, 0)
        self.assertFalse([c for c in calls if c[0] == "DELETE"])

    def test_replace_deletes_before_writing(self):
        calls = []
        with mock.patch(
            "core.feishu._http_json", side_effect=self._fake_http(calls, children=3)
        ), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-t"
        ), mock.patch("core.feishu.time.sleep"):
            res = update_docx_body(
                self._cfg(), self._docx_ref(), "新正文", mode="replace", confirmed=True
            )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.blocks_deleted, 3)
        self.assertEqual(res.blocks_written, 1)
        methods = [m for m, _u, _b in calls if m in {"DELETE", "POST"}]
        self.assertEqual(methods, ["DELETE", "POST"])
        delete_body = [b for m, _u, b in calls if m == "DELETE"][0]
        # 区间左闭右开，删 3 块是 [0, 3)
        self.assertEqual(delete_body, {"start_index": 0, "end_index": 3})

    def test_replace_on_empty_doc_skips_delete(self):
        calls = []
        with mock.patch(
            "core.feishu._http_json", side_effect=self._fake_http(calls, children=0)
        ), mock.patch("core.feishu_oauth.ensure_user_access_token", return_value="u-t"):
            res = update_docx_body(
                self._cfg(), self._docx_ref(), "新正文", mode="replace", confirmed=True
            )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.blocks_deleted, 0)
        self.assertFalse([c for c in calls if c[0] == "DELETE"])

    def test_replace_deletes_in_batches_of_50(self):
        """删除也按 50 分批；每轮都删最前面一批，因为删完后面的块会前移。"""
        calls = []
        with mock.patch(
            "core.feishu._http_json", side_effect=self._fake_http(calls, children=120)
        ), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-t"
        ), mock.patch("core.feishu.time.sleep"):
            res = update_docx_body(
                self._cfg(), self._docx_ref(), "新正文", mode="replace", confirmed=True
            )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.blocks_deleted, 120)
        deletes = [b for m, _u, b in calls if m == "DELETE"]
        self.assertEqual(
            deletes,
            [
                {"start_index": 0, "end_index": 50},
                {"start_index": 0, "end_index": 50},
                {"start_index": 0, "end_index": 20},
            ],
        )

    def test_wiki_link_resolved_to_obj_token(self):
        """wiki 链接没有 document_id，得先 get_node 换成 obj_token。"""
        calls = []
        with mock.patch(
            "core.feishu._http_json", side_effect=self._fake_http(calls, children=0)
        ), mock.patch("core.feishu_oauth.ensure_user_access_token", return_value="u-t"):
            res = update_docx_body(
                self._cfg(), self._wiki_ref(), "正文", confirmed=True
            )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.document_id, "docFromWiki")
        self.assertTrue(any("docFromWiki" in u for _m, u, _b in calls))

    def test_write_failure_after_delete_mentions_history(self):
        """删完才写挂，原文已经没了，必须提示能用历史版本恢复。"""

        def handler(method, url, *, headers=None, body=None, timeout=30.0):
            if method == "GET" and "/children?" in url:
                return {
                    "code": 0,
                    "data": {"items": [{"block_id": "b0"}], "has_more": False},
                }
            if method == "GET":
                return {"code": 0, "data": {"document": {"title": "原标题"}}}
            if method == "DELETE":
                return {"code": 0, "data": {}}
            return {"code": 1770032, "msg": "forbidden"}

        with mock.patch("core.feishu._http_json", side_effect=handler), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-t"
        ):
            res = update_docx_body(
                self._cfg(), self._docx_ref(), "新正文", mode="replace", confirmed=True
            )
        self.assertFalse(res.ok)
        self.assertEqual(res.blocks_deleted, 1)
        self.assertIn("历史版本", res.error)

    def test_preview_is_read_only(self):
        """预览用于确认前看清目标，不该发任何写请求。"""
        calls = []
        with mock.patch(
            "core.feishu._http_json", side_effect=self._fake_http(calls, children=5)
        ), mock.patch("core.feishu_oauth.ensure_user_access_token", return_value="u-t"):
            pre = preview_docx_body(self._cfg(), self._wiki_ref())
        self.assertTrue(pre.ok, pre.error)
        self.assertEqual(pre.block_count, 5)
        self.assertEqual(pre.title, "原标题")
        self.assertEqual(pre.document_id, "docFromWiki")
        self.assertTrue(all(m == "GET" for m, _u, _b in calls))


class CompressIntentTests(unittest.TestCase):
    def test_proper_nouns_survive_truncation(self):
        """专有主题词不在 _INTENT_KEEP 词表里，不能被压成只剩「方案」。"""
        intent = (
            "backstage 全站 zIndex 号段划分、stylelint 插件与存量 baseline "
            "收缩机制的完整落地方案"
        )
        out = _compress_intent(intent, "")
        self.assertIn("zIndex", out)
        self.assertIn("backstage", out)
        self.assertNotEqual(out, "方案")

    def test_boilerplate_still_compressed_to_keywords(self):
        """纯套话堆砌仍应压成词表关键词，避免问句里全是废话。"""
        intent = "这份文档讲了前端架构与后端接入的方案，还有工单、IM 的配置和部署踩坑，以及技术细节总结要点"
        out = _compress_intent(intent, "")
        self.assertIn("、", out)
        self.assertLess(len(out), len(intent))
        self.assertNotIn("这份文档讲了", out)

    def test_truncation_leaves_no_dangling_bracket(self):
        intent = "backstage zIndex 层级治理方案（【FE Tech】飞书文档 SQH5wsKsSiCSqlk3bSRccsyAnIb）"
        out = _compress_intent(intent, "")
        self.assertEqual(out, "backstage zIndex 层级治理方案")


if __name__ == "__main__":
    unittest.main()
