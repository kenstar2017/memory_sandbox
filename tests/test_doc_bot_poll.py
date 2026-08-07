"""评论轮询兜底的单测：不连飞书也能把「该回哪条、不该回哪条」全测一遍。

轮询比事件更危险：事件是飞书挑好了推给你，轮询是自己把整篇文档的评论捞回来。
一旦水位算错，历史评论会被当成新评论批量回复——那是在别人文档里刷屏。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.doc_bot import parse_comment_event  # noqa: E402
from core.doc_bot_poll import (  # noqa: E402
    PollCursor,
    advance_cursor,
    collect_new_replies,
    parse_epoch,
    poll_targets,
    reply_key,
    synthesize_comment_event,
)


def reply(reply_id: str, created: str, text: str, user_id: str = "ou_me"):
    return SimpleNamespace(
        reply_id=reply_id, created_at=created, text=text, user_id=user_id
    )


def comment(comment_id: str, replies):
    return SimpleNamespace(comment_id=comment_id, reply_items=list(replies))


class ParseEpochTests(unittest.TestCase):
    def test_reads_second_level_strings(self):
        self.assertEqual(parse_epoch("1700000000"), 1700000000.0)

    def test_garbage_becomes_zero_so_the_floor_drops_it(self):
        for bad in ("", None, "昨天", []):
            self.assertEqual(parse_epoch(bad), 0.0)


class CollectTests(unittest.TestCase):
    def test_only_replies_after_the_watermark(self):
        c = comment("c1", [reply("r1", "100", "老的"), reply("r2", "200", "新的")])
        got = collect_new_replies([c], since=150)
        self.assertEqual([i.reply.reply_id for i in got], ["r2"])

    def test_history_is_skipped_entirely_on_first_pass(self):
        # 启动水位 = 当前时间，知识库里堆着的历史评论一条都不能被当成新的
        c = comment("c1", [reply(f"r{i}", str(i), f"历史{i}") for i in range(1, 50)])
        self.assertEqual(collect_new_replies([c], since=1_000_000), [])

    def test_empty_replies_are_not_worth_a_round_trip(self):
        c = comment("c1", [reply("r1", "200", "   ")])
        self.assertEqual(collect_new_replies([c], since=100), [])

    def test_sorted_by_time_across_comments(self):
        got = collect_new_replies(
            [
                comment("c1", [reply("r1", "300", "晚")]),
                comment("c2", [reply("r2", "200", "早")]),
            ],
            since=100,
        )
        self.assertEqual([i.reply.reply_id for i in got], ["r2", "r1"])

    def test_seen_keys_drop_the_ones_handled_at_the_watermark(self):
        c = comment("c1", [reply("r1", "200", "已处理"), reply("r2", "200", "同秒新增")])
        got = collect_new_replies([c], since=200, seen_keys=["c1:r1"])
        self.assertEqual([i.reply.reply_id for i in got], ["r2"])

    def test_first_reply_is_the_comment_itself(self):
        c = comment("c1", [reply("r1", "200", "评论"), reply("r2", "201", "回复")])
        got = collect_new_replies([c], since=100)
        self.assertEqual([i.is_first for i in got], [True, False])

    def test_no_trigger_filter_here_or_confirmations_get_eaten(self):
        # 等确认的提案里用户只回一句「确认」，不带 @；在这层按触发词过滤会吞掉它
        c = comment("c1", [reply("r1", "200", "确认")])
        self.assertEqual(len(collect_new_replies([c], since=100)), 1)


class CursorTests(unittest.TestCase):
    def test_watermark_moves_to_the_newest_handled_reply(self):
        items = collect_new_replies(
            [comment("c1", [reply("r1", "200", "a"), reply("r2", "300", "b")])],
            since=100,
        )
        cur = advance_cursor(PollCursor(since=100), items)
        self.assertEqual(cur.since, 300)
        self.assertEqual(cur.edge_keys, ["c1:r2"])

    def test_nothing_new_keeps_the_cursor_put(self):
        cur = advance_cursor(PollCursor(since=300, edge_keys=["c1:r2"]), [])
        self.assertEqual((cur.since, cur.edge_keys), (300, ["c1:r2"]))

    def test_same_second_arrivals_accumulate_instead_of_replacing(self):
        items = collect_new_replies(
            [comment("c1", [reply("r3", "300", "同秒又一条")])],
            since=300,
            seen_keys=["c1:r2"],
        )
        cur = advance_cursor(PollCursor(since=300, edge_keys=["c1:r2"]), items)
        self.assertEqual(cur.since, 300)
        self.assertEqual(sorted(cur.edge_keys), ["c1:r2", "c1:r3"])

    def test_a_full_cycle_never_hands_back_the_same_reply(self):
        c = comment("c1", [reply("r1", "200", "一"), reply("r2", "200", "二")])
        cur = PollCursor(since=150)
        first = collect_new_replies([c], since=cur.since, seen_keys=cur.edge_keys)
        cur = advance_cursor(cur, first)
        second = collect_new_replies([c], since=cur.since, seen_keys=cur.edge_keys)
        self.assertEqual(len(first), 2)
        self.assertEqual(second, [])


class SynthesizeTests(unittest.TestCase):
    def test_payload_parses_back_into_a_comment_event(self):
        items = collect_new_replies(
            [comment("c1", [reply("r1", "200", "@BloomBot 记一下", user_id="ou_x")])],
            since=100,
        )
        payload = synthesize_comment_event(file_token="tok", item=items[0])
        ev = parse_comment_event(payload)
        self.assertIsNotNone(ev)
        self.assertEqual(ev.file_token, "tok")
        self.assertEqual(ev.file_type, "docx")
        self.assertEqual(ev.open_id, "ou_x")
        self.assertEqual(ev.notice_type, "add_comment")

    def test_dedup_key_matches_the_event_path(self):
        """两条路必须算出同一个键，否则同一条评论会被回两遍。"""
        items = collect_new_replies(
            [comment("c9", [reply("r9", "200", "hi")])], since=100
        )
        payload = synthesize_comment_event(file_token="tok", item=items[0])
        self.assertEqual(parse_comment_event(payload).key, "c9:r9")
        self.assertEqual(reply_key(items[0].comment, items[0].reply), "c9:r9")

    def test_reply_is_marked_as_add_reply(self):
        items = collect_new_replies(
            [comment("c1", [reply("r1", "200", "评论"), reply("r2", "300", "回复")])],
            since=250,
        )
        payload = synthesize_comment_event(file_token="tok", item=items[0])
        self.assertEqual(parse_comment_event(payload).notice_type, "add_reply")


class TargetTests(unittest.TestCase):
    def test_skips_failed_and_duplicate_docs(self):
        docs = [
            SimpleNamespace(document_id="a", url="ua", last_error=""),
            SimpleNamespace(document_id="b", url="ub", last_error="404 没了"),
            SimpleNamespace(document_id="a", url="ua2", last_error=""),
            SimpleNamespace(document_id="", url="uc", last_error=""),
        ]
        self.assertEqual(poll_targets(docs), [("a", "ua")])

    def test_extra_accepts_both_links_and_bare_tokens(self):
        got = poll_targets(
            [],
            ["https://x.larkoffice.com/docx/tok1", "tok2", "", "  "],
        )
        self.assertEqual(
            got, [("tok1", "https://x.larkoffice.com/docx/tok1"), ("tok2", "")]
        )

    def test_extra_does_not_duplicate_a_knowledge_doc(self):
        docs = [SimpleNamespace(document_id="tok1", url="ua", last_error="")]
        got = poll_targets(docs, ["https://x.larkoffice.com/docx/tok1"])
        self.assertEqual(got, [("tok1", "ua")])


if __name__ == "__main__":
    unittest.main()
