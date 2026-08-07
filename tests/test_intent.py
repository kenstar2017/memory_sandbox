"""写入意图识别：机器人和 BloomBox 共用这一份判定。

这里的重点是**误判**：把提问当成写入，会往库里塞垃圾，还会让用户以为机器人聋了。
所以正例之外，反例写得比正例还多。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.intent import (  # noqa: E402
    detect_remember,
    is_deictic,
    split_question_answer,
)
from core.utils import assemble_long_term_query  # noqa: E402


def body_of(text: str):
    got = detect_remember(text)
    return None if got is None else got.body


class HeadFormTests(unittest.TestCase):
    def test_classic_prefixes(self):
        self.assertEqual(body_of("记一下：mock 端口是 3001"), "mock 端口是 3001")
        self.assertEqual(body_of("记住 mock 端口是 3001"), "mock 端口是 3001")
        self.assertEqual(body_of("记下来 mock 端口是 3001"), "mock 端口是 3001")

    def test_politeness_is_ignored(self):
        for text in ("帮我记一下 x", "麻烦帮我记住 x", "请记录一下：x"):
            self.assertEqual(body_of(text), "x", text)

    def test_into_memory_phrases(self):
        self.assertEqual(body_of("存到记忆库：x"), "x")
        self.assertEqual(body_of("写进长期记忆 x"), "x")
        self.assertEqual(body_of("加到记忆里"), "")

    def test_deictic_carries_no_content(self):
        got = detect_remember("记一下这个结论")
        self.assertEqual(got.body, "这个结论")
        self.assertEqual(got.content, "")
        self.assertTrue(is_deictic("上面这条"))
        self.assertFalse(is_deictic("这个报警的根因"))


class BaFormTests(unittest.TestCase):
    def test_ba_takes_whatever_is_in_between(self):
        self.assertEqual(body_of("把飞书长连接的坑记一下"), "飞书长连接的坑")
        self.assertEqual(body_of("将 mock 端口是 3001 存到记忆库"), "mock 端口是 3001")

    def test_ba_with_a_deictic_object_has_no_content(self):
        got = detect_remember("把这个结论记到长期记忆")
        self.assertIsNotNone(got)
        self.assertEqual(got.content, "")


class TailFormTests(unittest.TestCase):
    def test_verb_at_the_end(self):
        self.assertEqual(
            body_of("这个报警的根因是签约日期过期，记一下"), "这个报警的根因是签约日期过期"
        )
        self.assertEqual(body_of("mock 端口是 3001，帮我存到记忆库吧"), "mock 端口是 3001")

    def test_trailing_pleasantries_are_not_content(self):
        self.assertEqual(
            body_of("这个报警的根因是签约日期过期，记一下，别忘了"), "这个报警的根因是签约日期过期"
        )
        self.assertEqual(detect_remember("记一下这个结论，谢谢").content, "")

    def test_modal_verbs(self):
        self.assertEqual(body_of("一定要记住 mock 端口是 3001"), "mock 端口是 3001")
        self.assertEqual(detect_remember("这个要记住").content, "")

    def test_a_pause_is_required(self):
        # 没有停顿就分不清「要记的东西」和「句子的一部分」
        self.assertIsNone(detect_remember("这个功能怎么记录"))


class EnglishTests(unittest.TestCase):
    def test_remember_prefix(self):
        self.assertEqual(body_of("remember: mock port is 3001"), "mock port is 3001")
        self.assertEqual(body_of("Please remember that mock port is 3001"), "mock port is 3001")

    def test_deictic(self):
        self.assertEqual(body_of("remember this"), "")
        self.assertTrue(is_deictic("this"))


class NotAWriteTests(unittest.TestCase):
    def test_questions(self):
        for text in (
            "保存位置在哪",
            "收藏夹在哪",
            "记忆库怎么用",
            "记忆状态",
            "如何记住这些配置",
            "我今天记录了很多东西",
            "帮我看下这个结论",
            "记得上次那个端口是多少",
        ):
            self.assertIsNone(detect_remember(text), text)

    def test_question_mark_wins(self):
        self.assertIsNone(detect_remember("这个要记一下吗？"))

    def test_retrieval_suffix_is_not_a_write(self):
        # assemble_long_term_query 拼的后缀是检索标记；认成写入的话，
        # 每一次 MCP 预检索都会顺手往库里写一条
        assembled = assemble_long_term_query("飞书机器人怎么配权限")
        self.assertIn("记录到长期记忆", assembled)
        self.assertIsNone(detect_remember(assembled))
        self.assertIsNone(detect_remember("记录到长期记忆。"))

    def test_management_commands_are_left_alone(self):
        for text in ("备份长时记忆", "清空长时记忆", "查看长时记忆", "优化已有记忆"):
            self.assertIsNone(detect_remember(text), text)

    def test_empty(self):
        self.assertIsNone(detect_remember(""))
        self.assertIsNone(detect_remember("   "))


class SplitTests(unittest.TestCase):
    def test_arrow(self):
        self.assertEqual(split_question_answer("问 => 答"), ("问", "答"))
        self.assertEqual(split_question_answer("问 || 答"), ("问", "答"))

    def test_multiline(self):
        self.assertEqual(split_question_answer("问\n答一\n答二"), ("问", "答一\n答二"))

    def test_single_blob(self):
        self.assertIsNone(split_question_answer("就一句话"))
        self.assertIsNone(split_question_answer(""))


if __name__ == "__main__":
    unittest.main()
