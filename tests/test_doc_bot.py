"""文档评论机器人的单测：不连飞书、不装 SDK 也能跑。

这条链路错了就是「在别人文档里乱说话、乱改字」，所以每个分支都要脱机测一遍，
尤其是「没确认不许写」和「自己别触发自己」。
"""

from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.doc_bot import (  # noqa: E402
    BOT_PREFIX,
    authorized,
    classify,
    comment_question,
    extract_append_text,
    extract_replacement,
    format_answer,
    format_proposal,
    is_bot_reply,
    is_cancellation,
    is_confirmation,
    mentions_bot,
    parse_comment_event,
    pick_reply_id,
    pick_reply_text,
    strip_trigger,
    wants_append,
)
from core.doc_bot import EditPlan  # noqa: E402
from core.doc_bot_state import DocBotState, EditProposal  # noqa: E402

ME = "ou_me"
TRIGGER = "@BloomBot"


def comment_event(
    *,
    open_id: str = ME,
    comment_id: str = "c_1",
    reply_id: str = "r_1",
    file_type: str = "docx",
    notice_type: str = "add_comment",
) -> dict:
    return {
        "schema": "2.0",
        "header": {"event_type": "drive.notice.comment_add_v1"},
        "event": {
            "notice_meta": {
                "file_type": file_type,
                "file_token": "docx_token",
                "from_user_id": {"open_id": open_id, "user_id": "u1"},
                "to_user_id": {"open_id": ME, "user_id": "u0"},
                "notice_type": notice_type,
            },
            "comment_id": comment_id,
            "reply_id": reply_id,
            "is_mentioned": True,
        },
    }


class ParseEventTests(unittest.TestCase):
    def test_pulls_the_fields_we_need(self):
        ev = parse_comment_event(comment_event())
        self.assertEqual(ev.file_token, "docx_token")
        self.assertEqual(ev.comment_id, "c_1")
        self.assertEqual(ev.reply_id, "r_1")
        self.assertEqual(ev.open_id, ME)
        self.assertEqual(ev.key, "c_1:r_1")

    def test_other_events_are_not_ours(self):
        payload = comment_event()
        payload["header"]["event_type"] = "im.message.receive_v1"
        self.assertIsNone(parse_comment_event(payload))

    def test_payload_without_a_file_token_is_useless(self):
        payload = comment_event()
        payload["event"]["notice_meta"]["file_token"] = ""
        self.assertIsNone(parse_comment_event(payload))


class GateTests(unittest.TestCase):
    def test_only_the_allowlist_is_served(self):
        self.assertTrue(authorized(ME, [ME, "ou_x"]))
        self.assertFalse(authorized("ou_other", [ME]))
        self.assertFalse(authorized(ME, []))

    def test_trigger_word_is_required(self):
        self.assertTrue(mentions_bot("@BloomBot 这段什么意思", TRIGGER))
        self.assertTrue(mentions_bot("bloombot 看下", TRIGGER))
        self.assertFalse(mentions_bot("这段什么意思", TRIGGER))

    def test_at_by_open_id_counts_as_pointing_at_the_bot(self):
        """
        评论接口返回的正文里，@ 只剩 `@ou_xxx` 这串 id，没有显示名。

        踩过：只比对「@BloomBot」，于是用户明明 @ 了机器人，这里判成没点名、评论被
        静悄悄丢掉，排查时一路误判成「飞书没推事件」。
        """
        bot = "ou_49d8e595f3e1086fd74c5958f5e923b3"
        text = f"@{bot} 记一下"
        self.assertFalse(mentions_bot(text, TRIGGER))
        self.assertTrue(mentions_bot(text, TRIGGER, bot))
        # 别人被 @ 到的评论仍然不该接
        self.assertFalse(mentions_bot("@ou_someone_else 你看下", TRIGGER, bot))

    def test_trigger_is_stripped_from_the_instruction(self):
        self.assertEqual(strip_trigger("@BloomBot 这段什么意思", TRIGGER), "这段什么意思")
        self.assertEqual(strip_trigger("这段什么意思 @BloomBot", TRIGGER), "这段什么意思")

    def test_the_reply_that_fired_the_event_is_the_one_we_read(self):
        replies = [
            SimpleNamespace(reply_id="r_1", text="第一条"),
            SimpleNamespace(reply_id="r_2", text="@BloomBot 看下这段"),
        ]
        self.assertEqual(pick_reply_text(replies, "r_2"), "@BloomBot 看下这段")
        # 找不到就退回最后一条：重投或时序错乱时它几乎总是刚发的那条
        self.assertEqual(pick_reply_text(replies, "r_9"), "@BloomBot 看下这段")
        self.assertEqual(pick_reply_text([], "r_1"), "")

    def test_the_reaction_lands_on_the_same_reply_we_read(self):
        # 表情挂 reply_id，回退规则必须和取正文一致，否则表情贴到别人那条回复上
        replies = [
            SimpleNamespace(reply_id="r_1", text="第一条"),
            SimpleNamespace(reply_id="r_2", text="@BloomBot 看下这段"),
        ]
        self.assertEqual(pick_reply_id(replies, "r_2"), "r_2")
        self.assertEqual(pick_reply_id(replies, ""), "r_2")
        self.assertEqual(pick_reply_id([], "r_1"), "")

    def test_its_own_reply_is_never_reprocessed(self):
        # 自动回复里就带着 BloomBot 四个字，不挡就自己触发自己，无限刷屏
        text = format_answer("结论是 A")
        self.assertTrue(is_bot_reply(text))
        self.assertTrue(mentions_bot(text, TRIGGER))  # 正因为这条会命中，才必须先挡


class CommentQuestionTests(unittest.TestCase):
    """存进记忆时用什么问法。标题决定了这条记忆以后还能不能被检索到。"""

    def test_selected_text_beats_a_bare_remember_command(self):
        # 划词评论最常见的样子：选中一段正文，只写一句「记一下」
        self.assertEqual(
            comment_question("记一下", "2 * 3 * 4 里的星号不该变成斜体"),
            "2 * 3 * 4 里的星号不该变成斜体",
        )
        for bare in ("记下来", "记录一下", "存一下", "记住"):
            self.assertEqual(comment_question(bare, "留存率按 7 日计算"), "留存率按 7 日计算")

    def test_a_real_question_is_kept_alongside_the_topic(self):
        self.assertEqual(
            comment_question("这段的口径是什么", "留存率按 7 日计算"),
            "留存率按 7 日计算：这段的口径是什么",
        )

    def test_whole_document_comment_has_no_quote_to_fall_back_on(self):
        self.assertEqual(comment_question("这段的口径是什么", ""), "这段的口径是什么")
        self.assertEqual(comment_question("记一下", ""), "记一下")

    def test_newlines_in_the_selection_are_flattened(self):
        self.assertEqual(comment_question("记一下", " 第一行\n 第二行 "), "第一行 第二行")

    def test_remember_command_carrying_content_keeps_both(self):
        self.assertEqual(
            comment_question("记一下这里改成五天", "三天内完成"),
            "三天内完成：记一下这里改成五天",
        )


class ClassifyTests(unittest.TestCase):
    def test_edit_intents(self):
        for text in ("把「三天」改成「五天」", "这里应该是 2024 年", "补充一句风险说明"):
            self.assertEqual(classify(text), "edit", text)

    def test_questions_stay_questions(self):
        for text in ("这段的口径是什么", "这里怎么改？", "我们要改吗"):
            self.assertEqual(classify(text), "ask", text)


class ConfirmationTests(unittest.TestCase):
    def test_explicit_yes(self):
        for text in ("确认", "确认。", "同意", "改吧", "OK", "可以改"):
            self.assertTrue(is_confirmation(text), text)

    def test_a_sentence_starting_with_confirm_is_not_a_yes(self):
        # 「确认一下这个数对不对」按前缀匹配就会被当成放行，真去改文档
        for text in ("确认一下这个数对不对", "同意的话我再说", "可以先看看别的吗"):
            self.assertFalse(is_confirmation(text), text)

    def test_explicit_no(self):
        for text in ("算了", "取消", "先不改"):
            self.assertTrue(is_cancellation(text), text)


class ReplacementTests(unittest.TestCase):
    def test_quoted_old_text_is_replaced_in_place(self):
        block = "交付周期三天，超时自动升级。"
        new, why = extract_replacement("把「三天」改成「五天」", block)
        self.assertEqual(new, "交付周期五天，超时自动升级。")
        self.assertIn("五天", why)

    def test_without_a_quote_the_whole_block_is_rewritten(self):
        new, _ = extract_replacement("改成：交付周期五天", "交付周期三天")
        self.assertEqual(new, "交付周期五天")

    def test_vague_instructions_fall_through_to_the_model(self):
        self.assertEqual(extract_replacement("这段读着别扭，顺一下", "原文"), ("", ""))

    def test_append_intent(self):
        self.assertTrue(wants_append("在文末补充一段风险说明"))
        self.assertFalse(wants_append("把三天改成五天"))
        self.assertEqual(extract_append_text("在文末补充 风险说明：注意超时"), "风险说明：注意超时")


class ProposalTextTests(unittest.TestCase):
    def test_proposal_says_it_has_not_changed_anything_yet(self):
        text = format_proposal(EditPlan(block_id="b1", old_text="旧", new_text="新"))
        self.assertIn(BOT_PREFIX, text)
        self.assertIn("还没有改", text)
        self.assertIn("确认", text)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = DocBotState(Path(self.dir.name) / "doc_bot_state.json")

    def tearDown(self):
        self.dir.cleanup()

    def test_a_replayed_event_is_only_handled_once(self):
        self.assertTrue(self.state.check_and_mark("c_1:r_1"))
        self.assertFalse(self.state.check_and_mark("c_1:r_1"))
        self.assertTrue(self.state.check_and_mark("c_1:r_2"))

    def test_dedup_survives_a_restart(self):
        self.state.check_and_mark("c_1:r_1")
        again = DocBotState(self.state.path)
        self.assertFalse(again.check_and_mark("c_1:r_1"))

    def test_pending_can_only_be_taken_once(self):
        self.state.put_pending(EditProposal(comment_id="c_1", block_id="b", new_text="新"))
        self.assertIsNotNone(self.state.take_pending("c_1"))
        self.assertIsNone(self.state.take_pending("c_1"))

    def test_stale_proposals_expire(self):
        old = EditProposal(comment_id="c_1", new_text="新", created_at=time.time() - 48 * 3600)
        self.state.put_pending(old)
        self.assertIsNone(self.state.peek_pending("c_1"))

    def test_a_corrupt_state_file_does_not_crash_the_bot(self):
        self.state.path.write_text("{ not json", encoding="utf-8")
        self.assertTrue(self.state.check_and_mark("c_1:r_1"))


# ---------- 端到端（全部依赖注入，不联网） ----------


class FakeResult(SimpleNamespace):
    pass


class FakeDocApi:
    """假的飞书文档侧：记录每一次调用，好断言「没确认时一个字都没写」。"""

    def __init__(self, *, quote: str = "交付周期三天", block_text: str = "交付周期三天，超时升级。"):
        self.quote = quote
        self.block_text = block_text
        self.posted: list = []
        self.posted_as_app: list = []
        self.edits: list = []
        self.appends: list = []
        self.reactions: list = []
        self.reaction_error = ""
        self.locate_error = ""
        self.reply_ok = True

    def get_comment(self, cfg, ref, comment_id, config_path=None):
        return FakeResult(
            ok=True,
            title="需求文档",
            comments=[
                SimpleNamespace(
                    comment_id=comment_id,
                    quote=self.quote,
                    reply_items=[SimpleNamespace(reply_id="r_1", text=self.text)],
                )
            ],
            error="",
        )

    def reply(
        self, cfg, ref, text, *, comment_id="", config_path=None, confirmed=False, as_app=False
    ):
        assert confirmed, "回评论必须显式 confirmed"
        if not self.reply_ok:
            return FakeResult(ok=False, error="没有评论权限")
        self.posted.append((comment_id, text))
        self.posted_as_app.append(as_app)
        return FakeResult(ok=True, comment_id="c_new", error="")

    def read_doc(self, cfg, ref, config_path=None):
        return FakeResult(ok=True, title="需求文档", content="交付周期三天，超时升级。")

    def find_block(self, cfg, ref, needle, config_path=None):
        if self.locate_error:
            raise RuntimeError(self.locate_error)
        return "b_1", self.block_text

    def edit_block(self, cfg, ref, block_id, new_text, *, expect_text="", config_path=None, confirmed=False):
        assert confirmed, "改正文必须显式 confirmed"
        self.edits.append((block_id, new_text, expect_text))
        return FakeResult(ok=True, error="")

    def append_body(self, cfg, ref, content, *, mode="append", config_path=None, confirmed=False):
        assert confirmed, "改正文必须显式 confirmed"
        self.appends.append((content, mode))
        return FakeResult(ok=True, error="")

    def react(self, cfg, ref, reply_id, reaction_type, *, action="add", config_path=None, confirmed=False):
        assert confirmed, "贴表情也是写操作，必须显式 confirmed"
        if self.reaction_error:
            raise RuntimeError(self.reaction_error)
        self.reactions.append((action, reply_id, reaction_type))
        return True


class FakeSandbox:
    def __init__(self, answer: str = "结论：口径以文档为准。"):
        self.llm = SimpleNamespace(generate=lambda prompt, context="", on_progress=None: answer)
        self.remembered: list = []
        self.queued_docs: list = []

    def build_reference_pack(self, query, top_k=5):
        return {"references": [{"question": "旧问法", "answer": "旧答案"}]}

    def remember(self, question, answer, scene=None, tags=None, **kwargs):
        self.remembered.append((question, answer, scene, tags))
        return "已写入"

    def queue_knowledge_doc(self, token, *, url="", origin="manual", scene="general", force=False):
        self.queued_docs.append((token, url, origin, force))
        return True


class HandleCommentTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.state = DocBotState(Path(self.dir.name) / "state.json")
        self.api = FakeDocApi()
        self.sandbox = FakeSandbox()

    def tearDown(self):
        self.dir.cleanup()

    def _config(self, **kwargs):
        from core.config import AppConfig, FeishuConfig

        cfg = AppConfig()
        cfg.feishu = FeishuConfig(
            app_id="cli_x",
            app_secret="s",
            doc_bot_enabled=True,
            doc_bot_trigger=TRIGGER,
            doc_bot_ack_after_seconds=0,  # 单测不等 8 秒
            **kwargs,
        )
        return cfg

    def _run(self, text, *, payload=None, config=None, allow=(ME,), **kwargs):
        import feishu_bot

        self.api.text = text
        return feishu_bot.handle_comment(
            payload or comment_event(),
            sandbox=self.sandbox,
            config=config or self._config(),
            allow=list(allow),
            state=self.state,
            get_comment=self.api.get_comment,
            reply=self.api.reply,
            read_doc=self.api.read_doc,
            find_block=self.api.find_block,
            edit_block=self.api.edit_block,
            append_body=self.api.append_body,
            react=self.api.react,
            **kwargs,
        )

    # ---- 门禁 ----

    def test_disabled_by_default_config(self):
        from core.config import AppConfig, FeishuConfig

        cfg = AppConfig()
        cfg.feishu = FeishuConfig(app_id="x", app_secret="y")
        self.assertEqual(self._run("@BloomBot 在吗", config=cfg), "skip:disabled")
        self.assertEqual(self.api.posted, [])

    def test_strangers_get_silence_not_an_explanation(self):
        # 评论整篇文档的人都看得见，对陌生人解释白名单等于把这套东西广播出去
        got = self._run("@BloomBot 在吗", payload=comment_event(open_id="ou_other"))
        self.assertEqual(got, "skip:not-allowed")
        self.assertEqual(self.api.posted, [])

    def test_replayed_events_are_dropped(self):
        self._run("@BloomBot 这段的口径是什么")
        self.assertEqual(self._run("@BloomBot 这段的口径是什么"), "skip:duplicate")

    def test_no_trigger_no_answer(self):
        self.assertEqual(self._run("这段我也看不懂"), "skip:no-trigger")
        self.assertEqual(self.api.posted, [])

    def test_it_does_not_answer_itself(self):
        self.assertEqual(self._run(format_answer("结论是 A")), "skip:self")
        self.assertEqual(self.api.posted, [])

    def test_non_docx_files_are_skipped(self):
        got = self._run("@BloomBot 改一下", payload=comment_event(file_type="sheet"))
        self.assertEqual(got, "skip:file_type=sheet")

    # ---- 回答 ----

    def test_question_is_answered_in_the_same_thread(self):
        got = self._run("@BloomBot 这段的口径是什么")
        self.assertEqual(got, "answered")
        comment_id, text = self.api.posted[0]
        self.assertEqual(comment_id, "c_1")
        self.assertIn("口径以文档为准", text)
        self.assertIn(BOT_PREFIX, text)

    def test_the_conclusion_goes_into_the_memory(self):
        self._run("@BloomBot 这段的口径是什么")
        self.assertTrue(self.sandbox.remembered)
        question, answer, scene, tags = self.sandbox.remembered[0]
        # 划词评论的问法以选中的原文为主题，指令只是提问角度
        self.assertEqual(question, "交付周期三天：这段的口径是什么")
        self.assertIn("doc-comment", tags)
        self.assertEqual(scene, "dev")

    def test_a_bare_remember_command_is_titled_by_the_selected_text(self):
        """踩过：选中一段正文只写「记一下」，存出来的记忆标题就叫「记一下」，检索不到。"""
        self._run("@BloomBot 记一下")
        question, _answer, _scene, _tags = self.sandbox.remembered[0]
        self.assertEqual(question, "交付周期三天")

    def test_a_broken_model_is_explained_not_swallowed(self):
        def boom(prompt, context="", on_progress=None):
            raise RuntimeError("agent 超时")

        self.sandbox.llm = SimpleNamespace(generate=boom)
        self.assertEqual(self._run("@BloomBot 这段的口径是什么"), "error:llm")
        self.assertIn("agent 超时", self.api.posted[0][1])

    def test_without_a_model_it_falls_back_to_the_memory(self):
        self.sandbox.llm = None
        self.assertEqual(self._run("@BloomBot 这段的口径是什么"), "answered")
        self.assertIn("旧答案", self.api.posted[0][1])

    # ---- 改动：提案 → 确认 → 落笔 ----

    def test_an_edit_request_only_gets_a_proposal(self):
        got = self._run("@BloomBot 把「三天」改成「五天」")
        self.assertEqual(got, "proposed")
        self.assertEqual(self.api.edits, [], "没确认之前一个字都不许写")
        self.assertIn("还没有改", self.api.posted[0][1])
        pending = self.state.peek_pending("c_1")
        self.assertEqual(pending.new_text, "交付周期五天，超时升级。")

    def test_confirmation_applies_the_pending_change(self):
        self._run("@BloomBot 把「三天」改成「五天」")
        got = self._run("确认", payload=comment_event(reply_id="r_2"))
        self.assertEqual(got, "applied")
        block_id, new_text, expect = self.api.edits[0]
        self.assertEqual(block_id, "b_1")
        self.assertEqual(new_text, "交付周期五天，超时升级。")
        # 提案与落笔之间别人可能改过，必须带着原文让接口比对
        self.assertEqual(expect, "交付周期三天，超时升级。")
        self.assertIn("已改", self.api.posted[-1][1])

    def test_confirming_twice_does_not_write_twice(self):
        self._run("@BloomBot 把「三天」改成「五天」")
        self._run("确认", payload=comment_event(reply_id="r_2"))
        self._run("确认", payload=comment_event(reply_id="r_3"))
        self.assertEqual(len(self.api.edits), 1)

    def test_cancelling_drops_the_proposal(self):
        self._run("@BloomBot 把「三天」改成「五天」")
        got = self._run("算了", payload=comment_event(reply_id="r_2"))
        self.assertEqual(got, "cancelled")
        self.assertEqual(self.api.edits, [])
        self.assertIsNone(self.state.peek_pending("c_1"))

    def test_a_half_hearted_yes_gets_a_nudge_not_an_edit(self):
        self._run("@BloomBot 把「三天」改成「五天」")
        got = self._run("确认一下这个数对不对", payload=comment_event(reply_id="r_2"))
        self.assertEqual(got, "nudged")
        self.assertEqual(self.api.edits, [])

    # ---- 知识库 ----

    def test_replying_pulls_the_document_into_the_knowledge_base(self):
        self.assertEqual(self._run("@BloomBot 这段的口径是什么"), "answered")
        # 事件里只有 file_token，没有链接：入库按 token 走，链接由入库层跟飞书补要
        self.assertEqual(self.sandbox.queued_docs, [("docx_token", "", "doc-comment", False)])

    def test_staying_silent_pulls_nothing(self):
        """没被 @ 就没说话，也就不该把别人的文档收进自己的知识库。"""
        self.assertEqual(self._run("这段我也看不懂"), "skip:no-trigger")
        self.assertEqual(self.sandbox.queued_docs, [])

    def test_a_reply_that_never_landed_pulls_nothing(self):
        self.api.reply_ok = False
        self._run("@BloomBot 这段的口径是什么")
        self.assertEqual(self.sandbox.queued_docs, [])

    def test_two_replies_in_one_turn_still_pull_once(self):
        # 表情贴不上时会多发一条「收到」，那也是一次 post，不能因此入库两次
        self.api.reaction_error = "没开表情权限"

        def slow(prompt, context="", on_progress=None):
            time.sleep(0.15)
            return "结论：口径以文档为准。"

        self.sandbox.llm = SimpleNamespace(generate=slow)
        self._run("@BloomBot 这段的口径是什么", ack_delay=0.01)
        self.assertGreaterEqual(len(self.api.posted), 2)
        self.assertEqual(len(self.sandbox.queued_docs), 1)

    def test_applying_an_edit_forces_a_refetch(self):
        """机器人自己刚改过正文，库里那份必须重抓，否则留的是被它推翻的旧正文。"""
        self._run("@BloomBot 把「三天」改成「五天」")
        self.sandbox.queued_docs.clear()
        self.assertEqual(self._run("确认", payload=comment_event(reply_id="r_2")), "applied")
        self.assertEqual([q[3] for q in self.sandbox.queued_docs], [True])

    def test_a_broken_knowledge_layer_does_not_break_the_reply(self):
        def boom(*a, **kw):
            raise RuntimeError("知识库炸了")

        self.sandbox.queue_knowledge_doc = boom
        self.assertEqual(self._run("@BloomBot 这段的口径是什么"), "answered")
        self.assertTrue(self.api.posted)

    def test_a_failed_write_is_reported(self):
        self._run("@BloomBot 把「三天」改成「五天」")
        self.api.edit_block = lambda *a, **k: FakeResult(ok=False, error="文档已被改过")
        got = self._run("确认", payload=comment_event(reply_id="r_2"))
        self.assertEqual(got, "error:apply")
        self.assertIn("文档已被改过", self.api.posted[-1][1])

    def test_whole_document_comments_cannot_be_located(self):
        self.api.quote = ""
        got = self._run("@BloomBot 把「三天」改成「五天」")
        self.assertEqual(got, "cannot-edit")
        self.assertIn("划词评论", self.api.posted[0][1])
        self.assertEqual(self.api.edits, [])

    def test_ambiguous_anchors_refuse_instead_of_guessing(self):
        self.api.locate_error = "命中 3 个块"
        got = self._run("@BloomBot 把「三天」改成「五天」")
        self.assertEqual(got, "cannot-edit")
        self.assertIn("命中 3 个块", self.api.posted[0][1])

    def test_append_needs_no_anchor(self):
        self.api.quote = ""
        self.assertEqual(self._run("@BloomBot 在文末补充 风险说明：注意超时"), "proposed")
        self._run("确认", payload=comment_event(reply_id="r_2"))
        self.assertEqual(self.api.appends, [("风险说明：注意超时", "append")])
        self.assertEqual(self.api.edits, [])

    def test_the_model_rewrites_when_the_instruction_is_vague(self):
        self.sandbox.llm = SimpleNamespace(
            generate=lambda prompt, context="", on_progress=None: "```\n交付周期五个工作日。\n```"
        )
        self.assertEqual(self._run("@BloomBot 这句建议改一下，读着别扭"), "proposed")
        self.assertEqual(self.state.peek_pending("c_1").new_text, "交付周期五个工作日。")

    def test_the_bot_speaks_as_the_app_not_as_me(self):
        # 评论接口两种 token 都收，署名跟着 token 走：用 user token 发，
        # 机器人的话在文档里就署本人的名字，只能靠正文前缀自证
        self._run("@BloomBot 这段的口径是什么")
        self.assertTrue(all(self.api.posted_as_app), self.api.posted_as_app)

    # ---- 表情当进度条 ----

    def test_answering_marks_working_then_done(self):
        self.assertEqual(self._run("@BloomBot 这段的口径是什么"), "answered")
        # 先贴「处理中」，答完补终态、再撤掉「处理中」——顺序反了中间断线会一个表情都不剩
        self.assertEqual(
            self.api.reactions,
            [
                ("add", "r_1", "Typing"),
                ("add", "r_1", "CheckMark"),
                ("delete", "r_1", "Typing"),
            ],
        )

    def test_a_dead_end_switches_to_the_failed_face(self):
        self.sandbox.llm = None
        self.sandbox.build_reference_pack = lambda q, top_k=5: {"references": []}
        self.assertEqual(self._run("@BloomBot 这段的口径是什么"), "no-answer")
        self.assertIn(("add", "r_1", "CrossMark"), self.api.reactions)

    def test_applying_a_confirmed_edit_also_shows_progress(self):
        self._run("@BloomBot 把「三天」改成「五天」")
        self.api.reactions.clear()
        self.assertEqual(self._run("确认", payload=comment_event(reply_id="r_2")), "applied")
        self.assertIn(("add", "r_1", "CheckMark"), self.api.reactions)

    def test_unrelated_replies_get_no_reaction(self):
        self.assertEqual(self._run("这段我也觉得怪"), "skip:no-trigger")
        self.assertEqual(self.api.reactions, [])

    def test_the_text_ack_only_fires_when_the_reaction_could_not_be_pinned(self):
        def slow(prompt, context="", on_progress=None):
            time.sleep(0.15)  # 慢到足够让「收到」定时器有机会开火
            return "结论：口径以文档为准。"

        self.sandbox.llm = SimpleNamespace(generate=slow)
        # 表情贴上了就别再刷「收到」：那条回复整篇文档的协作者都看得见
        self._run("@BloomBot 这段的口径是什么", ack_delay=0.01)
        self.assertNotIn("收到", "".join(t for _c, t in self.api.posted))

        self.api.reaction_error = "99991672 权限没开"
        self.api.posted.clear()
        self._run(
            "@BloomBot 这段的口径是什么",
            payload=comment_event(reply_id="r_9"),
            ack_delay=0.01,
        )
        self.assertTrue(
            any("收到" in t for _c, t in self.api.posted),
            "贴不上表情时必须退回文字回执，否则慢起来用户完全没反馈",
        )


class DocApiTests(unittest.TestCase):
    """三个新接口的 HTTP 层：URL、body 和门禁。"""

    def _cfg(self):
        from core.config import FeishuConfig

        return FeishuConfig(
            enabled=True, app_id="cli_x", app_secret="s", api_base="https://open.feishu.cn"
        )

    def _ref(self):
        from core.feishu import FeishuDocRef

        return FeishuDocRef(url="https://x.feishu.cn/docx/doc1", kind="docx", token="doc1")

    def test_subscribe_posts_the_event_type(self):
        from core import feishu

        captured = {}

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            captured.update(method=method, url=url, body=body)
            return {"code": 0, "data": {}}

        with patch.object(feishu, "_with_user_token", side_effect=lambda c, p, f: ("t", f("t"))), patch.object(
            feishu, "_http_json", side_effect=fake_http
        ):
            feishu.subscribe_user_doc_events(self._cfg())

        self.assertTrue(captured["url"].endswith("/drive/v1/user/subscription"))
        self.assertEqual(captured["body"], {"event_type": "drive.notice.comment_add_v1"})

    def test_get_file_comment_uses_batch_query(self):
        from core import feishu

        captured = {}

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            captured.update(method=method, url=url, body=body)
            return {
                "code": 0,
                "data": {
                    "items": [
                        {
                            "comment_id": "c_1",
                            "quote": "交付周期三天",
                            "reply_list": {
                                "replies": [
                                    {"reply_id": "r_1", "user_id": "u1", "content": {
                                        "elements": [{"type": "text_run", "text_run": {"text": "@BloomBot 看下"}}]
                                    }}
                                ]
                            },
                        }
                    ]
                },
            }

        with patch.object(feishu, "_with_user_token", side_effect=lambda c, p, f: ("t", f("t"))), patch.object(
            feishu, "_resolve_document_id", return_value=("doc1", "需求文档")
        ), patch.object(feishu, "_http_json", side_effect=fake_http):
            got = feishu.get_file_comment(self._cfg(), self._ref(), "c_1")

        self.assertTrue(got.ok)
        self.assertIn("/comments/batch_query", captured["url"])
        self.assertEqual(captured["body"], {"comment_ids": ["c_1"]})
        self.assertEqual(got.comments[0].quote, "交付周期三天")
        self.assertEqual(got.comments[0].reply_items[0].reply_id, "r_1")

    def test_reaction_hangs_off_the_reply_not_the_comment(self):
        from core import feishu

        captured = {}

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            captured.update(method=method, url=url, body=body)
            return {"code": 0, "data": {}}

        with patch.object(
            feishu, "_with_user_token", side_effect=lambda c, p, f: ("t", f("t"))
        ), patch.object(
            feishu, "_resolve_document_id", return_value=("doc1", "需求文档")
        ), patch.object(feishu, "_http_json", side_effect=fake_http):
            ok = feishu.update_comment_reaction(
                self._cfg(), self._ref(), "r_1", "Typing", confirmed=True
            )

        self.assertTrue(ok)
        self.assertIn("/drive/v2/files/doc1/comments/reaction", captured["url"])
        self.assertIn("file_type=docx", captured["url"])
        self.assertEqual(
            captured["body"],
            {"action": "add", "reply_id": "r_1", "reaction_type": "Typing"},
        )

    def test_reaction_is_pinned_as_the_app_so_it_is_not_signed_with_my_name(self):
        from core import feishu

        seen = []

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            seen.append((headers or {}).get("Authorization"))
            return {"code": 0, "data": {}}

        with patch.object(
            feishu, "_with_user_token", side_effect=lambda c, p, f: ("u-mine", f("u-mine"))
        ), patch.object(
            feishu, "_resolve_document_id", return_value=("doc1", "需求文档")
        ), patch.object(
            feishu, "_tenant_access_token", return_value="t-app"
        ), patch.object(feishu, "_http_json", side_effect=fake_http):
            feishu.update_comment_reaction(
                self._cfg(), self._ref(), "r_1", "Typing", confirmed=True
            )

        self.assertEqual(seen, ["Bearer t-app"], "应用身份能用时不该退回本人 token")

    def test_reaction_falls_back_to_my_token_when_the_app_has_no_access(self):
        from core import feishu

        seen = []

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            auth = (headers or {}).get("Authorization")
            seen.append(auth)
            if auth == "Bearer t-app":
                return {"code": 1061004, "msg": "no permission"}
            return {"code": 0, "data": {}}

        with patch.object(
            feishu, "_with_user_token", side_effect=lambda c, p, f: ("u-mine", f("u-mine"))
        ), patch.object(
            feishu, "_resolve_document_id", return_value=("doc1", "需求文档")
        ), patch.object(
            feishu, "_tenant_access_token", return_value="t-app"
        ), patch.object(feishu, "_http_json", side_effect=fake_http):
            ok = feishu.update_comment_reaction(
                self._cfg(), self._ref(), "r_1", "Typing", confirmed=True
            )

        self.assertTrue(ok)
        self.assertEqual(seen, ["Bearer t-app", "Bearer u-mine"])

    def test_as_app_comments_are_posted_by_the_bot(self):
        from core import feishu

        seen = []

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            seen.append((headers or {}).get("Authorization"))
            return {"code": 0, "data": {"comment_id": "c_new"}}

        with patch.object(
            feishu, "_with_user_token", side_effect=lambda c, p, f: ("u-mine", f("u-mine"))
        ), patch.object(
            feishu, "_resolve_document_id", return_value=("doc1", "需求文档")
        ), patch.object(
            feishu, "_tenant_access_token", return_value="t-app"
        ), patch.object(feishu, "_http_json", side_effect=fake_http):
            as_me = feishu.create_docx_comment(
                self._cfg(), self._ref(), "本人身份", comment_id="c_1", confirmed=True
            )
            as_bot = feishu.create_docx_comment(
                self._cfg(),
                self._ref(),
                "机器人身份",
                comment_id="c_1",
                confirmed=True,
                as_app=True,
            )

        self.assertTrue(as_me.ok and as_bot.ok)
        # 默认仍是本人身份：AI 代做评审时那些意见是你在说话
        self.assertEqual(seen, ["Bearer u-mine", "Bearer t-app"])
        self.assertEqual(as_bot.replied_to, "c_1", "退回路径拼的字段不能在应用身份下丢")

    def test_reaction_refuses_without_confirmation(self):
        from core import feishu

        with patch.object(feishu, "_http_json", side_effect=AssertionError("不该发请求")):
            self.assertFalse(
                feishu.update_comment_reaction(self._cfg(), self._ref(), "r_1", "Typing")
            )

    def test_block_update_refuses_without_confirmation(self):
        from core import feishu

        with patch.object(feishu, "_http_json", side_effect=AssertionError("不该发请求")):
            res = feishu.update_docx_block_text(self._cfg(), self._ref(), "b1", "新内容")
        self.assertFalse(res.ok)
        self.assertIn("确认", res.error)

    def test_block_update_patches_the_single_block(self):
        from core import feishu

        captured = {}

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            if method == "GET":
                return {"code": 0, "data": {"block": {"block_type": 2, "text": {"elements": [
                    {"text_run": {"content": "交付周期三天"}}
                ]}}}}
            captured.update(method=method, url=url, body=body)
            return {"code": 0, "data": {}}

        with patch.object(feishu, "_with_user_token", side_effect=lambda c, p, f: ("t", f("t"))), patch.object(
            feishu, "_resolve_document_id", return_value=("doc1", "需求文档")
        ), patch.object(feishu, "_http_json", side_effect=fake_http):
            res = feishu.update_docx_block_text(
                self._cfg(), self._ref(), "b1", "交付周期五天", confirmed=True
            )

        self.assertTrue(res.ok, res.error)
        self.assertEqual(captured["method"], "PATCH")
        self.assertTrue(captured["url"].endswith("/documents/doc1/blocks/b1"))
        self.assertEqual(
            captured["body"]["update_text_elements"]["elements"][0]["text_run"]["content"],
            "交付周期五天",
        )
        self.assertEqual(res.old_text, "交付周期三天")

    def test_block_update_refuses_when_the_text_moved_on(self):
        # 提案和落笔之间别人改过同一段，覆盖掉才是真事故
        from core import feishu

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            if method == "GET":
                return {"code": 0, "data": {"block": {"block_type": 2, "text": {"elements": [
                    {"text_run": {"content": "交付周期七天"}}
                ]}}}}
            raise AssertionError("不该发 PATCH")

        with patch.object(feishu, "_with_user_token", side_effect=lambda c, p, f: ("t", f("t"))), patch.object(
            feishu, "_resolve_document_id", return_value=("doc1", "需求文档")
        ), patch.object(feishu, "_http_json", side_effect=fake_http):
            res = feishu.update_docx_block_text(
                self._cfg(),
                self._ref(),
                "b1",
                "交付周期五天",
                expect_text="交付周期三天",
                confirmed=True,
            )

        self.assertFalse(res.ok)
        self.assertIn("已被别人改过", res.error)

    def test_block_update_refuses_non_text_blocks(self):
        from core import feishu

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            if method == "GET":
                return {"code": 0, "data": {"block": {"block_type": 31}}}
            raise AssertionError("不该发 PATCH")

        with patch.object(feishu, "_with_user_token", side_effect=lambda c, p, f: ("t", f("t"))), patch.object(
            feishu, "_resolve_document_id", return_value=("doc1", "需求文档")
        ), patch.object(feishu, "_http_json", side_effect=fake_http):
            res = feishu.update_docx_block_text(
                self._cfg(), self._ref(), "b1", "新内容", confirmed=True
            )

        self.assertFalse(res.ok)
        self.assertIn("不是文本块", res.error)


if __name__ == "__main__":
    unittest.main()
