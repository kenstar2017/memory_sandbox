"""飞书链接解析与工作记忆复用单测（无网络）。"""

import json
import shutil
import tempfile
import time
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

import yaml

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


class FileSubscribeTests(unittest.TestCase):
    """按文件订阅：用户维度订阅只在「你本人收到通知」时推，自己发的评论永远不触发。"""

    def setUp(self):
        self.cfg = FeishuConfig(enabled=True, app_id="cli_x", app_secret="s")
        self.ref = FeishuDocRef(
            url="https://x.larkoffice.com/wiki/WikiTok1", kind="wiki", token="WikiTok1"
        )

    def _run(self, responses):
        """responses: 按调用顺序返回，值为 dict 或要抛的异常。"""
        calls = []

        def fake_http(method, url, **kw):
            calls.append((method, url, kw.get("headers", {}).get("Authorization", "")))
            got = responses.pop(0)
            if isinstance(got, Exception):
                raise got
            return got

        with mock.patch("core.feishu._http_json", side_effect=fake_http), mock.patch(
            "core.feishu._with_user_token", lambda c, p, step: ("u-tok", step("u-tok"))
        ), mock.patch("core.feishu._resolve_document_id", return_value=("DocxTok1", "标题")):
            from core.feishu import subscribe_file_events

            res = subscribe_file_events(self.cfg, self.ref)
        return res, calls

    def test_prefers_the_app_identity(self):
        """应用身份订阅之后谁评论都推，包括自己发的——所以要先试它。"""
        with mock.patch("core.feishu._tenant_access_token", return_value="t-tok"):
            res, calls = self._run([{"code": 0}])
        self.assertTrue(res.ok)
        self.assertEqual(res.identity, "tenant")
        self.assertEqual(res.document_id, "DocxTok1")
        self.assertIn("Bearer t-tok", calls[0][2])

    def test_resolves_a_wiki_link_to_its_docx_token(self):
        """wiki token 不能直接喂给订阅接口。"""
        with mock.patch("core.feishu._tenant_access_token", return_value="t-tok"):
            _res, calls = self._run([{"code": 0}])
        self.assertIn("/files/DocxTok1/subscribe", calls[0][1])
        self.assertNotIn("WikiTok1/subscribe", calls[0][1])

    def test_falls_back_to_the_user_identity(self):
        with mock.patch("core.feishu._tenant_access_token", return_value="t-tok"):
            res, calls = self._run([{"code": 99991672, "msg": "denied"}, {"code": 0}])
        self.assertTrue(res.ok)
        self.assertEqual(res.identity, "user")
        self.assertIn("Bearer u-tok", calls[1][2])

    def test_missing_app_scope_is_spelled_out(self):
        with mock.patch("core.feishu._tenant_access_token", return_value="t-tok"):
            res, _ = self._run(
                [{"code": 99991672, "msg": "denied"}, {"code": 1061045, "msg": "nope"}]
            )
        self.assertFalse(res.ok)
        self.assertIn("docs:event:subscribe", res.error)
        self.assertIn("自己发的评论", res.error)

    def test_unconfigured_feishu_does_not_call_out(self):
        from core.feishu import subscribe_file_events

        res = subscribe_file_events(FeishuConfig(enabled=True), self.ref)
        self.assertFalse(res.ok)
        self.assertIn("app_id", res.error)


class SharedRefreshTokenTests(unittest.TestCase):
    """App / MCP / 机器人共用一份配置，而 refresh_token 是一次性的。

    谁先刷新，其它常驻进程内存里那个就作废了，再拿去刷只会得到 invalid_grant，
    于是那个进程一直失效到重启为止（机器人表现为「评论事件到了但读评论失败」）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="oauth_race_")
        self.path = Path(self.tmp) / "config.yaml"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write_disk(self, **feishu):
        self.path.write_text(
            yaml.safe_dump({"feishu": feishu}, allow_unicode=True), encoding="utf-8"
        )

    def _stale(self):
        return FeishuConfig(
            app_id="cli_x",
            app_secret="s",
            user_access_token="old-access",
            refresh_token="old-refresh",
            user_token_expires_at=int(time.time()) - 10,
        )

    def test_picks_up_the_token_another_process_just_wrote(self):
        from core.feishu_oauth import ensure_user_access_token

        self._write_disk(
            user_access_token="fresh-access",
            refresh_token="fresh-refresh",
            user_token_expires_at=int(time.time()) + 3600,
        )
        cfg = self._stale()
        with mock.patch("core.feishu_oauth.refresh_user_access_token") as refresh:
            got = ensure_user_access_token(cfg, config_path=str(self.path))
        self.assertEqual(got, "fresh-access")
        refresh.assert_not_called()  # 别再去烧那个已经作废的 refresh_token

    def test_uses_the_newest_refresh_token_when_the_disk_one_also_expired(self):
        from core.feishu_oauth import ensure_user_access_token

        self._write_disk(
            user_access_token="also-old",
            refresh_token="fresh-refresh",
            user_token_expires_at=int(time.time()) - 5,
        )
        cfg = self._stale()
        seen = {}

        def fake_refresh(c):
            seen["refresh"] = c.refresh_token
            return {"access_token": "brand-new", "refresh_token": "r2", "expires_in": 7200}

        # persist_feishu_auth 会无视 config_path 强行写用户目录（防密钥进 git），
        # 单测不能让它去动真配置
        with mock.patch("core.feishu_oauth.refresh_user_access_token", fake_refresh), mock.patch(
            "core.feishu_oauth.persist_feishu_auth"
        ):
            got = ensure_user_access_token(cfg, config_path=str(self.path))
        self.assertEqual(seen["refresh"], "fresh-refresh")
        self.assertEqual(got, "brand-new")

    def test_a_refresh_lost_by_a_hair_falls_back_to_disk(self):
        """就在我们要刷的那一瞬间被别的进程抢先了。"""
        from core.feishu_oauth import ensure_user_access_token

        cfg = self._stale()

        def fake_refresh(c):
            self._write_disk(
                user_access_token="winner-access",
                refresh_token="winner-refresh",
                user_token_expires_at=int(time.time()) + 3600,
            )
            raise RuntimeError("刷新 user_access_token 失败：invalid_grant")

        with mock.patch("core.feishu_oauth.refresh_user_access_token", fake_refresh):
            got = ensure_user_access_token(cfg, config_path=str(self.path))
        self.assertEqual(got, "winner-access")

    def test_a_genuine_failure_still_raises(self):
        from core.feishu_oauth import ensure_user_access_token

        cfg = self._stale()
        with mock.patch(
            "core.feishu_oauth.refresh_user_access_token",
            side_effect=RuntimeError("刷新 user_access_token 失败：invalid_grant"),
        ):
            with self.assertRaises(RuntimeError):
                ensure_user_access_token(cfg, config_path=str(self.path))


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

    def test_comment_and_event_scopes_reach_stale_configs(self):
        """
        文档评论机器人要的四项权限必须并进旧配置。

        踩过：docs:event:subscribe 当初只加在 FeishuConfig.oauth_scope 的默认值上，
        而存过配置的机器会用自己那份旧 scope 覆盖默认值，于是重跑授权也不请求它，
        订阅评论事件时才报权限不足。
        """
        from core.feishu_oauth import _merged_scopes

        cfg = FeishuConfig(app_id="cli_x", oauth_scope="offline_access wiki:node:read")
        merged = _merged_scopes(cfg).split()
        self.assertIn("docs:document.comment:read", merged)
        self.assertIn("docs:document.comment:create", merged)
        self.assertIn("docs:event:subscribe", merged)
        self.assertIn("docs:document.subscription", merged)

    def test_retired_scope_dropped_from_stale_config(self):
        """旧配置里残留的 docx:document 会让整个授权页失败，必须剔掉而不是并进去。"""
        from core.feishu_oauth import _merged_scopes

        cfg = FeishuConfig(
            app_id="cli_x", oauth_scope="offline_access docx:document"
        )
        merged = _merged_scopes(cfg).split()
        self.assertNotIn("docx:document", merged)
        self.assertIn("docx:document:write_only", merged)

    def test_missing_granted_scopes_detects_unapproved(self):
        """需审核权限没批下来时，换票响应的 scope 里就没有它，应能提前报出来。"""
        from core.feishu_oauth import missing_granted_scopes

        cfg = FeishuConfig(app_id="cli_x", oauth_scope="")
        payload = {
            "access_token": "u-x",
            "scope": (
                "offline_access docs:document.content:read wiki:wiki:readonly "
                "wiki:node:read wiki:node:update docx:document:create "
                "docx:document:readonly "
                "docs:document.comment:read docs:document.comment:create "
                "docs:event:subscribe docs:document.subscription "
                "drive:drive.metadata:readonly im:message:readonly "
                "board:whiteboard:node:read board:whiteboard:node:create"
            ),
        }
        self.assertEqual(
            missing_granted_scopes(cfg, payload), ["docx:document:write_only"]
        )

    def test_missing_granted_scopes_silent_without_scope_field(self):
        """响应不带 scope 时无法判断，不能当成全部未授予。"""
        from core.feishu_oauth import missing_granted_scopes

        cfg = FeishuConfig(app_id="cli_x")
        self.assertEqual(missing_granted_scopes(cfg, {"access_token": "u-x"}), [])

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

    # ---- 行内样式 ----

    def _elements(self, block):
        field = [k for k in block if k != "block_type"][0]
        return block[field]["elements"]

    def _styled(self, block):
        """[(文本, 样式标记)]，便于断言。"""
        out = []
        for e in self._elements(block):
            run = e["text_run"]
            st = run.get("text_element_style") or {}
            flags = tuple(sorted(k for k, v in st.items() if v is True))
            out.append((run["content"], flags))
        return out

    def test_bold_becomes_style_not_literal_asterisks(self):
        """之前 **粗体** 会原样写成字面量。"""
        blocks = markdown_to_docx_blocks("请**务必**确认")
        self.assertEqual(
            self._styled(blocks[0]),
            [("请", ()), ("务必", ("bold",)), ("确认", ())],
        )

    def test_italic_strike_and_inline_code(self):
        blocks = markdown_to_docx_blocks("*斜* ~~删~~ `co de`")
        styled = self._styled(blocks[0])
        self.assertIn(("斜", ("italic",)), styled)
        self.assertIn(("删", ("strikethrough",)), styled)
        self.assertIn(("co de", ("inline_code",)), styled)

    def test_underscore_bold_and_italic(self):
        self.assertIn(("粗", ("bold",)), self._styled(markdown_to_docx_blocks("__粗__")[0]))
        self.assertIn(("斜", ("italic",)), self._styled(markdown_to_docx_blocks("_斜_")[0]))

    def test_link_url_is_percent_encoded(self):
        """飞书要求 link.url 做 url_encode（: 与 / 也要转义），否则链接打不开。"""
        blocks = markdown_to_docx_blocks("见 [开放平台](https://open.feishu.cn/)")
        link_el = [
            e
            for e in self._elements(blocks[0])
            if (e["text_run"].get("text_element_style") or {}).get("link")
        ][0]
        self.assertEqual(link_el["text_run"]["content"], "开放平台")
        self.assertEqual(
            link_el["text_run"]["text_element_style"]["link"]["url"],
            "https%3A%2F%2Fopen.feishu.cn%2F",
        )

    def test_bold_inside_link_keeps_both(self):
        blocks = markdown_to_docx_blocks("[**重要**](https://a.b)")
        run = self._elements(blocks[0])[0]["text_run"]
        self.assertEqual(run["content"], "重要")
        self.assertTrue(run["text_element_style"]["bold"])
        self.assertIn("link", run["text_element_style"])

    def test_inline_code_content_not_parsed_further(self):
        """代码里的星号是字面量，不该再当粗体。"""
        blocks = markdown_to_docx_blocks("`a ** b`")
        self.assertEqual(self._styled(blocks[0]), [("a ** b", ("inline_code",))])

    def test_code_fence_body_keeps_markdown_literally(self):
        blocks = markdown_to_docx_blocks("```\n**不是粗体** | 不是表格\n```")
        self.assertEqual(self._styled(blocks[0]), [("**不是粗体** | 不是表格", ())])

    def test_plain_text_has_no_style_key(self):
        """没样式时不要塞空 style，免得改变既有输出。"""
        run = self._elements(markdown_to_docx_blocks("纯文本")[0])[0]["text_run"]
        self.assertNotIn("text_element_style", run)

    def test_multiplication_asterisks_not_treated_as_italic(self):
        blocks = markdown_to_docx_blocks("2 * 3 * 4 的结果")
        self.assertEqual(self._styled(blocks[0]), [("2 * 3 * 4 的结果", ())])

    def test_snake_case_word_not_italic(self):
        """some_var_name 里的下划线不是斜体标记。"""
        blocks = markdown_to_docx_blocks("变量 some_var_name 用法")
        self.assertEqual(self._styled(blocks[0]), [("变量 some_var_name 用法", ())])

    # ---- 表格 ----

    def test_pipe_table_becomes_table_block(self):
        """之前表格会退化成一堆竖线的普通段落。"""
        from core.feishu import _TABLE_CELLS

        md = "| 能力 | 权限 |\n|------|------|\n| 读 | read |\n| 写 | write |"
        blocks = markdown_to_docx_blocks(md)
        self.assertEqual([b["block_type"] for b in blocks], [31])
        prop = blocks[0]["table"]["property"]
        self.assertEqual((prop["row_size"], prop["column_size"]), (3, 2))
        self.assertTrue(prop["header_row"])
        self.assertEqual(
            blocks[0][_TABLE_CELLS],
            [["能力", "权限"], ["读", "read"], ["写", "write"]],
        )

    def test_table_alignment_separator_accepted(self):
        md = "| a | b | c |\n|:---|:---:|---:|\n| 1 | 2 | 3 |"
        blocks = markdown_to_docx_blocks(md)
        self.assertEqual([b["block_type"] for b in blocks], [31])

    def test_ragged_row_padded_to_column_count(self):
        from core.feishu import _TABLE_CELLS

        md = "| a | b | c |\n|---|---|---|\n| 1 |"
        blocks = markdown_to_docx_blocks(md)
        self.assertEqual(blocks[0][_TABLE_CELLS][1], ["1", "", ""])

    def test_escaped_pipe_stays_in_cell(self):
        from core.feishu import _TABLE_CELLS

        md = "| 表达式 | 说明 |\n|---|---|\n| a \\| b | 或 |"
        blocks = markdown_to_docx_blocks(md)
        self.assertEqual(blocks[0][_TABLE_CELLS][1], ["a | b", "或"])

    def test_pipe_line_without_separator_stays_paragraph(self):
        """正文里偶然出现竖线不该被当成表格。"""
        blocks = markdown_to_docx_blocks("| 这只是一行文字 |")
        self.assertEqual([b["block_type"] for b in blocks], [2])

    def test_table_mixed_with_other_blocks_keeps_order(self):
        md = "## 标题\n| a |\n|---|\n| 1 |\n结尾段落"
        blocks = markdown_to_docx_blocks(md)
        self.assertEqual([b["block_type"] for b in blocks], [4, 31, 2])

    def test_divider_still_works_after_table_support(self):
        """--- 分割线和表格分隔行长得像，不能互相误判。"""
        blocks = markdown_to_docx_blocks("上\n\n---\n\n下")
        self.assertEqual([b["block_type"] for b in blocks], [2, 22, 2])

    def test_table_cells_parse_inline_styles(self):
        from core.feishu import _table_descendants

        md = "| 能力 |\n|---|\n| **必须**做 |"
        blocks = markdown_to_docx_blocks(md)
        payload = _table_descendants(blocks[0], 0)
        texts = [d for d in payload["descendants"] if d["block_type"] == 2]
        styled = [
            (
                e["text_run"]["content"],
                bool((e["text_run"].get("text_element_style") or {}).get("bold")),
            )
            for e in texts[1]["text"]["elements"]
        ]
        self.assertEqual(styled, [("必须", True), ("做", False)])


class TableColumnWidthTests(unittest.TestCase):
    """
    不给 column_width，飞书会用偏小的固定默认值平分，
    长路径会被挤成一列一个字。
    """

    def _prop(self, md):
        blocks = markdown_to_docx_blocks(md)
        return blocks[0]["table"]["property"]

    def test_widths_cover_every_column_and_fill_page(self):
        from core.feishu import _TABLE_TOTAL_WIDTH

        prop = self._prop("| a | b | c |\n|---|---|---|\n| 1 | 2 | 3 |")
        self.assertEqual(len(prop["column_width"]), prop["column_size"])
        self.assertEqual(sum(prop["column_width"]), _TABLE_TOTAL_WIDTH)

    def test_widths_are_ints(self):
        """接口要 array(int)，float 会被拒。"""
        prop = self._prop("| a | b |\n|---|---|\n| 1 | 2 |")
        for w in prop["column_width"]:
            self.assertIsInstance(w, int)

    def test_long_column_gets_more_room_than_short_one(self):
        md = (
            "| 位置 | 风险 |\n|---|---|\n"
            "| `modules/account/src/pages/Settlement/index.tsx` | 低 |"
        )
        first, second = self._prop(md)["column_width"]
        self.assertGreater(first, second)

    def test_never_below_api_minimum(self):
        """接口下限 50px，我们自己再保底到 100px。"""
        from core.feishu import _TABLE_MIN_WIDTH

        md = "| a | 很长的一列内容需要占掉大部分宽度所以另一列会被压缩 |\n|---|---|\n| 1 | x |"
        for w in self._prop(md)["column_width"]:
            self.assertGreaterEqual(w, _TABLE_MIN_WIDTH)

    def test_cjk_counted_as_double_width(self):
        """同样字数的中文占两倍显示宽度；短内容会双双撞到保底值，所以要够长。"""
        from core.feishu import _column_widths

        cjk, ascii_ = _column_widths([["中文占两格需要更宽的列", "abcdefghijk"]])
        self.assertGreater(cjk, ascii_)

    def test_markup_does_not_inflate_width(self):
        """**粗体** 的星号渲染后不存在，不该占宽度。"""
        from core.feishu import _column_widths

        styled = _column_widths([["**粗体**", "x"]])
        plain = _column_widths([["粗体", "x"]])
        self.assertEqual(styled, plain)

    def test_many_columns_keep_minimum_and_allow_scroll(self):
        from core.feishu import _TABLE_MIN_WIDTH, _column_widths

        widths = _column_widths([["c"] * 10])
        self.assertEqual(widths, [_TABLE_MIN_WIDTH] * 10)

    def test_ragged_table_still_gets_widths(self):
        prop = self._prop("| a | b | c |\n|---|---|---|\n| 1 |")
        self.assertEqual(len(prop["column_width"]), 3)


class TableDescendantPayloadTests(unittest.TestCase):
    """表格必须走「创建嵌套块」接口，平铺的 children 接口建不出三层结构。"""

    def _payload(self, md):
        from core.feishu import _table_descendants

        blocks = markdown_to_docx_blocks(md)
        table = [b for b in blocks if b["block_type"] == 31][0]
        return _table_descendants(table, 7)

    def test_payload_shape(self):
        p = self._payload("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertEqual(p["children_id"], ["t7"])
        table = p["descendants"][0]
        self.assertEqual(table["block_id"], "t7")
        self.assertEqual(table["block_type"], 31)
        # 2 行 * 2 列 = 4 个单元格，各带一个文本子块
        self.assertEqual(len(table["children"]), 4)
        cells = [d for d in p["descendants"] if d["block_type"] == 32]
        texts = [d for d in p["descendants"] if d["block_type"] == 2]
        self.assertEqual((len(cells), len(texts)), (4, 4))
        for cell in cells:
            self.assertEqual(len(cell["children"]), 1)
            self.assertIn(cell["children"][0], {t["block_id"] for t in texts})

    def test_cell_ids_are_unique_across_tables(self):
        """同一次写入有多张表时临时 id 不能撞。"""
        from core.feishu import _table_descendants

        md = "| a |\n|---|\n| 1 |\n\n文字\n\n| b |\n|---|\n| 2 |"
        blocks = markdown_to_docx_blocks(md)
        tables = [(i, b) for i, b in enumerate(blocks) if b["block_type"] == 31]
        ids = []
        for i, t in tables:
            ids += [d["block_id"] for d in _table_descendants(t, i)["descendants"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_empty_cell_still_gets_text_child(self):
        """空单元格也必须挂文本子块，否则接口报错。"""
        p = self._payload("| a | b |\n|---|---|\n| 1 | |")
        cells = [d for d in p["descendants"] if d["block_type"] == 32]
        for cell in cells:
            self.assertEqual(len(cell["children"]), 1)
        texts = [d for d in p["descendants"] if d["block_type"] == 2]
        self.assertTrue(all(t["text"]["elements"] for t in texts))

    def test_private_cell_key_not_sent_to_api(self):
        """_ms_table_cells 是内部字段，不能出现在请求体里。"""
        from core.feishu import _TABLE_CELLS

        p = self._payload("| a |\n|---|\n| 1 |")
        self.assertNotIn(_TABLE_CELLS, json.dumps(p))


class AppendBlocksRoutingTests(unittest.TestCase):
    """混排时表格与平铺块分别走不同接口，但顺序必须和原文一致。"""

    def _calls(self, md):
        from core.feishu import _append_docx_blocks, markdown_to_docx_blocks

        calls = []

        def fake(method, url, **kw):
            calls.append((url, kw.get("body")))
            return {"code": 0, "data": {}}

        with mock.patch("core.feishu._http_json", side_effect=fake), mock.patch(
            "core.feishu.time.sleep"
        ):
            written = _append_docx_blocks(
                "https://open.feishu.cn",
                "tok",
                "doc1",
                markdown_to_docx_blocks(md),
                30.0,
            )
        return calls, written

    def test_table_uses_descendant_endpoint(self):
        calls, written = self._calls("| a |\n|---|\n| 1 |")
        self.assertEqual(len(calls), 1)
        self.assertIn("/descendant", calls[0][0])
        self.assertEqual(written, 1)

    def test_flat_blocks_use_children_endpoint(self):
        calls, written = self._calls("段落一\n段落二")
        self.assertEqual(len(calls), 1)
        self.assertIn("/children", calls[0][0])
        self.assertEqual(written, 2)

    def test_mixed_content_preserves_document_order(self):
        """段落→表格→段落必须按序发三次，否则表格会跑到文末。"""
        calls, written = self._calls("开头\n\n| a |\n|---|\n| 1 |\n\n结尾")
        kinds = ["table" if "/descendant" in u else "flat" for u, _ in calls]
        self.assertEqual(kinds, ["flat", "table", "flat"])
        self.assertEqual(written, 3)
        self.assertEqual(
            calls[0][1]["children"][0]["text"]["elements"][0]["text_run"]["content"],
            "开头",
        )
        self.assertEqual(
            calls[2][1]["children"][0]["text"]["elements"][0]["text_run"]["content"],
            "结尾",
        )

    def test_flat_blocks_still_batched_at_50(self):
        calls, written = self._calls("\n".join(f"段落{i}" for i in range(120)))
        self.assertEqual([len(b["children"]) for _, b in calls], [50, 50, 20])
        self.assertEqual(written, 120)

    def test_error_reports_blocks_already_written(self):
        from core.feishu import _append_docx_blocks, markdown_to_docx_blocks

        state = {"n": 0}

        def fake(method, url, **kw):
            state["n"] += 1
            if state["n"] == 2:
                return {"code": 1770006, "msg": "bad"}
            return {"code": 0, "data": {}}

        with mock.patch("core.feishu._http_json", side_effect=fake), mock.patch(
            "core.feishu.time.sleep"
        ):
            with self.assertRaises(RuntimeError) as ctx:
                _append_docx_blocks(
                    "https://open.feishu.cn",
                    "tok",
                    "doc1",
                    markdown_to_docx_blocks("\n".join(f"段落{i}" for i in range(80))),
                    30.0,
                )
        self.assertIn("已写 50 块", str(ctx.exception))


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


class FeishuMcpToolTests(unittest.TestCase):
    """MCP 工具会被任意 agent 调用，写操作的确认门禁必须在这一层也成立。"""

    def _args(self, **kw):
        return kw

    def _call(self, name, args):
        import mcp_server

        return mcp_server._call_feishu_tool(mock.MagicMock(), name, args)

    def test_write_tools_registered(self):
        """另一个 agent 只看到 memory_feishu_bookmark，就是因为这些工具没注册过。"""
        import mcp_server

        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertIn("memory_feishu_create_doc", names)
        self.assertIn("memory_feishu_edit_body", names)
        self.assertIn("memory_feishu_set_title", names)
        self.assertIn("memory_feishu_preview", names)

    def test_write_tools_require_confirmed_in_schema(self):
        """confirmed 必须是 required，否则 agent 会漏传而默认放行。"""
        import mcp_server

        for name in (
            "memory_feishu_create_doc",
            "memory_feishu_edit_body",
            "memory_feishu_set_title",
        ):
            tool = next(t for t in mcp_server.TOOLS if t["name"] == name)
            self.assertIn("confirmed", tool["inputSchema"]["required"], name)

    def test_create_doc_refused_without_confirmed(self):
        with mock.patch("core.feishu._http_json") as http:
            res = self._call(
                "memory_feishu_create_doc", self._args(title="新文档", content="正文")
            )
        self.assertTrue(res.get("isError"))
        self.assertIn("未确认", res["content"][0]["text"])
        http.assert_not_called()

    def test_edit_body_refused_without_confirmed(self):
        with mock.patch("core.feishu._http_json") as http:
            res = self._call(
                "memory_feishu_edit_body",
                self._args(
                    url="https://foo.feishu.cn/docx/AbC123",
                    content="正文",
                    mode="replace",
                ),
            )
        self.assertTrue(res.get("isError"))
        self.assertIn("未确认", res["content"][0]["text"])
        http.assert_not_called()

    def test_set_title_refused_without_confirmed(self):
        with mock.patch("core.feishu._http_json") as http:
            res = self._call(
                "memory_feishu_set_title",
                self._args(
                    url="https://bytedance.larkoffice.com/wiki/Tok123", title="新标题"
                ),
            )
        self.assertTrue(res.get("isError"))
        self.assertIn("未确认", res["content"][0]["text"])
        http.assert_not_called()

    def test_confirmed_false_is_not_truthy_bypass(self):
        """显式传 false 与不传等价，都必须拒绝。"""
        with mock.patch("core.feishu._http_json") as http:
            res = self._call(
                "memory_feishu_create_doc", self._args(title="x", confirmed=False)
            )
        self.assertTrue(res.get("isError"))
        http.assert_not_called()

    def test_invalid_url_rejected(self):
        with mock.patch("core.feishu._http_json") as http:
            res = self._call(
                "memory_feishu_edit_body",
                self._args(url="https://example.com/foo", content="x", confirmed=True),
            )
        self.assertTrue(res.get("isError"))
        self.assertIn("链接", res["content"][0]["text"])
        http.assert_not_called()

    def test_preview_needs_no_confirmation(self):
        """只读预览不该被门禁挡住，否则 agent 没法在确认前看清目标。"""
        sb = mock.MagicMock()
        with mock.patch("core.feishu.preview_docx_body") as prev:
            prev.return_value = mock.MagicMock(
                ok=True,
                url="u",
                title="标题",
                document_id="doc1",
                block_count=7,
                error="",
            )
            import mcp_server

            res = mcp_server._call_feishu_tool(
                sb,
                "memory_feishu_preview",
                {"url": "https://foo.feishu.cn/docx/AbC123"},
            )
        self.assertFalse(res.get("isError"))
        self.assertIn("\"block_count\": 7", res["content"][0]["text"])


class FeishuReadToolTests(unittest.TestCase):
    """preview 只给标题和块数，读正文得走 memory_feishu_read。"""

    def _read(self, args, content="正文内容", ok=True, error=""):
        import mcp_server

        with mock.patch("core.feishu.fetch_feishu_document") as fetch:
            fetch.return_value = mock.MagicMock(
                ok=ok,
                url="https://foo.feishu.cn/docx/Abc",
                title="需求文档",
                document_id="doc1",
                content=content,
                error=error,
            )
            return mcp_server._call_feishu_tool(
                mock.MagicMock(), "memory_feishu_read", args
            )

    def _payload(self, res):
        return json.loads(res["content"][0]["text"])

    def test_read_tool_registered(self):
        import mcp_server

        names = {t["name"] for t in mcp_server.TOOLS}
        self.assertIn("memory_feishu_read", names)

    def test_returns_full_body(self):
        res = self._read({"url": "https://foo.feishu.cn/docx/Abc"}, content="第一段\n第二段")
        p = self._payload(res)
        self.assertTrue(p["ok"])
        self.assertEqual(p["content"], "第一段\n第二段")
        self.assertEqual(p["title"], "需求文档")
        self.assertFalse(p["truncated"])
        self.assertIsNone(p["next_offset"])

    def test_long_body_is_paged_not_silently_cut(self):
        """119 块的长文档要能续读完，否则 agent 只拿到开头还以为读全了。"""
        body = "".join(str(i % 10) for i in range(1000))
        first = self._payload(
            self._read({"url": "https://foo.feishu.cn/docx/Abc", "max_chars": 400}, content=body)
        )
        self.assertTrue(first["truncated"])
        self.assertEqual(first["next_offset"], 400)
        self.assertEqual(first["total_chars"], 1000)

        second = self._payload(
            self._read(
                {
                    "url": "https://foo.feishu.cn/docx/Abc",
                    "max_chars": 400,
                    "offset": first["next_offset"],
                },
                content=body,
            )
        )
        third = self._payload(
            self._read(
                {
                    "url": "https://foo.feishu.cn/docx/Abc",
                    "max_chars": 400,
                    "offset": second["next_offset"],
                },
                content=body,
            )
        )
        self.assertIsNone(third["next_offset"])
        self.assertEqual(
            first["content"] + second["content"] + third["content"], body
        )

    def test_read_failure_surfaces_error(self):
        res = self._read(
            {"url": "https://foo.feishu.cn/docx/Abc"}, ok=False, error="token 过期"
        )
        self.assertTrue(res.get("isError"))
        self.assertIn("token 过期", res["content"][0]["text"])

    def test_invalid_url_rejected(self):
        import mcp_server

        with mock.patch("core.feishu.fetch_feishu_document") as fetch:
            res = mcp_server._call_feishu_tool(
                mock.MagicMock(), "memory_feishu_read", {"url": "https://example.com/x"}
            )
        self.assertTrue(res.get("isError"))
        fetch.assert_not_called()

    def test_bad_offset_type_rejected(self):
        res = self._read({"url": "https://foo.feishu.cn/docx/Abc", "offset": "abc"})
        self.assertTrue(res.get("isError"))

    def test_read_does_not_write_memory(self):
        """读是只读：不该顺手往记忆里塞东西。"""
        import mcp_server

        sb = mock.MagicMock()
        with mock.patch("core.feishu.fetch_feishu_document") as fetch:
            fetch.return_value = mock.MagicMock(
                ok=True,
                url="u",
                title="t",
                document_id="d",
                content="c",
                error="",
            )
            mcp_server._call_feishu_tool(
                sb, "memory_feishu_read", {"url": "https://foo.feishu.cn/docx/Abc"}
            )
        sb.remember_feishu_write.assert_not_called()
        sb.remember.assert_not_called()

    def test_preview_still_has_no_content_field(self):
        """preview 故意不带正文（省 token），这点要锁住，否则两个工具职责会糊掉。"""
        import mcp_server

        with mock.patch("core.feishu.preview_docx_body") as prev:
            prev.return_value = mock.MagicMock(
                ok=True, url="u", title="t", document_id="d", block_count=119, error=""
            )
            res = mcp_server._call_feishu_tool(
                mock.MagicMock(),
                "memory_feishu_preview",
                {"url": "https://foo.feishu.cn/docx/Abc"},
            )
        self.assertNotIn("content", json.loads(res["content"][0]["text"]))


class FeishuCommentTests(unittest.TestCase):
    """评论是写操作（协作者立刻看得见），门禁与改正文同级。"""

    def _ref(self):
        return FeishuDocRef(
            url="https://foo.feishu.cn/docx/AbC123", kind="docx", token="AbC123"
        )

    def _cfg(self):
        return FeishuConfig(
            enabled=True, app_id="cli_x", app_secret="s", user_access_token="u-tok"
        )

    def test_refuses_without_explicit_confirmation(self):
        from core.feishu import create_docx_comment

        with mock.patch("core.feishu._http_json") as http:
            res = create_docx_comment(self._cfg(), self._ref(), "评论内容")
        self.assertFalse(res.ok)
        self.assertIn("未确认", res.error)
        http.assert_not_called()

    def test_rejects_empty_comment(self):
        from core.feishu import create_docx_comment

        with mock.patch("core.feishu._http_json") as http:
            res = create_docx_comment(
                self._cfg(), self._ref(), "   ", confirmed=True
            )
        self.assertFalse(res.ok)
        http.assert_not_called()

    def test_posts_text_run_payload(self):
        from core.feishu import create_docx_comment

        calls = []

        def fake(method, url, **kw):
            calls.append((method, url, kw.get("body")))
            if "/comments" in url:
                return {"code": 0, "data": {"comment_id": "c-999"}}
            return {"code": 0, "data": {"document": {"title": "目标文档"}}}

        with mock.patch("core.feishu._http_json", side_effect=fake), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-tok"
        ):
            res = create_docx_comment(
                self._cfg(), self._ref(), "这里建议补充埋点", confirmed=True
            )
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.comment_id, "c-999")
        post = [c for c in calls if c[0] == "POST"][0]
        self.assertIn("file_type=docx", post[1])
        el = post[2]["reply_list"]["replies"][0]["content"]["elements"][0]
        self.assertEqual(el["text_run"]["text"], "这里建议补充埋点")
        self.assertNotIn("comment_id", post[2])

    def test_reply_goes_into_the_thread_not_the_bottom_of_the_doc(self):
        """
        回复必须打到 comments/{id}/replies。

        往 comments 接口的 body 里塞 comment_id 是没用的——那个字段只在响应里有，
        请求体规范没有它，会被静默忽略，于是每条「回复」都变成文档底部一条新的
        全文评论（2026-08-07 实测）。
        """
        from core.feishu import create_docx_comment

        posts = []

        def fake(method, url, **kw):
            if method == "POST":
                posts.append((url, kw.get("body")))
                return {"code": 0, "data": {"reply_id": "r-new"}}
            return {"code": 0, "data": {"document": {"title": "t"}}}

        with mock.patch("core.feishu._http_json", side_effect=fake), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-tok"
        ):
            res = create_docx_comment(
                self._cfg(),
                self._ref(),
                "同意",
                comment_id="c-old",
                confirmed=True,
            )
        url, body = posts[0]
        self.assertIn("/comments/c-old/replies", url)
        self.assertIn("file_type=docx", url)
        self.assertEqual(
            body["content"]["elements"][0]["text_run"]["text"], "同意"
        )
        self.assertNotIn("reply_list", body, "回复接口的 body 不是全文评论那套结构")
        self.assertEqual(res.replied_to, "c-old")
        self.assertEqual(res.reply_id, "r-new")
        # 评论串还是原来那条，别让调用方以为新开了一条评论
        self.assertEqual(res.comment_id, "c-old")

    def test_permission_error_names_the_scope(self):
        from core.feishu import create_docx_comment

        def fake(method, url, **kw):
            if method == "POST":
                raise RuntimeError("HTTP 403: {\"code\":1069303}")
            return {"code": 0, "data": {"document": {"title": "t"}}}

        with mock.patch("core.feishu._http_json", side_effect=fake), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-tok"
        ):
            res = create_docx_comment(self._cfg(), self._ref(), "x", confirmed=True)
        self.assertFalse(res.ok)
        self.assertIn("docs:document.comment:create", res.error)

    def test_list_comments_paginates_and_parses(self):
        from core.feishu import list_docx_comments

        pages = [
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "comment_id": "c1",
                            "is_whole": True,
                            "is_solved": False,
                            "reply_list": {
                                "replies": [
                                    {
                                        "content": {
                                            "elements": [
                                                {
                                                    "type": "text_run",
                                                    "text_run": {"text": "第一条"},
                                                }
                                            ]
                                        }
                                    }
                                ]
                            },
                        }
                    ],
                    "has_more": True,
                    "page_token": "pt-2",
                },
            },
            {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "comment_id": "c2",
                            "is_whole": False,
                            "quote": "被选中的原文",
                            "reply_list": {
                                "replies": [
                                    {
                                        "content": {
                                            "elements": [
                                                {
                                                    "type": "text_run",
                                                    "text_run": {"text": "局部意见"},
                                                }
                                            ]
                                        }
                                    }
                                ]
                            },
                        }
                    ],
                    "has_more": False,
                },
            },
        ]
        seen = []

        def fake(method, url, **kw):
            if "/comments" in url:
                seen.append(url)
                return pages[len(seen) - 1]
            return {"code": 0, "data": {"document": {"title": "目标文档"}}}

        with mock.patch("core.feishu._http_json", side_effect=fake), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-tok"
        ):
            res = list_docx_comments(self._cfg(), self._ref())
        self.assertTrue(res.ok, res.error)
        self.assertEqual([c.comment_id for c in res.comments], ["c1", "c2"])
        self.assertEqual(res.comments[0].replies, ["第一条"])
        # 客户端里加的局部评论 API 加不了，但要能读出来
        self.assertFalse(res.comments[1].is_whole)
        self.assertEqual(res.comments[1].quote, "被选中的原文")
        self.assertIn("page_token=pt-2", seen[1])

    def test_list_comments_respects_cap(self):
        from core.feishu import list_docx_comments

        def fake(method, url, **kw):
            if "/comments" in url:
                return {
                    "code": 0,
                    "data": {
                        "items": [{"comment_id": f"c{i}"} for i in range(50)],
                        "has_more": True,
                        "page_token": "next",
                    },
                }
            return {"code": 0, "data": {"document": {"title": "t"}}}

        with mock.patch("core.feishu._http_json", side_effect=fake), mock.patch(
            "core.feishu_oauth.ensure_user_access_token", return_value="u-tok"
        ):
            res = list_docx_comments(self._cfg(), self._ref(), max_comments=10)
        self.assertEqual(len(res.comments), 10)
        self.assertTrue(res.truncated)

    def test_comment_element_types_become_readable_text(self):
        from core.feishu import _comment_text

        text = _comment_text(
            {
                "elements": [
                    {"type": "text_run", "text_run": {"text": "看看 "}},
                    {"type": "person", "person": {"user_id": "ou_1"}},
                    {"type": "docs_link", "docs_link": {"url": "https://x/docs/1"}},
                ]
            }
        )
        self.assertEqual(text, "看看 @ou_1https://x/docs/1")


class FeishuCommentMemoryTests(unittest.TestCase):
    """评论落库不能把正文记录冲掉——问法相同就会被去重更新覆盖。"""

    def _build(self, action, content):
        from core.feishu_memory import build_write_memory

        return build_write_memory(
            action=action,
            url="https://foo.feishu.cn/docx/AbC123",
            title="招募卡升级为公会主页",
            document_id="doc1",
            content=content,
            blocks_written=3 if action != "comment" else 0,
            ok=True,
        )

    def test_comment_question_differs_from_body_question(self):
        body = self._build("append", "# 正文")
        comment = self._build("comment", "建议补充埋点")
        self.assertNotEqual(body.question, comment.question)
        self.assertIn("评论记录", comment.question)
        self.assertIn("正文与写入记录", body.question)

    def test_comment_recorded_with_its_text(self):
        mem = self._build("comment", "建议补充埋点")
        self.assertIn("加评论", mem.answer)
        self.assertIn("建议补充埋点", mem.answer)
        # 评论没有大纲可言
        self.assertNotIn("大纲：", mem.answer)

    def test_failed_comment_not_recorded(self):
        from core.feishu_memory import build_write_memory

        self.assertIsNone(
            build_write_memory(
                action="comment",
                url="https://foo.feishu.cn/docx/AbC123",
                title="t",
                ok=False,
                error="权限不足",
            )
        )

    def _sandbox(self, tmp):
        cfg = AppConfig(
            sensory=SensoryConfig(ttl=5.0),
            working=WorkingConfig(chunk_size=7),
            long_term=LongTermConfig(persist_dir=tmp, top_k=3),
        )
        return MemorySandbox(config=cfg)

    def _write(self, sb, action, content, **kw):
        return sb.remember_feishu_write(
            action=action,
            url="https://foo.feishu.cn/docx/AbC123",
            title="招募卡升级为公会主页",
            document_id="doc1",
            content=content,
            ok=True,
            **kw,
        )

    def test_comment_does_not_overwrite_body_record_in_store(self):
        """端到端：同一篇文档的正文记录与评论记录应共存为两条。

        去重链里有「同飞书 token 即同一条」，光让问法不同是不够的。
        """
        tmp = tempfile.mkdtemp(prefix="cmt-mem-")
        try:
            sb = self._sandbox(tmp)
            self._write(sb, "append", "# 背景\n正文很长", blocks_written=5)
            self._write(sb, "comment", "建议补充埋点")
            self.assertEqual(len(sb.long_term.records), 2)
            blob = "\n".join(r.answer for r in sb.long_term.records)
            self.assertIn("正文很长", blob)
            self.assertIn("建议补充埋点", blob)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_body_writes_still_dedup_to_one_record(self):
        """回归：分面机制不能破坏原有的「同一篇文档正文只留一条」。"""
        tmp = tempfile.mkdtemp(prefix="cmt-mem-")
        try:
            sb = self._sandbox(tmp)
            self._write(sb, "append", "第一次正文", blocks_written=2)
            self._write(sb, "replace", "第二次正文", blocks_written=3, blocks_deleted=2)
            self.assertEqual(len(sb.long_term.records), 1)
            self.assertIn("第二次正文", sb.long_term.records[0].answer)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_repeated_comments_dedup_within_their_own_facet(self):
        """评论之间仍要去重更新，不能每评论一次就多一条。"""
        tmp = tempfile.mkdtemp(prefix="cmt-mem-")
        try:
            sb = self._sandbox(tmp)
            self._write(sb, "comment", "第一条评论")
            self._write(sb, "comment", "第二条评论")
            self.assertEqual(len(sb.long_term.records), 1)
            self.assertIn("第二条评论", sb.long_term.records[0].answer)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_reading_doc_then_commenting_keeps_both(self):
        """先把文档正文记进来，再评论，不该把正文记忆冲掉。"""
        tmp = tempfile.mkdtemp(prefix="cmt-mem-")
        try:
            sb = self._sandbox(tmp)
            sb.remember(
                "《招募卡升级为公会主页》飞书文档技术要点 "
                "https://foo.feishu.cn/docx/AbC123",
                "文档要点：招募卡改造方案，包含灰度策略。",
            )
            self._write(sb, "comment", "建议补充埋点")
            self.assertEqual(len(sb.long_term.records), 2)
            blob = "\n".join(r.answer for r in sb.long_term.records)
            self.assertIn("灰度策略", blob)
            self.assertIn("建议补充埋点", blob)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_comment_record_is_retrievable_by_topic(self):
        """评论记录也要能被文档主题词命中，否则落库了却找不回来。"""
        tmp = tempfile.mkdtemp(prefix="cmt-mem-")
        try:
            sb = self._sandbox(tmp)
            self._write(sb, "comment", "建议补充埋点")
            for q in ("公会主页", "招募卡升级"):
                self.assertTrue(sb.long_term.search_hits(q, top_k=3), q)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class FeishuWriteMemoryTests(unittest.TestCase):
    """写操作落库：问法要稳定可命中，飞书侧没动过则不该留痕。"""

    def _build(self, **kw):
        from core.feishu_memory import build_write_memory

        base = dict(
            action="create",
            url="https://bytedance.larkoffice.com/docx/AbC123",
            title="客服工单系统接入说明",
            document_id="doc123",
            blocks_written=3,
            ok=True,
        )
        base.update(kw)
        return build_write_memory(**base)

    def test_question_uses_bracket_title_and_keeps_url(self):
        """《标题》+ 末尾链接，与读取型飞书记忆同构才能被同样的检索命中。"""
        mem = self._build()
        self.assertTrue(mem.question.startswith("《客服工单系统接入说明》"))
        self.assertIn("https://bytedance.larkoffice.com/docx/AbC123", mem.question)

    def test_answer_records_action_and_ids(self):
        mem = self._build(content="# 概述\n正文内容")
        self.assertIn("新建文档", mem.answer)
        self.assertIn("doc123", mem.answer)
        self.assertIn("写入块：3", mem.answer)

    def test_replace_records_deleted_count(self):
        mem = self._build(action="replace", blocks_deleted=8, blocks_written=2)
        self.assertIn("替换正文", mem.answer)
        self.assertIn("删除原有块：8", mem.answer)

    def test_title_change_keeps_old_title(self):
        mem = self._build(
            action="title", title="新标题", old_title="旧标题", blocks_written=0
        )
        self.assertIn("旧标题", mem.answer)
        self.assertIn("《新标题》", mem.question)

    def test_nothing_recorded_when_feishu_untouched(self):
        """未确认被拒 / token 失效这类零副作用调用不该记成改动史。"""
        self.assertIsNone(
            self._build(ok=False, document_id="", blocks_written=0, error="未确认")
        )

    def test_half_created_doc_is_recorded(self):
        """文档建出来了但正文写失败：这个半成品必须留档，否则没人记得清理。"""
        mem = self._build(ok=False, blocks_written=0, error="写正文失败")
        self.assertIsNotNone(mem)
        self.assertIn("未完成", mem.answer)
        self.assertIn("doc123", mem.answer)

    def test_replace_that_deleted_then_failed_is_recorded(self):
        mem = self._build(
            action="replace", ok=False, blocks_deleted=5, blocks_written=0, error="写入失败"
        )
        self.assertIsNotNone(mem)
        self.assertIn("删除原有块：5", mem.answer)

    def test_outline_skips_headings_inside_code_fence(self):
        """代码块里的 # 是注释，混进大纲会让大纲失去导航价值（正文摘录里出现无妨）。"""
        content = "# 真标题\n```\n# 这是注释不是标题\n```\n## 子标题"
        mem = self._build(content=content)
        outline = mem.answer.split("大纲：")[1].split("正文摘录：")[0]
        self.assertIn("真标题", outline)
        self.assertIn("子标题", outline)
        self.assertNotIn("这是注释不是标题", outline)

    def test_long_body_is_excerpted_not_dumped(self):
        mem = self._build(content="正文段落。" * 500)
        self.assertLess(len(mem.answer), 1500)
        self.assertIn("…", mem.answer)

    def test_facts_only_use_whitelisted_keys(self):
        """facts 白名单外的键会被 normalize_facts 静默丢掉，别放进去自欺欺人。"""
        from core.structure import FACT_KEYS

        mem = self._build()
        self.assertEqual(mem.facts["path"], "https://bytedance.larkoffice.com/docx/AbC123")
        self.assertTrue(set(mem.facts).issubset(set(FACT_KEYS)))
        # document_id 进不了 facts，必须能在正文里找回
        self.assertIn("doc123", mem.answer)

    def test_placeholder_title_falls_back_without_brackets(self):
        mem = self._build(title="")
        self.assertNotIn("《", mem.question)
        self.assertIn("飞书文档", mem.question)

    def test_same_doc_edited_twice_keeps_one_question(self):
        """问法稳定，交给 save_memory 去重更新，一篇文档不该堆成多条。"""
        a = self._build(action="append", content="第一次")
        b = self._build(action="append", content="第二次")
        self.assertEqual(a.question, b.question)


class FeishuWriteRememberedByMcpTests(unittest.TestCase):
    """MCP 写工具成功后必须落库，且落库失败不能盖掉写成功。"""

    def _sb(self):
        sb = mock.MagicMock()
        sb.remember_feishu_write.return_value = "已写入长时记忆 [abc123]"
        return sb

    def test_create_doc_records_to_long_term(self):
        import mcp_server

        sb = self._sb()
        with mock.patch("core.feishu.create_docx_document") as create:
            create.return_value = mock.MagicMock(
                ok=True,
                title="新文档",
                url="https://foo.feishu.cn/docx/Abc",
                document_id="doc1",
                blocks_written=2,
                error="",
            )
            res = mcp_server._call_feishu_tool(
                sb,
                "memory_feishu_create_doc",
                {"title": "新文档", "content": "# 标题", "confirmed": True},
            )
        sb.remember_feishu_write.assert_called_once()
        kw = sb.remember_feishu_write.call_args.kwargs
        self.assertEqual(kw["action"], "create")
        self.assertEqual(kw["document_id"], "doc1")
        self.assertEqual(kw["content"], "# 标题")
        self.assertIn("已写入长时记忆", res["content"][0]["text"])

    def test_edit_body_records_mode_and_counts(self):
        import mcp_server

        sb = self._sb()
        with mock.patch("core.feishu.update_docx_body") as upd:
            upd.return_value = mock.MagicMock(
                ok=True,
                url="https://foo.feishu.cn/docx/Abc",
                title="目标文档",
                mode="replace",
                document_id="doc1",
                blocks_written=3,
                blocks_deleted=7,
                error="",
            )
            mcp_server._call_feishu_tool(
                sb,
                "memory_feishu_edit_body",
                {
                    "url": "https://foo.feishu.cn/docx/Abc",
                    "content": "新正文",
                    "mode": "replace",
                    "confirmed": True,
                },
            )
        kw = sb.remember_feishu_write.call_args.kwargs
        self.assertEqual(kw["action"], "replace")
        self.assertEqual(kw["blocks_deleted"], 7)
        self.assertEqual(kw["title"], "目标文档")

    def test_set_title_records_old_and_new(self):
        import mcp_server

        sb = self._sb()
        with mock.patch("core.feishu.update_wiki_node_title") as upd:
            upd.return_value = mock.MagicMock(
                ok=True,
                url="https://foo.larkoffice.com/wiki/Abc",
                old_title="旧",
                new_title="新",
                error="",
            )
            mcp_server._call_feishu_tool(
                sb,
                "memory_feishu_set_title",
                {
                    "url": "https://foo.larkoffice.com/wiki/Abc",
                    "title": "新",
                    "confirmed": True,
                },
            )
        kw = sb.remember_feishu_write.call_args.kwargs
        self.assertEqual(kw["action"], "title")
        self.assertEqual(kw["old_title"], "旧")

    def test_remember_failure_does_not_mask_successful_write(self):
        """落库炸了也要报 ok，否则调用方重试会再建一篇重复文档。"""
        import mcp_server

        sb = mock.MagicMock()
        sb.remember_feishu_write.side_effect = RuntimeError("磁盘满了")
        with mock.patch("core.feishu.create_docx_document") as create:
            create.return_value = mock.MagicMock(
                ok=True,
                title="新文档",
                url="https://foo.feishu.cn/docx/Abc",
                document_id="doc1",
                blocks_written=1,
                error="",
            )
            res = mcp_server._call_feishu_tool(
                sb,
                "memory_feishu_create_doc",
                {"title": "新文档", "confirmed": True},
            )
        self.assertFalse(res.get("isError"))
        text = res["content"][0]["text"]
        self.assertIn("落库失败", text)
        self.assertIn("磁盘满了", text)

    def test_no_remember_when_write_refused(self):
        import mcp_server

        sb = self._sb()
        mcp_server._call_feishu_tool(
            sb, "memory_feishu_create_doc", {"title": "x"}
        )
        sb.remember_feishu_write.assert_not_called()

    def test_preview_does_not_record(self):
        import mcp_server

        sb = self._sb()
        with mock.patch("core.feishu.preview_docx_body") as prev:
            prev.return_value = mock.MagicMock(
                ok=True, url="u", title="t", document_id="d", block_count=1, error=""
            )
            mcp_server._call_feishu_tool(
                sb, "memory_feishu_preview", {"url": "https://foo.feishu.cn/docx/Abc"}
            )
        sb.remember_feishu_write.assert_not_called()


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


class LocateBlockTests(unittest.TestCase):
    """局部评论要先按文字定位到块；挂错位置比失败更糟，所以歧义时必须报错。"""

    def _blocks(self):
        return [
            {"block_id": "root", "block_type": 1, "page": {"elements": [
                {"text_run": {"content": "整篇标题"}}]}},
            {"block_id": "b1", "block_type": 2, "text": {"elements": [
                {"text_run": {"content": "段落里的 "}},
                {"text_run": {"content": "粗体", "text_element_style": {"bold": True}}},
                {"text_run": {"content": " 都应生效"}},
            ]}},
            {"block_id": "b2", "block_type": 4, "heading2": {"elements": [
                {"text_run": {"content": "表格渲染验证"}}]}},
            {"block_id": "b3", "block_type": 2, "text": {"elements": [
                {"text_run": {"content": "待确认"}}]}},
            {"block_id": "b4", "block_type": 2, "text": {"elements": [
                {"text_run": {"content": "待确认"}}]}},
        ]

    def test_plain_text_joins_all_elements(self):
        from core.feishu import _block_plain_text

        self.assertEqual(
            _block_plain_text(self._blocks()[1]), "段落里的 粗体 都应生效"
        )

    def test_locates_by_substring(self):
        from core.feishu import _locate_block

        self.assertEqual(_locate_block(self._blocks(), "表格渲染验证"), "b2")

    def test_substring_across_styled_elements(self):
        """跨样式边界的文字也要能定位，粗体会把段落切成多个 element。"""
        from core.feishu import _locate_block

        self.assertEqual(_locate_block(self._blocks(), "里的粗体都应"), "b1")

    def test_whitespace_insensitive(self):
        from core.feishu import _locate_block

        self.assertEqual(_locate_block(self._blocks(), "段落里的  粗体"), "b1")

    def test_page_block_skipped(self):
        """页面块是文档根、整篇文字都挂在它下面，不能当锚点。"""
        from core.feishu import _locate_block

        with self.assertRaises(RuntimeError):
            _locate_block(self._blocks(), "整篇标题")

    def test_ambiguous_match_raises_with_candidates(self):
        from core.feishu import _locate_block

        with self.assertRaises(RuntimeError) as ctx:
            _locate_block(self._blocks(), "待确认")
        msg = str(ctx.exception)
        self.assertIn("命中 2 个块", msg)
        self.assertIn("b3", msg)

    def test_exact_match_wins_over_partial(self):
        from core.feishu import _locate_block

        blocks = [
            {"block_id": "long", "block_type": 2, "text": {"elements": [
                {"text_run": {"content": "结果待确认，请复核"}}]}},
            {"block_id": "exact", "block_type": 2, "text": {"elements": [
                {"text_run": {"content": "结果待确认"}}]}},
        ]
        self.assertEqual(_locate_block(blocks, "结果待确认"), "exact")

    def test_missing_text_raises(self):
        from core.feishu import _locate_block

        with self.assertRaises(RuntimeError) as ctx:
            _locate_block(self._blocks(), "并不存在的文字")
        self.assertIn("没找到", str(ctx.exception))


class AnchoredCommentTests(unittest.TestCase):
    """局部评论必须走 v2 的 new_comments + anchor.block_id；v1 只能建全文评论。"""

    def _cfg(self):
        return FeishuConfig(
            enabled=True, app_id="cli_x", app_secret="s", user_access_token="u-tok"
        )

    def _ref(self):
        return FeishuDocRef(url="https://x.larkoffice.com/docx/DOC1", kind="docx", token="DOC1")

    def _run(self, **kw):
        from core.feishu import create_docx_comment

        calls = []

        def fake(method, url, **rest):
            calls.append((method, url, rest.get("body")))
            if "/blocks?" in url or url.endswith("/blocks"):
                return {"code": 0, "data": {"items": [
                    {"block_id": "blk9", "block_type": 2, "text": {"elements": [
                        {"text_run": {"content": "需要复核的那一段"}}]}},
                ], "has_more": False}}
            if url.rstrip("/").endswith("/documents/DOC1"):
                return {"code": 0, "data": {"document": {"title": "T"}}}
            return {"code": 0, "data": {"comment_id": "c1"}}

        with mock.patch("core.feishu._http_json", side_effect=fake):
            res = create_docx_comment(
                self._cfg(), self._ref(), "请补充说明", confirmed=True, **kw
            )
        return res, calls

    def test_anchor_text_uses_new_comments_endpoint(self):
        res, calls = self._run(anchor_text="需要复核的那一段")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.block_id, "blk9")
        post = [c for c in calls if c[0] == "POST"][-1]
        self.assertIn("/new_comments", post[1])
        self.assertEqual(post[2]["anchor"], {"block_id": "blk9"})
        self.assertEqual(post[2]["file_type"], "docx")
        self.assertEqual(
            post[2]["reply_elements"], [{"type": "text", "text": "请补充说明"}]
        )

    def test_explicit_block_id_skips_block_listing(self):
        """已知 block_id 就不必再拉全量块，省一次请求。"""
        res, calls = self._run(block_id="blkX")
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.block_id, "blkX")
        self.assertFalse([c for c in calls if "/blocks" in c[1]])

    def test_whole_comment_still_uses_v1_endpoint(self):
        res, calls = self._run()
        self.assertTrue(res.ok, res.error)
        self.assertEqual(res.block_id, "")
        post = [c for c in calls if c[0] == "POST"][-1]
        self.assertTrue(post[1].split("?")[0].endswith("/comments"))
        self.assertIn("reply_list", post[2])

    def test_reply_and_anchor_are_mutually_exclusive(self):
        res, calls = self._run(comment_id="c0", anchor_text="需要复核的那一段")
        self.assertFalse(res.ok)
        self.assertIn("不能同时", res.error)
        self.assertFalse(calls)

    def test_unconfirmed_sends_nothing(self):
        from core.feishu import create_docx_comment

        with mock.patch("core.feishu._http_json") as http:
            res = create_docx_comment(
                self._cfg(), self._ref(), "x", anchor_text="需要复核的那一段"
            )
        self.assertFalse(res.ok)
        http.assert_not_called()

    def test_angle_brackets_escaped(self):
        """new_comments 不接受裸 < >。"""
        from core.feishu import _escape_comment_text

        self.assertEqual(_escape_comment_text("a<b>c"), "a&lt;b&gt;c")

    def test_locate_failure_reports_without_commenting(self):
        res, calls = self._run(anchor_text="文档里没有这句")
        self.assertFalse(res.ok)
        self.assertIn("没找到", res.error)
        self.assertFalse([c for c in calls if "comments" in c[1]])


class ChatHistoryTests(unittest.TestCase):
    """
    读群历史：被引用的卡片读不出字时去上游找料。

    接口只能整页按时间倒序翻，所以要一直翻到被引用那条，再往前数几条；
    翻不到就退回最近几条。返回给模型的必须是时间正序。
    """

    APP_ID = "cli_me"

    def _cfg(self):
        return FeishuConfig(enabled=True, app_id=self.APP_ID, app_secret="s")

    def _msg(self, mid: str, text: str, *, sender="ou_user", stype="user") -> dict:
        return {
            "message_id": mid,
            "msg_type": "text",
            "body": {"content": json.dumps({"text": text})},
            "sender": {"id": sender, "sender_type": stype},
        }

    def _run(self, pages, **kwargs):
        urls = []

        def fake(method, url, **kw):
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t"}
            urls.append(url)
            return {"code": 0, "data": pages[len(urls) - 1]}

        from core.feishu import list_chat_messages

        with mock.patch("core.feishu._http_json", side_effect=fake):
            return list_chat_messages(self._cfg(), "oc_1", **kwargs), urls

    def test_walks_back_to_the_quoted_message_and_takes_what_is_before_it(self):
        page1 = {
            "items": [self._msg("om_new", "记下来"), self._msg("om_card", "卡片")],
            "has_more": True,
            "page_token": "p2",
        }
        page2 = {
            "items": [self._msg("om_ask", "分析这个告警"), self._msg("om_alert", "告警详情")],
            "has_more": False,
        }
        got, urls = self._run([page1, page2], before_message_id="om_card", limit=2)
        # 只要卡片**之前**的，且按时间正序交给模型
        self.assertEqual(
            [json.loads(m["content"])["text"] for m in got], ["告警详情", "分析这个告警"]
        )
        self.assertEqual(len(urls), 2)
        self.assertIn("page_token=p2", urls[1])

    def test_stops_paging_once_it_has_enough(self):
        page1 = {
            "items": [
                self._msg("om_card", "卡片"),
                self._msg("om_a", "一"),
                self._msg("om_b", "二"),
            ],
            "has_more": True,
            "page_token": "p2",
        }
        _got, urls = self._run([page1], before_message_id="om_card", limit=2)
        self.assertEqual(len(urls), 1)

    def test_our_own_messages_are_not_context(self):
        """机器人上一轮的回复混进去只会带偏模型。"""
        page = {
            "items": [
                self._msg("om_mine", "已记住：…", sender=self.APP_ID, stype="app"),
                self._msg("om_alert", "告警详情"),
            ],
            "has_more": False,
        }
        got, _urls = self._run([page], limit=5)
        self.assertEqual([json.loads(m["content"])["text"] for m in got], ["告警详情"])

    def test_missing_scope_says_which_one(self):
        def fake(method, url, **kw):
            if "tenant_access_token" in url:
                return {"code": 0, "tenant_access_token": "t"}
            raise RuntimeError('HTTP 403: {"code":230027}')

        from core.feishu import list_chat_messages

        with mock.patch("core.feishu._http_json", side_effect=fake):
            with self.assertRaises(RuntimeError) as caught:
                list_chat_messages(self._cfg(), "oc_1")
        self.assertIn("im:message.group_msg", str(caught.exception))


class McpFeishuConfigFreshnessTests(unittest.TestCase):
    """MCP 是常驻进程，飞书凭据必须每次从磁盘重读，否则重新登录后一直报 token 失效。"""

    class _FakeSandbox:
        def __init__(self, cfg):
            self.config = cfg

    def _sb(self, token):
        cfg = mock.Mock()
        cfg.feishu = FeishuConfig(enabled=True, app_id="cli_x", user_access_token=token)
        return self._FakeSandbox(cfg)

    def test_reloads_token_from_disk(self):
        import mcp_server

        sb = self._sb("stale-token")
        fresh_cfg = mock.Mock()
        fresh_cfg.feishu = FeishuConfig(
            enabled=True, app_id="cli_x", user_access_token="new-token"
        )
        with mock.patch.object(mcp_server, "load_config", return_value=fresh_cfg):
            got = mcp_server._fresh_feishu_cfg(sb)
        self.assertEqual(got.user_access_token, "new-token")
        # 单例上的旧配置也要一起换掉，免得别处继续用陈的
        self.assertEqual(sb.config.feishu.user_access_token, "new-token")

    def test_falls_back_when_config_unreadable(self):
        """配置读不出来时用缓存兜底，不能让飞书工具直接崩。"""
        import mcp_server

        sb = self._sb("cached-token")
        with mock.patch.object(
            mcp_server, "load_config", side_effect=OSError("boom")
        ):
            got = mcp_server._fresh_feishu_cfg(sb)
        self.assertEqual(got.user_access_token, "cached-token")


if __name__ == "__main__":
    unittest.main()
