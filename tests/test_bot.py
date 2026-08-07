"""飞书机器人纯逻辑层的单测：不连飞书、不装 SDK 也能跑。"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.bot import (  # noqa: E402
    MAX_REPLY_CHARS,
    SeenMessages,
    authorize,
    message_text,
    parse_command,
    parse_event,
    respond,
)

ME = "ou_me"


CARD = json.dumps(
    {
        "header": {"title": {"tag": "plain_text", "content": "报警归因"}},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": "结论：签约日期过期"}},
            {"tag": "img", "img_key": "img_x"},
            {
                "tag": "column_set",
                "columns": [
                    {"elements": [{"tag": "markdown", "content": "错误码 4030066"}]}
                ],
            },
        ],
    }
)


def event(
    text: str,
    *,
    open_id: str = ME,
    message_id: str = "om_1",
    chat_type: str = "p2p",
    message_type: str = "text",
    sender_type: str = "user",
    content: str = "",
    parent_id: str = "",
    mentions=None,
) -> dict:
    message = {
        "message_id": message_id,
        "chat_id": "oc_1",
        "chat_type": chat_type,
        "message_type": message_type,
        "content": content or json.dumps({"text": text}),
    }
    if parent_id:
        message["parent_id"] = parent_id
    if mentions:
        message["mentions"] = [
            {"key": f"@_user_{i}", "id": {"open_id": oid}, "name": name}
            for i, (oid, name) in enumerate(mentions, start=1)
        ]
    return {
        "schema": "2.0",
        "header": {"event_type": "im.message.receive_v1"},
        "event": {
            "sender": {
                "sender_id": {"open_id": open_id},
                "sender_type": sender_type,
            },
            "message": message,
        },
    }


class FakeSandbox:
    def __init__(
        self,
        *,
        hit: bool = False,
        refs=None,
        hit_answer: str = "命中的答案",
        hit_id: str = "lt1",
    ) -> None:
        self.hit = hit
        self.refs = refs if refs is not None else []
        self.hit_answer = hit_answer
        self.hit_id = hit_id
        self.asked: list = []
        self.remembered: list = []
        self.reloaded = 0
        self.last_remembered = None
        self.last_remembered_updated = False
        self.long_term = SimpleNamespace(reload=self._reload)
        self.config = SimpleNamespace(sandbox=SimpleNamespace(default_scene="general"))

    def _reload(self) -> None:
        self.reloaded += 1

    def ask_local(self, text):
        self.asked.append(text)
        if self.hit:
            hits = [{"id": self.hit_id, "answer": self.hit_answer}] if self.hit_id else []
            return SimpleNamespace(
                source="long_term",
                answer=self.hit_answer,
                meta={"hit_local": True, "hits": hits},
            )
        return SimpleNamespace(source="miss", answer="", meta={"hit_local": False})

    def build_reference_pack(self, query, top_k=5):
        return {"references": list(self.refs)[:top_k]}

    def remember(self, question, answer, scene=None, **kwargs):
        self.remembered.append((question, answer, scene))
        self.last_remembered = SimpleNamespace(question=question)
        return "已写入长时记忆"

    def status(self):
        return {
            "long_term": {"declarative_count": 42, "procedural_count": 3},
            "working": {"size": 1, "max_size": 10, "scene": "dev"},
        }


class ParseEventTests(unittest.TestCase):
    def test_plain_text(self):
        got = parse_event(event("客服文档在哪"))
        self.assertIsNotNone(got)
        self.assertEqual(got.text, "客服文档在哪")
        self.assertEqual(got.open_id, ME)
        self.assertEqual(got.message_id, "om_1")

    def test_group_mention_placeholder_is_stripped(self):
        got = parse_event(event("@_user_1 客服文档在哪", chat_type="group"))
        self.assertEqual(got.text, "客服文档在哪")

    def test_bot_own_message_is_ignored(self):
        # 机器人自己发的消息也会回推，处理了就死循环
        self.assertIsNone(parse_event(event("hi", sender_type="app")))

    def test_another_bot_gets_through_once_we_know_who_we_are(self):
        # 开了「获取群组中其他机器人和用户@当前机器人的消息」之后，
        # Slardar 这类机器人能直接驱动落库，不必等人转发一手
        got = parse_event(
            event("记一下：接口超时的根因是连接池打满", open_id="ou_slardar", sender_type="app"),
            self_open_id=ME,
        )
        self.assertIsNotNone(got)
        self.assertTrue(got.from_bot)
        self.assertEqual(got.open_id, "ou_slardar")

    def test_our_own_message_still_dropped_even_knowing_ourselves(self):
        payload = event("我刚回的那句", open_id=ME, sender_type="app")
        self.assertIsNone(parse_event(payload, self_open_id=ME))

    def test_unknown_self_id_keeps_dropping_every_bot(self):
        # 认不出自己就分不清哪条是自己发的，放行等于开死循环。宁可漏接
        payload = event("hi", open_id="ou_slardar", sender_type="app")
        self.assertIsNone(parse_event(payload))
        self.assertIsNone(parse_event(payload, self_open_id="  "))

    def test_non_text_message_is_ignored(self):
        payload = event("", message_type="image", content=json.dumps({"image_key": "x"}))
        self.assertIsNone(parse_event(payload))

    def test_rich_text_is_flattened(self):
        content = json.dumps(
            {
                "title": "标题",
                "content": [
                    [{"tag": "at", "user_id": "x"}, {"tag": "text", "text": "第一行"}],
                    [{"tag": "text", "text": "第二行"}],
                ],
            }
        )
        got = parse_event(event("", message_type="post", content=content))
        self.assertEqual(got.text, "标题\n第一行\n第二行")

    def test_parent_id_is_kept(self):
        got = parse_event(event("@_user_1 记一下这个结论", chat_type="group", parent_id="om_card"))
        self.assertEqual(got.parent_id, "om_card")

    def test_card_is_flattened_without_images(self):
        got = message_text("interactive", CARD)
        self.assertEqual(got, "报警归因\n结论：签约日期过期\n错误码 4030066")

    def test_card_links_survive_as_markdown(self):
        """
        报警卡片的「报警详情」「下钻分析」都挂在 href 上。只取 text 的话，
        存进记忆的结论是条断头路——回头没法点回原地复查。
        """
        content = json.dumps(
            {
                "elements": [
                    [
                        {"tag": "text", "text": "错误码 "},
                        {"tag": "a", "text": "4030066", "href": "https://slardar/x?id=1"},
                        {"tag": "a", "href": "https://slardar/naked"},
                    ]
                ]
            }
        )
        got = message_text("interactive", content)
        self.assertIn("[4030066](https://slardar/x?id=1)", got)
        self.assertIn("https://slardar/naked", got)  # 没有文案的链接也别丢

    def test_rich_text_links_survive_too(self):
        content = json.dumps(
            {
                "title": "标题",
                "content": [
                    [
                        {"tag": "text", "text": "详情见"},
                        {"tag": "a", "text": "报警", "href": "https://slardar/a"},
                    ]
                ],
            }
        )
        got = message_text("post", content)
        self.assertEqual(got, "标题\n详情见[报警](https://slardar/a)")

    def test_a_row_repeated_in_summary_and_detail_is_kept_once(self):
        """报警卡片爱把同一行写两遍，带上链接后长度直接翻倍。"""
        row = {"tag": "a", "text": "4030066", "href": "https://slardar/x"}
        got = message_text("interactive", json.dumps({"elements": [[row], [row]]}))
        self.assertEqual(got.count("4030066"), 1)


class AuthorizeTests(unittest.TestCase):
    def test_empty_allowlist_hands_back_the_open_id(self):
        # 先有鸡还是先有蛋：用户不配白名单就拿不到 open_id，拿不到就配不了
        msg = authorize(ME, [])
        self.assertIn(ME, msg)

    def test_stranger_is_refused_without_leaking_anything(self):
        msg = authorize("ou_other", [ME])
        self.assertIsNotNone(msg)
        self.assertNotIn("ou_other", msg)

    def test_allowed_user_passes(self):
        self.assertIsNone(authorize(ME, [" ou_me ", ""]))

    def test_stranger_never_reaches_the_memory(self):
        sb = FakeSandbox(hit=True)
        out = respond(sb, event("客服文档", open_id="ou_other"), allow=[ME])
        self.assertNotIn("命中的答案", out.text)
        self.assertEqual(sb.asked, [])


class ParseCommandTests(unittest.TestCase):
    def test_default_is_ask(self):
        cmd = parse_command("客服文档在哪")
        self.assertEqual(cmd.kind, "ask")
        self.assertEqual(cmd.query, "客服文档在哪")

    def test_multiline_remember(self):
        cmd = parse_command("记一下：长连接怎么保存\n先跑本地进程\n再回后台点保存")
        self.assertEqual(cmd.kind, "remember")
        self.assertEqual(cmd.question, "长连接怎么保存")
        self.assertEqual(cmd.answer, "先跑本地进程\n再回后台点保存")

    def test_single_line_with_separator(self):
        cmd = parse_command("记住 长连接怎么保存 || 先跑本地进程再保存")
        self.assertEqual(cmd.kind, "remember")
        self.assertEqual(cmd.question, "长连接怎么保存")
        self.assertEqual(cmd.answer, "先跑本地进程再保存")

    def test_single_line_is_stored_as_is(self):
        # 逼用户重打一遍「问 => 答」不如先存下来，问答同文照样检索得到
        cmd = parse_command("记一下 长连接怎么保存")
        self.assertEqual(cmd.kind, "remember")
        self.assertEqual(cmd.question, "长连接怎么保存")
        self.assertEqual(cmd.answer, "长连接怎么保存")

    def test_natural_phrasings_are_recognised(self):
        for text in (
            "帮我记住 mock 端口是 3001",
            "把 mock 端口是 3001 记一下",
            "mock 端口是 3001，记下来",
            "存到记忆库：mock 端口是 3001",
        ):
            self.assertEqual(parse_command(text).kind, "remember", text)

    def test_asking_what_it_does_gets_the_manual(self):
        # 问机器人自己是干嘛的，绕一圈 agent 既慢又答不准
        for text in ("你有什么功能？", "能做什么", "你是谁", "帮助", "HELP"):
            self.assertEqual(parse_command(text).kind, "help", text)

    def test_questions_are_still_questions(self):
        for text in ("保存位置在哪", "这个功能怎么记录", "记忆库怎么用"):
            self.assertEqual(parse_command(text).kind, "ask", text)

    def test_bare_deictic_without_a_quote_asks_for_content(self):
        self.assertEqual(parse_command("记一下这个结论").kind, "help")

    def test_quote_supplies_the_answer(self):
        cmd = parse_command("记一下 签约日期报警的根因", quoted="结论：签约日期过期\n错误码 4030066")
        self.assertEqual(cmd.kind, "remember")
        self.assertEqual(cmd.question, "签约日期报警的根因")
        self.assertEqual(cmd.answer, "结论：签约日期过期\n错误码 4030066")
        self.assertTrue(cmd.from_quote)

    def test_deictic_falls_back_to_the_quoted_first_line(self):
        # 「这个结论」只是指代，拿它当问法以后一条都检索不到
        for text in ("记一下这个结论", "记一下", "记住上面这条"):
            cmd = parse_command(text, quoted="结论：签约日期过期\n错误码 4030066")
            self.assertEqual(cmd.kind, "remember", text)
            self.assertEqual(cmd.question, "结论：签约日期过期", text)

    def test_explicit_answer_beats_the_quote(self):
        cmd = parse_command("记一下：问题\n我自己写的答案", quoted="卡片正文")
        self.assertEqual(cmd.answer, "我自己写的答案")
        self.assertFalse(cmd.from_quote)

    def test_status_and_help(self):
        self.assertEqual(parse_command("状态").kind, "status")
        self.assertEqual(parse_command("帮助").kind, "help")


class RespondTests(unittest.TestCase):
    def test_hit_is_returned_with_references(self):
        sb = FakeSandbox(hit=True, refs=[{"question": "客服文档", "answer": "在 wiki"}])
        out = respond(sb, event("客服文档在哪"), allow=[ME])
        self.assertIn("命中的答案", out.text)
        self.assertIn("在 wiki", out.text)
        self.assertEqual(out.reply_to, "om_1")

    def test_the_hit_is_not_echoed_under_related_memories(self):
        """
        软召回用的是同一个 query，命中的那条一定也在软召回里。
        不摘掉它，用户看到的就是「答完又把同一段话抄了一遍」。
        """
        sb = FakeSandbox(
            hit=True,
            hit_answer="端口 3001，改 config.yaml 后重启",
            refs=[
                {"id": "lt1", "question": "mock 端口", "answer": "端口 3001，改 config.yaml 后重启"},
                {"id": "lt2", "question": "另一条", "answer": "另一个结论"},
            ],
        )
        out = respond(sb, event("mock 端口是多少"), allow=[ME])
        self.assertEqual(out.text.count("端口 3001，改 config.yaml 后重启"), 1)
        self.assertIn("另一个结论", out.text)

    def test_no_related_section_when_the_hit_was_the_only_match(self):
        sb = FakeSandbox(
            hit=True,
            hit_answer="就这一条",
            refs=[{"id": "lt1", "question": "问", "answer": "就这一条"}],
        )
        out = respond(sb, event("查一下"), allow=[ME])
        self.assertNotIn("相关记忆", out.text)
        self.assertEqual(out.text.count("就这一条"), 1)

    def test_a_hit_without_an_id_is_caught_by_its_text(self):
        """工作/程序性记忆命中拿不到 id，只能比内容，否则照样会抄一遍。"""
        answer = "改 core/bot.py 的 _do_ask，摘掉命中的那条再列参考"
        sb = FakeSandbox(
            hit=True,
            hit_id="",
            hit_answer=answer,
            refs=[{"question": "怎么改", "answer": answer}],
        )
        out = respond(sb, event("怎么改"), allow=[ME])
        self.assertEqual(out.text.count(answer), 1)

    def test_a_short_reference_swallowed_by_the_answer_is_kept(self):
        """按内容排重不能伤及无辜：短答案碰巧被包含的多半是另一条记忆。"""
        sb = FakeSandbox(
            hit=True,
            hit_answer="端口 3001，回滚看 runbook",
            refs=[{"id": "lt2", "question": "端口", "answer": "3001"}],
        )
        out = respond(sb, event("端口"), allow=[ME])
        self.assertIn("相关记忆", out.text)

    def test_misses_still_list_everything_soft_matched(self):
        sb = FakeSandbox(hit=False, refs=[{"id": "lt1", "question": "沾边的问", "answer": "沾边的答"}])
        out = respond(sb, event("完全没记过的问题"), allow=[ME])
        self.assertIn("没有直接命中", out.text)
        self.assertIn("沾边的答", out.text)

    def test_miss_retries_with_the_original_wording(self):
        # 库里可能存着没带「记录到长期记忆」后缀的旧条目
        sb = FakeSandbox(hit=False)
        respond(sb, event("客服文档在哪"), allow=[ME])
        self.assertEqual(len(sb.asked), 2)
        self.assertNotEqual(sb.asked[0], sb.asked[1])
        self.assertEqual(sb.asked[1], "客服文档在哪")

    def test_miss_without_references_says_so(self):
        out = respond(FakeSandbox(hit=False), event("没人问过的问题"), allow=[ME])
        self.assertIn("没找到", out.text)

    def test_remember_writes_through(self):
        sb = FakeSandbox()
        out = respond(sb, event("记一下：问题\n答案"), allow=[ME])
        self.assertEqual(sb.remembered, [("问题", "答案", "dev")])
        self.assertIn("已记住", out.text)

    def test_status(self):
        out = respond(FakeSandbox(), event("状态"), allow=[ME])
        self.assertIn("42", out.text)

    def test_reply_to_a_card_is_remembered(self):
        sb = FakeSandbox()
        payload = event("@_user_1 记一下这个结论", chat_type="group", parent_id="om_card")
        out = respond(
            sb, payload, allow=[ME], fetch_message=lambda mid: ("interactive", CARD)
        )
        question, answer, scene = sb.remembered[0]
        self.assertEqual(question, "报警归因")
        self.assertIn("结论：签约日期过期", answer)
        self.assertEqual(scene, "dev")
        self.assertIn("你回复的那条消息", out.text)

    def test_quote_is_only_fetched_for_writes(self):
        # 每条群消息都去拉被引用的原文，等于白打一倍接口
        calls = []
        respond(
            FakeSandbox(hit=True),
            event("客服文档在哪", parent_id="om_card"),
            allow=[ME],
            fetch_message=lambda mid: calls.append(mid),
        )
        self.assertEqual(calls, [])

    def test_unreadable_quote_says_why_instead_of_guessing(self):
        sb = FakeSandbox()

        def boom(mid):
            raise RuntimeError("need scope: im:message.group_msg")

        out = respond(
            sb,
            event("记一下这个结论", parent_id="om_card"),
            allow=[ME],
            fetch_message=boom,
        )
        self.assertIn("im:message.group_msg", out.text)
        self.assertEqual(sb.remembered, [])

    def test_a_textless_quote_says_so_instead_of_dumping_help(self):
        """
        实际踩到的：回复一条没有文字的消息说「记下来」，接口给回来的正文是空的。
        当时回的是整段帮助文本，用户只能对着屏幕猜自己哪里说错了。
        """
        sb = FakeSandbox()
        out = respond(
            sb,
            event("记下来", parent_id="om_img"),
            allow=[ME],
            fetch_message=lambda mid: ("image", json.dumps({"image_key": "img_v3"})),
        )
        self.assertIn("没有可提取的文字", out.text)
        self.assertNotIn("这段说明", out.text)  # 别再把帮助文本糊上来
        self.assertEqual(sb.remembered, [])

    def test_quoting_another_apps_card_names_the_real_reason(self):
        """
        别的机器人发的卡片，飞书只给一个带 image_key 的摘要壳，正文一个字都没有。
        说「没有可提取的文字」会让人以为是我们解析坏了，得点破是卡片不开放。
        """
        sb = FakeSandbox()
        summary = {
            "title": None,
            "elements": [[{"tag": "img", "image_key": "img_v3"}, {"tag": "text", "text": " "}]],
        }
        out = respond(
            sb,
            event("记下来", parent_id="om_card"),
            allow=[ME],
            fetch_message=lambda mid: ("interactive", json.dumps(summary)),
        )
        self.assertIn("卡片", out.text)
        self.assertIn("复制", out.text)
        # 转发要排在复制前面：卡的是 sender_type=app，人转发一次就能读到全文，
        # 比让人手抄一大段省事
        self.assertIn("转发", out.text)
        self.assertLess(out.text.index("转发"), out.text.index("复制"))
        self.assertEqual(sb.remembered, [])

    def test_help_is_still_help_when_nothing_was_quoted(self):
        out = respond(FakeSandbox(), event("帮助"), allow=[ME])
        self.assertIn("查记忆", out.text)

    def test_reload_before_answering(self):
        # BloomBox / MCP / CLI 可能刚写过盘
        sb = FakeSandbox(hit=True)
        respond(sb, event("客服文档"), allow=[ME])
        self.assertEqual(sb.reloaded, 1)

    def test_duplicate_delivery_is_answered_once(self):
        sb = FakeSandbox(hit=True)
        seen = SeenMessages()
        self.assertIsNotNone(respond(sb, event("客服文档"), allow=[ME], seen=seen))
        self.assertIsNone(respond(sb, event("客服文档"), allow=[ME], seen=seen))

    def test_long_reply_is_clipped(self):
        refs = [{"question": "q" * 60, "answer": "a" * 300} for _ in range(20)]
        out = respond(FakeSandbox(hit=True, refs=refs), event("查"), allow=[ME])
        self.assertLessEqual(len(out.text), MAX_REPLY_CHARS + 40)

    def test_backend_error_still_gets_a_reply(self):
        sb = FakeSandbox(hit=True)
        sb.ask_local = lambda text: (_ for _ in ()).throw(RuntimeError("库炸了"))
        out = respond(sb, event("查"), allow=[ME])
        self.assertIn("库炸了", out.text)


class OnStartTests(unittest.TestCase):
    """「我接单了」的信号：干活的才发，不理的一律不发。"""

    def test_called_once_before_the_work(self):
        started: list = []
        sb = FakeSandbox(hit=True)
        sb.ask_local = lambda text: (
            started.append("ask") or SimpleNamespace(source="long_term", answer="答")
        )
        respond(sb, event("客服文档"), allow=[ME], on_start=lambda mid: started.append(mid))
        self.assertEqual(started[0], "om_1")
        self.assertEqual(started.count("om_1"), 1)

    def test_not_called_for_messages_we_ignore(self):
        started: list = []
        seen = SeenMessages()
        cb = lambda mid: started.append(mid)  # noqa: E731

        respond(FakeSandbox(), event("hi", sender_type="app"), allow=[ME], on_start=cb)
        respond(FakeSandbox(), event("谁啊", open_id="ou_x"), allow=[ME], on_start=cb)
        respond(FakeSandbox(hit=True), event("查"), allow=[ME], seen=seen, on_start=cb)
        respond(FakeSandbox(hit=True), event("查"), allow=[ME], seen=seen, on_start=cb)

        self.assertEqual(started, ["om_1"])

    def test_an_unlisted_bot_is_dropped_without_a_whitelist_lecture(self):
        # 白名单话术是说给人听的。冲另一个机器人喊，群里只多一条没人看的噪音
        started: list = []
        out = respond(
            FakeSandbox(hit=True),
            event("查", open_id="ou_slardar", sender_type="app"),
            allow=[ME],
            self_open_id=ME,
            on_start=lambda mid: started.append(mid),
        )
        self.assertIsNone(out)
        self.assertEqual(started, [])

    def test_a_whitelisted_bot_gets_a_real_answer(self):
        out = respond(
            FakeSandbox(hit=True),
            event("查", open_id="ou_slardar", sender_type="app"),
            allow=[ME, "ou_slardar"],
            self_open_id=ME,
        )
        self.assertIsNotNone(out)
        self.assertIn("命中的答案", out.text)

    def test_a_broken_callback_does_not_swallow_the_reply(self):
        def boom(mid):
            raise RuntimeError("表情接口挂了")

        out = respond(FakeSandbox(hit=True), event("查"), allow=[ME], on_start=boom)
        self.assertIn("命中的答案", out.text)


class FakeReactions:
    """记录表情调用；顺带保证 dispatch 的单测不会真的去连飞书。"""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.added: list = []
        self.removed: list = []

    def add(self, cfg, message_id, emoji_type):
        if self.fail:
            raise RuntimeError("need scope: im:message.reactions:write_only")
        self.added.append((cfg, message_id, emoji_type))
        return f"re_{len(self.added)}"

    def remove(self, cfg, message_id, reaction_id):
        self.removed.append((message_id, reaction_id))

    @property
    def emojis(self):
        return [emoji for _, _, emoji in self.added]


class DispatchTests(unittest.TestCase):
    """入口那层接缝：core/bot.py 和 send_bot_text 各自都对，拼起来仍可能错。"""

    def test_send_gets_the_feishu_section_not_the_whole_config(self):
        # 真出过：传了整个 AppConfig，运行时才炸 'AppConfig' object has no attribute 'app_id'
        import feishu_bot
        from core.config import AppConfig, FeishuConfig

        calls = []

        def fake_send(cfg, text, *, reply_to="", chat_id=""):
            calls.append((cfg, text, reply_to))
            return "om_sent"

        config = AppConfig()
        config.feishu = FeishuConfig(app_id="cli_x", app_secret="s")
        reactions = FakeReactions()
        got = feishu_bot.dispatch(
            event("客服文档"),
            sandbox=FakeSandbox(hit=True),
            config=config,
            allow=[ME],
            seen=SeenMessages(),
            send=fake_send,
            react_add=reactions.add,
            react_remove=reactions.remove,
        )

        self.assertEqual(got, "om_sent")
        self.assertIsInstance(calls[0][0], FeishuConfig)
        self.assertEqual(calls[0][2], "om_1")
        # 表情也得拿 FeishuConfig，和发消息同一个坑
        self.assertIsInstance(reactions.added[0][0], FeishuConfig)

    def test_fetch_also_gets_the_feishu_section(self):
        import feishu_bot
        from core.config import AppConfig, FeishuConfig

        seen_cfg = []

        def fake_fetch(cfg, message_id):
            seen_cfg.append(cfg)
            return "interactive", CARD

        config = AppConfig()
        config.feishu = FeishuConfig(app_id="cli_x", app_secret="s")
        sandbox = FakeSandbox()
        reactions = FakeReactions()
        feishu_bot.dispatch(
            event("记一下这个结论", parent_id="om_card"),
            sandbox=sandbox,
            config=config,
            allow=[ME],
            seen=SeenMessages(),
            send=lambda *a, **k: "om_sent",
            fetch=fake_fetch,
            react_add=reactions.add,
            react_remove=reactions.remove,
        )

        self.assertIsInstance(seen_cfg[0], FeishuConfig)
        self.assertEqual(sandbox.remembered[0][0], "报警归因")

    def test_ignored_event_sends_nothing(self):
        import feishu_bot
        from core.config import AppConfig

        calls = []
        reactions = FakeReactions()
        got = feishu_bot.dispatch(
            event("hi", sender_type="app"),
            sandbox=FakeSandbox(),
            config=AppConfig(),
            allow=[ME],
            seen=SeenMessages(),
            send=lambda *a, **k: calls.append(a),
            react_add=reactions.add,
            react_remove=reactions.remove,
        )
        self.assertIsNone(got)
        self.assertEqual(calls, [])
        self.assertEqual(reactions.added, [])

    def test_send_failure_does_not_escalate(self):
        # 发不出去只记一行日志，长连接不能跟着挂
        import feishu_bot
        from core.config import AppConfig

        def boom(*args, **kwargs):
            raise RuntimeError("权限没批")

        reactions = FakeReactions()
        with patch("sys.stderr"):
            got = feishu_bot.dispatch(
                event("客服文档"),
                sandbox=FakeSandbox(hit=True),
                config=AppConfig(),
                allow=[ME],
                seen=SeenMessages(),
                send=boom,
                react_add=reactions.add,
                react_remove=reactions.remove,
            )
        self.assertIsNone(got)


class EventLogTests(unittest.TestCase):
    """
    「消息到底有没有到本机」必须能从日志直接看出来。

    SDK 只在事件类型没有处理器时才打日志，而 im.message.receive_v1 是注册过的，
    所以「收到了但逻辑没处理」和「压根没收到」在日志里长得一模一样，排障时全靠猜。
    """

    def test_summary_identifies_the_message_without_quoting_it(self):
        import feishu_bot

        line = feishu_bot.event_summary(event("这句正文不该出现在日志里"))
        self.assertIn("chat_type=p2p", line)
        self.assertIn("mid=om_1", line)
        self.assertIn("from=ou_me", line)
        self.assertNotIn("这句正文", line)

    def test_summary_starts_with_the_time(self):
        """没有时间戳就没法把用户发来的截图和某一行日志对上。"""
        import feishu_bot

        head = feishu_bot.event_summary(event("查")).split()[0]
        self.assertRegex(head, r"^\d{2}:\d{2}:\d{2}$")

    def test_summary_flags_a_quoted_message(self):
        import feishu_bot

        payload = event("记下来")
        payload["event"]["message"]["parent_id"] = "om_parent"
        self.assertIn("引用=有", feishu_bot.event_summary(payload))

    def test_summary_survives_a_malformed_event(self):
        import feishu_bot

        # 日志辅助函数把主链路带崩就本末倒置了
        self.assertTrue(feishu_bot.event_summary({}))
        self.assertTrue(feishu_bot.event_summary({"event": "不是字典"}))

    def test_arrival_is_logged_even_when_the_event_is_ignored(self):
        import feishu_bot
        from core.config import AppConfig

        with patch("builtins.print") as printed:
            feishu_bot.dispatch(
                {"event": {"sender": {"sender_type": "app"}}},
                sandbox=FakeSandbox(),
                config=AppConfig(),
                allow=[ME],
                seen=SeenMessages(),
                send=lambda *a, **k: "om_sent",
            )
        logged = " ".join(str(c.args[0]) for c in printed.call_args_list if c.args)
        self.assertIn("收到消息", logged)
        self.assertIn("不处理", logged)


class ReactionTests(unittest.TestCase):
    """表情当进度条：接单贴「处理中」，答完换「完成」。"""

    def _config(self):
        from core.config import AppConfig, FeishuConfig

        config = AppConfig()
        config.feishu = FeishuConfig(app_id="cli_x", app_secret="s")
        return config

    def _dispatch(self, reactions, *, send=None, payload=None, allow=None):
        import feishu_bot

        return feishu_bot.dispatch(
            payload or event("客服文档"),
            sandbox=FakeSandbox(hit=True),
            config=self._config(),
            allow=[ME] if allow is None else allow,
            seen=SeenMessages(),
            send=send or (lambda *a, **k: "om_sent"),
            react_add=reactions.add,
            react_remove=reactions.remove,
        )

    def test_working_then_done(self):
        from core.feishu import REACTION_DONE, REACTION_WORKING

        reactions = FakeReactions()
        self._dispatch(reactions)

        self.assertEqual(reactions.emojis, [REACTION_WORKING, REACTION_DONE])
        # 「处理中」要撤掉，否则两个表情一起挂着
        self.assertEqual(reactions.removed, [("om_1", "re_1")])

    def test_reaction_lands_on_the_users_message(self):
        reactions = FakeReactions()
        self._dispatch(reactions)
        self.assertEqual({mid for _, mid, _ in reactions.added}, {"om_1"})

    def test_working_comes_before_the_answer(self):
        from core.feishu import REACTION_WORKING

        order: list = []
        reactions = FakeReactions()
        original_add = reactions.add

        def add(cfg, message_id, emoji_type):
            order.append(f"react:{emoji_type}")
            return original_add(cfg, message_id, emoji_type)

        reactions.add = add
        self._dispatch(reactions, send=lambda *a, **k: order.append("send") or "om_sent")

        self.assertEqual(order[0], f"react:{REACTION_WORKING}")
        self.assertEqual(order[1], "send")

    def test_send_failure_shows_the_failed_emoji(self):
        from core.feishu import REACTION_FAILED, REACTION_WORKING

        def boom(*args, **kwargs):
            raise RuntimeError("权限没批")

        reactions = FakeReactions()
        with patch("sys.stderr"):
            self._dispatch(reactions, send=boom)
        self.assertEqual(reactions.emojis, [REACTION_WORKING, REACTION_FAILED])

    def test_refused_stranger_gets_no_reaction(self):
        # 没资格的人只收到一句拒绝，不该看见机器人「在干活」
        reactions = FakeReactions()
        self._dispatch(reactions, payload=event("查", open_id="ou_x"))
        self.assertEqual(reactions.added, [])

    def test_missing_permission_never_blocks_the_reply(self):
        import feishu_bot

        reactions = FakeReactions(fail=True)
        with patch("sys.stderr"):
            got = self._dispatch(reactions)
        self.assertEqual(got, "om_sent")
        # 贴都没贴上就别再去撤
        self.assertEqual(reactions.removed, [])
        feishu_bot._reaction_warned.clear()

    def test_the_permission_warning_is_printed_once(self):
        import feishu_bot

        feishu_bot._reaction_warned.clear()
        reactions = FakeReactions(fail=True)
        with patch("sys.stderr") as err:
            self._dispatch(reactions)
            self._dispatch(reactions)
        printed = [c for c in err.write.call_args_list if "表情" in str(c)]
        self.assertEqual(len(printed), 1)
        feishu_bot._reaction_warned.clear()


class LlmFallbackTests(unittest.TestCase):
    """本地没命中就交给模型：不能再拿「没找到」把用户打发走。"""

    def _sandbox(self, answer: str = "模型算出来的结论"):
        sb = FakeSandbox(hit=False)
        sb.llm = object()  # 有模型才走慢通道
        sb.chatted: list = []

        def chat(text, **kwargs):
            sb.chatted.append(text)
            return SimpleNamespace(answer=answer, source="llm")

        sb.chat = chat
        return sb

    def test_miss_marks_the_question_for_the_slow_lane(self):
        out = respond(self._sandbox(), event("这个报警怎么排"), allow=[ME])
        self.assertEqual(out.slow_query, "这个报警怎么排")

    def test_hit_never_goes_to_the_model(self):
        sb = self._sandbox()
        sb.hit = True
        out = respond(sb, event("客服文档"), allow=[ME])
        self.assertEqual(out.slow_query, "")

    def test_without_a_model_the_old_wording_stays(self):
        out = respond(FakeSandbox(hit=False), event("这个报警怎么排"), allow=[ME])
        self.assertEqual(out.slow_query, "")
        self.assertIn("本地记忆里没找到", out.text)

    def test_remember_and_status_never_go_to_the_model(self):
        for text in ("记一下 mock 端口是 3001", "状态", "帮助"):
            out = respond(self._sandbox(), event(text), allow=[ME])
            self.assertEqual(out.slow_query, "", text)

    def test_answer_says_it_came_from_the_model_and_got_stored(self):
        from core.bot import answer_with_llm

        sb = self._sandbox()
        got = answer_with_llm(sb, "这个报警怎么排")
        self.assertIn("模型算出来的结论", got)
        self.assertIn("已存进记忆库", got)
        self.assertEqual(sb.chatted, ["这个报警怎么排"])

    def test_failed_answers_do_not_claim_to_be_stored(self):
        # sb.chat() 不写回失败类答复，这里就不能许空愿
        from core.bot import answer_with_llm

        got = answer_with_llm(self._sandbox("[LLM Error] agent 超时"), "问题")
        self.assertIn("超时", got)
        self.assertNotIn("已存进记忆库", got)

    def test_a_crashing_model_is_reported_verbatim(self):
        from core.bot import answer_with_llm

        sb = self._sandbox()
        sb.chat = lambda text, **kw: (_ for _ in ()).throw(RuntimeError("agent 没装"))
        self.assertIn("agent 没装", answer_with_llm(sb, "问题"))

    def test_long_answers_are_clipped(self):
        from core.bot import answer_with_llm

        got = answer_with_llm(self._sandbox("字" * (MAX_REPLY_CHARS + 500)), "问题")
        self.assertLess(len(got), MAX_REPLY_CHARS + 100)


class WorkerTests(unittest.TestCase):
    """慢任务必须离开长连接回调：飞书 3 秒不返回就重推同一条事件。"""

    def _config(self):
        from core.config import AppConfig, FeishuConfig

        config = AppConfig()
        config.feishu = FeishuConfig(app_id="cli_x", app_secret="s")
        return config

    def _slow_sandbox(self):
        sb = FakeSandbox(hit=False)
        sb.llm = object()
        sb.chat = lambda text, **kw: SimpleNamespace(answer="模型结论", source="llm")
        return sb

    def test_queue_runs_jobs_and_reports_when_full(self):
        import feishu_bot

        worker = feishu_bot.Worker(max_pending=1)  # 不 start，纯看队列容量
        self.assertTrue(worker.submit(lambda: None))
        self.assertFalse(worker.submit(lambda: None))

    def test_a_crashing_job_does_not_kill_the_thread(self):
        import threading

        import feishu_bot

        done = threading.Event()
        worker = feishu_bot.Worker()
        worker.start()
        worker.submit(lambda: (_ for _ in ()).throw(RuntimeError("炸")))
        worker.submit(done.set)
        self.assertTrue(done.wait(timeout=5))

    def test_slow_answer_is_queued_not_sent_inline(self):
        import threading

        import feishu_bot

        sent: list = []
        delivered = threading.Event()

        def fake_send(cfg, text, *, reply_to="", chat_id=""):
            sent.append((text, reply_to))
            delivered.set()
            return "om_sent"

        worker = feishu_bot.Worker()
        worker.start()
        reactions = FakeReactions()
        got = feishu_bot.dispatch(
            event("这个报警怎么排"),
            sandbox=self._slow_sandbox(),
            config=self._config(),
            allow=[ME],
            seen=SeenMessages(),
            send=fake_send,
            react_add=reactions.add,
            react_remove=reactions.remove,
            worker=worker,
        )

        # 回调立刻返回，回复由 worker 补发
        self.assertIsNone(got)
        self.assertTrue(delivered.wait(timeout=5))
        self.assertIn("模型结论", sent[0][0])
        self.assertEqual(sent[0][1], "om_1")

    def test_the_working_reaction_only_flips_when_the_slow_answer_lands(self):
        from core.feishu import REACTION_DONE, REACTION_WORKING

        import feishu_bot

        reactions = FakeReactions()
        feishu_bot.dispatch(
            event("这个报警怎么排"),
            sandbox=self._slow_sandbox(),
            config=self._config(),
            allow=[ME],
            seen=SeenMessages(),
            send=lambda *a, **k: "om_sent",
            react_add=reactions.add,
            react_remove=reactions.remove,
        )  # 不给 worker：原地跑完，方便断言顺序
        self.assertEqual(reactions.emojis, [REACTION_WORKING, REACTION_DONE])

    def test_a_full_queue_falls_back_to_the_local_answer(self):
        import feishu_bot

        sent: list = []
        worker = feishu_bot.Worker(max_pending=1)  # 不 start：塞满就不会被消费
        worker.submit(lambda: None)

        got = feishu_bot.dispatch(
            event("这个报警怎么排"),
            sandbox=self._slow_sandbox(),
            config=self._config(),
            allow=[ME],
            seen=SeenMessages(),
            send=lambda cfg, text, **k: sent.append(text) or "om_sent",
            react_add=FakeReactions().add,
            react_remove=FakeReactions().remove,
            worker=worker,
        )

        self.assertEqual(got, "om_sent")
        self.assertIn("本地记忆里没找到", sent[0])


class GroupMentionTests(unittest.TestCase):
    """
    群里必须 @ 到自己才回。踩过：用户 @ 的是另一个运维机器人，BloomBot 也跟着答了一条——
    以前收不到群消息所以没暴露，群消息权限一开就变成逢消息必接话。
    """

    ME_BOT = "ou_bot_me"

    def _out(self, payload, **kw):
        return respond(FakeSandbox(hit=True), payload, allow=[ME], self_open_id=self.ME_BOT, **kw)

    def test_a_group_message_aimed_at_another_bot_is_ignored(self):
        out = self._out(
            event(
                "@_user_1 记下来",
                chat_type="group",
                mentions=[("ou_slardar", "Slardar CAT 运维助理")],
            )
        )
        self.assertIsNone(out)

    def test_being_mentioned_in_the_group_still_answers(self):
        out = self._out(
            event(
                "@_user_1 客服文档",
                chat_type="group",
                mentions=[(self.ME_BOT, "BloomBot")],
            )
        )
        self.assertIsNotNone(out)

    def test_one_mention_among_several_is_enough(self):
        out = self._out(
            event(
                "@_user_1 @_user_2 客服文档",
                chat_type="group",
                mentions=[("ou_slardar", "Slardar CAT"), (self.ME_BOT, "BloomBot")],
            )
        )
        self.assertIsNotNone(out)

    def test_private_chat_never_needs_a_mention(self):
        self.assertIsNotNone(self._out(event("客服文档")))

    def test_an_unknown_self_id_keeps_answering_rather_than_going_mute(self):
        # 认不出自己就整个群哑掉的话，现象是「机器人挂了」，没有任何线索可查
        out = respond(
            FakeSandbox(hit=True),
            event("@_user_1 客服文档", chat_type="group", mentions=[("ou_x", "谁")]),
            allow=[ME],
        )
        self.assertIsNotNone(out)

    def test_the_display_name_is_a_fallback_when_the_open_id_is_unknown(self):
        out = respond(
            FakeSandbox(hit=True),
            event("@_user_1 客服文档", chat_type="group", mentions=[("ou_other", "BloomBot")]),
            allow=[ME],
            self_name="BloomBot",
        )
        self.assertIsNotNone(out)


class QuotedFallbackTests(unittest.TestCase):
    """
    应用身份读不动时换用户身份再试一次。

    注意只兜「报错」这一种：空壳曾经也重试过，2026-08-06 实测两种身份返回的字节数
    完全相同（跨应用卡片飞书对谁都不下发正文），那次重试是白打接口，已删。
    """

    SHELL = json.dumps(
        {
            "title": None,
            "elements": [[{"tag": "img", "image_key": "img_v3"}, {"tag": "text", "text": " "}]],
        }
    )

    def _cfg(self):
        from core.config import FeishuConfig

        return FeishuConfig(app_id="cli_x", app_secret="s")

    def _run(self, *, app_side, user_side):
        import feishu_bot

        calls: list = []

        def as_app(cfg, mid):
            calls.append("app")
            if isinstance(app_side, Exception):
                raise app_side
            return app_side

        def as_user(cfg, mid, config_path=None):
            calls.append("user")
            if isinstance(user_side, Exception):
                raise user_side
            return user_side

        with patch.object(feishu_bot, "fetch_bot_message", as_app), patch.object(
            feishu_bot, "fetch_message_as_user", as_user
        ):
            return feishu_bot.fetch_quoted(self._cfg(), "om_card"), calls

    def test_readable_text_never_touches_the_user_identity(self):
        got, calls = self._run(
            app_side=("text", json.dumps({"text": "结论：签约日期过期"})),
            user_side=("text", json.dumps({"text": "不该被用到"})),
        )
        self.assertIn("签约日期过期", message_text(*got))
        self.assertEqual(calls, ["app"])

    def test_an_empty_shell_is_not_retried_as_the_user(self):
        """实测用户身份返回同样的空壳，重试只是白打一次接口。"""
        got, calls = self._run(
            app_side=("interactive", self.SHELL),
            user_side=("interactive", CARD),
        )
        self.assertEqual(got, ("interactive", self.SHELL))
        self.assertEqual(calls, ["app"])

    def test_the_user_identity_also_covers_an_outright_app_side_failure(self):
        got, _calls = self._run(
            app_side=RuntimeError("230027 没开 im:message.group_msg"),
            user_side=("text", json.dumps({"text": "用户身份读到的"})),
        )
        self.assertIn("用户身份读到的", message_text(*got))

    def test_both_failing_reports_the_app_side_reason(self):
        # 应用身份那条错带着「去开 im:message.group_msg」的指路，比用户身份的错有用
        with self.assertRaises(RuntimeError) as caught:
            self._run(
                app_side=RuntimeError("230027 没开 im:message.group_msg"),
                user_side=RuntimeError("99991679 缺 im:message:readonly"),
            )
        self.assertIn("230027", str(caught.exception))


class ChatContextTests(unittest.TestCase):
    """
    引用的卡片读不出字时，去上游几条里找料。

    这条路是照着现场反推出来的：Slardar 的结论卡片是 157 字节空壳，但它上游那条
    **人转发进群的**告警卡片有 1115 个可读字，接口照给。别的机器人（Aime / Mira）
    能就同一张卡片给出结论，读的就是这段，不是什么高级权限。
    """

    SHELL = json.dumps(
        {"title": None, "elements": [[{"tag": "img", "image_key": "img_v3"}]]}
    )

    def _messages(self):
        return [
            {"msg_type": "interactive", "content": CARD, "sender_type": "user"},
            {
                "msg_type": "text",
                "content": json.dumps({"text": "@_user_1 分析这个告警"}),
                "sender_type": "user",
            },
            {"msg_type": "interactive", "content": self.SHELL, "sender_type": "app"},
        ]

    def test_context_keeps_the_readable_and_drops_the_shell(self):
        from core.bot import build_chat_context

        got = build_chat_context(self._messages())
        self.assertIn("结论：签约日期过期", got)
        self.assertIn("【群成员】", got)
        self.assertNotIn("img_v3", got)  # 空壳整条丢掉，别拿占位图充数

    def test_the_upgrade_placeholder_is_not_content(self):
        """有的跨应用卡片壳里带着「请升级客户端」，抽得出字，但一个字都不是内容。"""
        from core.bot import build_chat_context

        got = build_chat_context(
            [
                {
                    "msg_type": "interactive",
                    "content": json.dumps(
                        {"elements": [[{"tag": "text", "text": "请升级至最新版本客户端，以查看内容"}]]}
                    ),
                    "sender_type": "app",
                },
                {"msg_type": "interactive", "content": CARD, "sender_type": "user"},
            ]
        )
        self.assertNotIn("请升级", got)
        self.assertIn("报警归因", got)

    def test_context_keeps_chronological_order(self):
        from core.bot import build_chat_context

        got = build_chat_context(self._messages())
        self.assertLess(got.index("报警归因"), got.index("分析这个告警"))

    def test_over_budget_keeps_the_longest(self):
        """预算不够时先保长的：告警详情、日志才是料，寒暄挤掉了也不可惜。"""
        from core.bot import (
            MAX_CONTEXT_CHARS,
            MAX_CONTEXT_MESSAGE_CHARS,
            build_chat_context,
        )

        def msg(text: str) -> dict:
            return {
                "msg_type": "text",
                "content": json.dumps({"text": text}),
                "sender_type": "user",
            }

        # 按预算推尺寸：两条装得下、三条装不下，改常量时不用回来改用例
        size = min(MAX_CONTEXT_MESSAGE_CHARS, MAX_CONTEXT_CHARS * 2 // 5)
        got = build_chat_context(
            [
                msg("嗯"),
                msg("告警详情 " + "料" * size),
                msg("关键日志 " + "料" * (size - 100)),
                msg("补充说明 " + "料" * (size - 200)),
            ]
        )
        self.assertLessEqual(len(got), MAX_CONTEXT_CHARS)
        self.assertIn("告警详情", got)
        self.assertIn("关键日志", got)
        self.assertNotIn("补充说明", got)
        self.assertIn("嗯", got)  # 剩下的边角料顺手带上，它又不占地方

    def test_parse_qa_reads_the_two_lines(self):
        from core.bot import parse_context_qa

        q, a = parse_context_qa("问题：4030066 是什么\n答案：签约日期过期\n补充一行")
        self.assertEqual(q, "4030066 是什么")
        self.assertEqual(a, "签约日期过期\n补充一行")

    def test_parse_qa_refuses_an_empty_handed_model(self):
        from core.bot import parse_context_qa

        self.assertEqual(parse_context_qa("问题：x\n答案：信息不足"), ("", ""))
        self.assertEqual(parse_context_qa("我觉得可能是日期问题吧"), ("", ""))


class ContextRememberTests(unittest.TestCase):
    def _sandbox(self, reply: str):
        sb = FakeSandbox()
        sb.llm = SimpleNamespace(
            generate=lambda prompt, context="", on_progress=None: reply
        )
        return sb

    def test_conclusion_is_stored_with_a_searchable_question(self):
        from core.bot import remember_with_context

        sb = self._sandbox("问题：Anchor 邀请接口 4030066 报警根因\n答案：签约日期过期")
        got = remember_with_context(sb, "记下来", "【群成员】告警详情…")
        self.assertEqual(
            sb.remembered, [("Anchor 邀请接口 4030066 报警根因", "签约日期过期", "dev")]
        )
        self.assertIn("已记住", got)
        self.assertIn("还原", got)  # 得说清这条不是照抄卡片，是推出来的

    def test_not_enough_material_stores_nothing(self):
        from core.bot import remember_with_context

        sb = self._sandbox("问题：x\n答案：信息不足")
        got = remember_with_context(sb, "记下来", "【群成员】嗯")
        self.assertEqual(sb.remembered, [])
        self.assertIn("复制", got)

    def test_a_crashing_model_still_gives_a_way_out(self):
        from core.bot import remember_with_context

        sb = FakeSandbox()
        sb.llm = SimpleNamespace(
            generate=lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("agent 超时"))
        )
        got = remember_with_context(sb, "记下来", "上下文")
        self.assertIn("agent 超时", got)
        self.assertIn("复制", got)
        self.assertEqual(sb.remembered, [])


class ContextFallbackRoutingTests(unittest.TestCase):
    """什么时候才去翻上游：只在引用读不出字、且配了模型的时候。"""

    SHELL = ChatContextTests.SHELL

    def _sandbox(self):
        sb = FakeSandbox()
        sb.llm = object()
        return sb

    def _respond(self, sb, *, fetch_message, history):
        return respond(
            sb,
            event("记下来", parent_id="om_card"),
            allow=[ME],
            fetch_message=fetch_message,
            fetch_context=history,
        )

    def test_unreadable_card_goes_to_the_slow_lane_with_context(self):
        calls = []

        def history(chat_id, before):
            calls.append((chat_id, before))
            return [{"msg_type": "interactive", "content": CARD, "sender_type": "user"}]

        out = self._respond(
            self._sandbox(),
            fetch_message=lambda mid: ("interactive", self.SHELL),
            history=history,
        )
        self.assertEqual(calls, [("oc_1", "om_card")])
        self.assertEqual(out.slow_query, "记下来")
        self.assertIn("结论：签约日期过期", out.slow_context)

    def test_a_readable_quote_never_reads_the_history(self):
        calls = []
        sb = self._sandbox()
        out = self._respond(
            sb,
            fetch_message=lambda mid: ("interactive", CARD),
            history=lambda chat_id, before: calls.append(before) or [],
        )
        self.assertEqual(calls, [])
        self.assertEqual(out.slow_context, "")
        self.assertTrue(sb.remembered)

    def test_nothing_readable_upstream_falls_back_to_copy_paste(self):
        out = self._respond(
            self._sandbox(),
            fetch_message=lambda mid: ("interactive", self.SHELL),
            history=lambda chat_id, before: [
                {"msg_type": "interactive", "content": self.SHELL, "sender_type": "app"}
            ],
        )
        self.assertEqual(out.slow_context, "")
        self.assertIn("复制", out.text)

    def test_without_a_model_it_does_not_even_read_the_history(self):
        calls = []
        out = self._respond(
            FakeSandbox(),  # 没有 llm
            fetch_message=lambda mid: ("interactive", self.SHELL),
            history=lambda chat_id, before: calls.append(before) or [],
        )
        self.assertEqual(calls, [])
        self.assertIn("复制", out.text)

    def test_a_failing_history_read_says_why(self):
        def boom(chat_id, before):
            raise RuntimeError("need scope: im:message.group_msg")

        out = self._respond(
            self._sandbox(),
            fetch_message=lambda mid: ("interactive", self.SHELL),
            history=boom,
        )
        self.assertIn("im:message.group_msg", out.text)

    def test_dispatch_routes_context_to_the_remember_lane(self):
        import feishu_bot
        from core.config import AppConfig, FeishuConfig

        config = AppConfig()
        config.feishu = FeishuConfig(app_id="cli_x", app_secret="s")
        sent: list = []
        seen: list = []

        with patch.object(
            feishu_bot, "remember_with_context", lambda sb, q, ctx: seen.append(ctx) or "已记住"
        ), patch.object(
            feishu_bot, "answer_with_llm", lambda sb, q: "不该走到这"
        ):
            feishu_bot.dispatch(
                event("记下来", parent_id="om_card"),
                sandbox=self._sandbox(),
                config=config,
                allow=[ME],
                seen=SeenMessages(),
                send=lambda cfg, text, reply_to="": sent.append(text) or "om_out",
                fetch=lambda cfg, mid: ("interactive", self.SHELL),
                history=lambda cfg, chat_id, before_message_id="": [
                    {"msg_type": "interactive", "content": CARD, "sender_type": "user"}
                ],
                react_add=lambda *a, **kw: "r1",
                react_remove=lambda *a, **kw: None,
            )
        self.assertEqual(sent, ["已记住"])
        self.assertIn("结论：签约日期过期", seen[0])


class LlmTimeoutTests(unittest.TestCase):
    def _cfg(self, llm_timeout, cap):
        from core.config import AppConfig, FeishuConfig

        cfg = AppConfig()
        cfg.feishu = FeishuConfig(bot_llm_timeout=cap)
        cfg.llm.timeout = llm_timeout
        return cfg

    def test_chat_caps_the_batch_sized_timeout(self):
        import feishu_bot

        cfg = self._cfg(600, 150)
        feishu_bot.clamp_llm_timeout(cfg)
        self.assertEqual(cfg.llm.timeout, 150)

    def test_a_shorter_configured_timeout_is_left_alone(self):
        import feishu_bot

        cfg = self._cfg(30, 150)
        feishu_bot.clamp_llm_timeout(cfg)
        self.assertEqual(cfg.llm.timeout, 30)

    def test_zero_cap_disables_the_clamp(self):
        import feishu_bot

        cfg = self._cfg(600, 0)
        feishu_bot.clamp_llm_timeout(cfg)
        self.assertEqual(cfg.llm.timeout, 600)


class SendTests(unittest.TestCase):
    def _cfg(self):
        from core.config import FeishuConfig

        return FeishuConfig(app_id="cli_x", app_secret="s", api_base="https://open.feishu.cn")

    def test_reply_uses_the_reply_endpoint(self):
        from core import feishu

        captured = {}

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            captured["url"] = url
            captured["body"] = body
            return {"code": 0, "data": {"message_id": "om_new"}}

        with patch.object(feishu, "_tenant_access_token", return_value="t"), patch.object(
            feishu, "_http_json", side_effect=fake_http
        ):
            got = feishu.send_bot_text(self._cfg(), "你好", reply_to="om_1")

        self.assertEqual(got, "om_new")
        self.assertTrue(captured["url"].endswith("/im/v1/messages/om_1/reply"))
        self.assertEqual(json.loads(captured["body"]["content"])["text"], "你好")

    def test_chat_id_uses_the_create_endpoint(self):
        from core import feishu

        captured = {}

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            captured["url"] = url
            captured["body"] = body
            return {"code": 0, "data": {"message_id": "om_new"}}

        with patch.object(feishu, "_tenant_access_token", return_value="t"), patch.object(
            feishu, "_http_json", side_effect=fake_http
        ):
            feishu.send_bot_text(self._cfg(), "你好", chat_id="oc_1")

        self.assertIn("receive_id_type=chat_id", captured["url"])
        self.assertEqual(captured["body"]["receive_id"], "oc_1")

    def test_fetch_returns_type_and_content(self):
        from core import feishu

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            self.assertEqual(method, "GET")
            self.assertTrue(url.endswith("/im/v1/messages/om_card"))
            return {
                "code": 0,
                "data": {"items": [{"msg_type": "interactive", "body": {"content": CARD}}]},
            }

        with patch.object(feishu, "_tenant_access_token", return_value="t"), patch.object(
            feishu, "_http_json", side_effect=fake_http
        ):
            got = feishu.fetch_bot_message(self._cfg(), "om_card")

        self.assertEqual(got, ("interactive", CARD))

    def test_add_reaction_posts_the_emoji_and_returns_its_id(self):
        from core import feishu

        captured = {}

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            captured.update(method=method, url=url, body=body)
            return {"code": 0, "data": {"reaction_id": "re_1"}}

        with patch.object(feishu, "_tenant_access_token", return_value="t"), patch.object(
            feishu, "_http_json", side_effect=fake_http
        ):
            got = feishu.add_message_reaction(self._cfg(), "om_1", feishu.REACTION_WORKING)

        self.assertEqual(got, "re_1")
        self.assertEqual(captured["method"], "POST")
        self.assertTrue(captured["url"].endswith("/im/v1/messages/om_1/reactions"))
        self.assertEqual(captured["body"], {"reaction_type": {"emoji_type": "OnIt"}})

    def test_remove_reaction_deletes_by_reaction_id(self):
        from core import feishu

        captured = {}

        def fake_http(method, url, *, headers=None, body=None, timeout=30.0):
            captured.update(method=method, url=url)
            return {"code": 0}

        with patch.object(feishu, "_tenant_access_token", return_value="t"), patch.object(
            feishu, "_http_json", side_effect=fake_http
        ):
            feishu.remove_message_reaction(self._cfg(), "om_1", "re_1")

        self.assertEqual(captured["method"], "DELETE")
        self.assertTrue(captured["url"].endswith("/im/v1/messages/om_1/reactions/re_1"))

    def test_remove_without_a_reaction_id_is_a_no_op(self):
        from core import feishu

        with patch.object(feishu, "_http_json", side_effect=AssertionError("不该发请求")):
            feishu.remove_message_reaction(self._cfg(), "om_1", "")

    def test_reaction_permission_error_names_the_scope(self):
        from core import feishu

        with patch.object(feishu, "_tenant_access_token", return_value="t"), patch.object(
            feishu, "_http_json", side_effect=RuntimeError("HTTP 400: code 231002")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                feishu.add_message_reaction(self._cfg(), "om_1", "DONE")
        self.assertIn("im:message.reactions:write_only", str(ctx.exception))

    def test_fetch_permission_error_names_the_scope(self):
        from core import feishu

        with patch.object(feishu, "_tenant_access_token", return_value="t"), patch.object(
            feishu, "_http_json", side_effect=RuntimeError("HTTP 400: code 230027")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                feishu.fetch_bot_message(self._cfg(), "om_card")
        self.assertIn("im:message.group_msg", str(ctx.exception))

    def test_permission_error_names_the_scope(self):
        from core import feishu

        with patch.object(feishu, "_tenant_access_token", return_value="t"), patch.object(
            feishu, "_http_json", side_effect=RuntimeError("HTTP 403: code 99991672")
        ):
            with self.assertRaises(RuntimeError) as ctx:
                feishu.send_bot_text(self._cfg(), "x", reply_to="om_1")
        self.assertIn("im:message:send_as_bot", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
