"""飞书链接解析与工作记忆复用单测（无网络）。"""

import unittest

from core.feishu import extract_feishu_urls
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


class WorkingReuseTests(unittest.TestCase):
    def test_feishu_url_skips_working_reuse(self):
        wm = WorkingMemory(chunk_size=7)
        q = "https://bytedance.larkoffice.com/wiki/QZJowpLhBiY58xkChx0chzTZn4f 分析文档"
        bad = "读不到该文档正文，无法提炼前端工作。user_access_token Invalid access token 99991668"
        wm.add_context(q, ["分析", "文档"], role="user")
        wm.add_context(bad, ["读不到"], role="assistant")
        self.assertTrue(is_non_reusable_answer(bad))
        self.assertIsNone(wm.local_match(q))


if __name__ == "__main__":
    unittest.main()

