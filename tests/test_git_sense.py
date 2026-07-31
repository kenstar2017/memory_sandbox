#!/usr/bin/env python3
"""Git 变更感知与协作提示测试。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.config import AppConfig, LLMConfig, LongTermConfig, SensoryConfig, WorkingConfig
from core.git_sense import find_stale_memories, suggest_review_habits


class _Rec:
    def __init__(self, **kw):
        self.id = kw.get("id", "x")
        self.question = kw.get("question", "")
        self.answer = kw.get("answer", "")
        self.tags = kw.get("tags", [])
        self.facts = kw.get("facts", {})
        self.keywords = kw.get("keywords", [])


class GitSenseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_git_")
        self.cfg = AppConfig(
            sensory=SensoryConfig(ttl=5.0),
            working=WorkingConfig(chunk_size=7),
            long_term=LongTermConfig(persist_dir=self.tmp, similarity_threshold=0.65),
            llm=LLMConfig(enabled=True, provider="mock"),
        )
        self.sb = MemorySandbox(config=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_find_stale_by_path(self):
        recs = [
            _Rec(
                id="1",
                question="飞书配置脚本",
                answer="见 scripts/configure_feishu.sh",
                facts={"path": "scripts/configure_feishu.sh"},
                tags=["feishu"],
            ),
            _Rec(id="2", question="无关", answer="hello world"),
        ]
        hints = find_stale_memories(
            recs, ["scripts/configure_feishu.sh", "README.md"], limit=5
        )
        self.assertTrue(hints)
        self.assertEqual(hints[0].memory_id, "1")
        self.assertTrue(hints[0].matched_paths)

    def test_review_habits_from_log(self):
        with patch("core.git_sense.resolve_git_root", return_value=Path(self.tmp)):
            with patch(
                "core.git_sense._run_git",
                return_value="fix: login race\nfeat: add oauth\nchore: bump",
            ):
                hints = suggest_review_habits(self.tmp, max_hints=3)
        self.assertTrue(hints)
        self.assertTrue(any("bug" in h.question or "修" in h.question for h in hints))

    def test_sandbox_git_check_no_repo(self):
        payload = self.sb.check_git_changes(cwd=self.tmp)
        self.assertIsNone(payload.get("git_root"))
        self.assertEqual(payload.get("stale"), [])

    def test_list_packs_empty(self):
        data = self.sb.list_packs()
        self.assertIn("packs", data)
        self.assertEqual(data["packs"], [])


if __name__ == "__main__":
    unittest.main()
