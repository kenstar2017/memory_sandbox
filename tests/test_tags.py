#!/usr/bin/env python3
"""tags 工具单元测试。"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core.tags import merge_tags, normalize_tags, parse_tags_from_text, tags_match


class TagsTests(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_tags(["#Feishu", " frontend ", "feishu"]), ["feishu", "frontend"])

    def test_parse_from_text(self):
        self.assertEqual(parse_tags_from_text("修 #bugfix 并更新 #Frontend"), ["bugfix", "frontend"])

    def test_merge_and_match(self):
        merged = merge_tags(["a"], ["#B", "a"])
        self.assertEqual(merged, ["a", "b"])
        self.assertTrue(tags_match(["a", "b"], ["b"], mode="any"))
        self.assertFalse(tags_match(["a"], ["b"], mode="any"))
        self.assertTrue(tags_match(["a", "b"], ["a", "b"], mode="all"))


if __name__ == "__main__":
    unittest.main()
