#!/usr/bin/env python3
"""脱敏 / 结构化 / 提炼 相关核心测试。"""

from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.config import AppConfig, LLMConfig, LongTermConfig, SensoryConfig, WorkingConfig
from core.extract import extract_memory_candidates
from core.scrub import scrub_text


class P1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_p1_")
        self.cfg = AppConfig(
            sensory=SensoryConfig(ttl=5.0),
            working=WorkingConfig(chunk_size=7),
            long_term=LongTermConfig(
                persist_dir=self.tmp,
                similarity_threshold=0.65,
                top_k=3,
            ),
            llm=LLMConfig(enabled=True, provider="mock"),
        )
        self.sb = MemorySandbox(config=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scrub_token_and_env(self):
        text = "OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwxyz012345\nBearer abcdefghijklmnop"
        r = scrub_text(text)
        self.assertTrue(r.redacted)
        self.assertIn("[REDACTED]", r.text)
        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz012345", r.text)

    def test_remember_scrubs_secrets(self):
        msg = self.sb.remember(
            "密钥配置",
            "TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            scene="dev",
            kind="env",
            facts={"env": "TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789"},
        )
        self.assertIn("已脱敏", msg)
        rec = self.sb.long_term.records[0]
        self.assertIn("[REDACTED]", rec.answer)
        self.assertNotIn("ghp_", rec.answer)

    def test_structured_kind_and_facts(self):
        self.sb.remember(
            "前端构建命令",
            "pnpm build",
            scene="dev",
            tags=["frontend"],
            kind="command",
            facts={"command": "pnpm build"},
        )
        rec = self.sb.long_term.records[0]
        self.assertEqual(rec.kind, "command")
        self.assertEqual(rec.facts.get("command"), "pnpm build")
        hits = self.sb.long_term.search_hits("构建", kind="command", tags=["frontend"])
        self.assertTrue(hits)
        self.assertEqual(hits[0].record.kind, "command")

    def test_extract_candidates(self):
        text = """
$ pnpm build
Error: missing FEISHU_APP_ID
决定改用 oauth 脚本登录
./scripts/configure_feishu.sh
"""
        cands = extract_memory_candidates(text, max_n=3)
        self.assertTrue(cands)
        kinds = {c.kind for c in cands}
        self.assertTrue(kinds & {"command", "pitfall", "decision", "path"})

    def test_sandbox_extract_api(self):
        payload = self.sb.extract_candidates("$ curl https://example.com\nFailed to connect")
        self.assertIn("candidates", payload)
        self.assertTrue(payload["candidates"])


if __name__ == "__main__":
    unittest.main()
