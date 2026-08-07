"""存储问法的归一化：可以清洗，不能截断。

标题是用户回头认这条记忆的唯一凭据，所以存储侧只清口水前缀和句末标点；
检索侧的核心词（extract_core）该怎么剥还怎么剥，只是不再写进标题。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.question_optimize import extract_core, optimize_question  # noqa: E402


def canonical(question: str) -> str:
    return optimize_question(question).canonical


class NormalisationTests(unittest.TestCase):
    def test_spoken_heads_and_trailing_punctuation_go_away(self):
        self.assertEqual(canonical("请问 agency 项目的启动命令是什么？"), "agency 项目的启动命令是什么")
        self.assertEqual(canonical("我想知道灰度开关在哪里"), "灰度开关在哪里")

    def test_core_stays_searchable_even_though_the_title_is_whole(self):
        got = optimize_question("灰度开关的文件位置在哪里")
        self.assertEqual(got.canonical, "灰度开关的文件位置在哪里")
        self.assertIn("灰度开关", got.aliases)
        self.assertIn("灰度开关", got.embed_text)


class NeverTruncateTests(unittest.TestCase):
    def test_why_is_not_split_into_a_dangling_character(self):
        title = "飞书群里 @BloomBot 说「记一下这个结论」，机器人既不回复也不记忆，为什么"
        self.assertEqual(canonical(title), title)

    def test_titles_are_kept_whole(self):
        for title in (
            "记忆沙箱如何修掉会误导检索的过时记忆 #memory-sandbox #mcp",
            "BloomBox 如何感知外部（MCP/CLI）新写、原地覆盖、删除记忆",
            "长连接怎么保存，先跑本地进程再点保存",
            "agency 项目怎么启动",
            "live_web_agency 是干什么",
            "项目A根目录完整路径",
            "客服技术文档在哪里",
            "切换开发环境要注意什么",
            "这个功能记一下",
            "构建命令",
        ):
            self.assertEqual(canonical(title), title, title)

    def test_optimisation_is_idempotent(self):
        # 不幂等就意味着每跑一次「优化已有记忆」标题都再掉一个字
        for title in (
            "客服技术文档在哪里",
            "live_web_agency 是干什么",
            "请问 agency 项目的启动命令是什么",
            "飞书机器人长连接通了但收不到消息怎么定位 #feishu #bot",
        ):
            once = canonical(title)
            self.assertEqual(canonical(once), once, title)


class QuerySideStaysAggressiveTests(unittest.TestCase):
    """检索侧的核心词是一次性的，照旧尽量剥，别被存储侧的保护拖累。"""

    def test_core_still_strips_the_predicate(self):
        self.assertEqual(extract_core("agency 项目怎么启动"), "agency 项目")

    def test_core_strips_lookup_tails(self):
        self.assertEqual(extract_core("客服技术文档在哪里"), "客服技术文档")

    def test_core_does_not_split_why(self):
        self.assertEqual(extract_core("长连接为什么断了"), "长连接为什么断")

    def test_core_keeps_a_bounded_how_tail(self):
        # `.*$` 会把整条从句吃掉，只剩项目名
        self.assertEqual(
            extract_core("agency 项目怎么启动并配置代理服务"), "agency 项目怎么启动并配置代理服务"
        )


if __name__ == "__main__":
    unittest.main()
