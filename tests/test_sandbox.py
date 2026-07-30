#!/usr/bin/env python3
"""核心链路冒烟测试（标准库 unittest）。"""

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
from core.utils import assemble_long_term_query


class MemorySandboxTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_sandbox_")
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

    def test_sensory_reject_empty(self):
        r = self.sb.chat("   ")
        self.assertEqual(r.source, "sensory_reject")

    def test_working_arith_and_followup(self):
        r1 = self.sb.chat("计算 3*4")
        self.assertEqual(r1.source, "working")
        self.assertEqual(r1.answer, "12")
        r2 = self.sb.chat("再加3")
        self.assertEqual(r2.source, "working")
        self.assertEqual(r2.answer, "15")

    def test_long_term_hit_skips_llm(self):
        self.sb.remember("构建命令", "pnpm build")
        r = self.sb.chat("构建命令是什么")
        self.assertEqual(r.source, "long_term")
        self.assertIn("pnpm build", r.answer)

    def test_llm_fallback(self):
        r = self.sb.chat("一个从未见过的冷门问题 xyzzy-42")
        self.assertEqual(r.source, "llm")
        self.assertIn("MockLLM", r.answer)

    def test_forget_command(self):
        self.sb.remember("密钥口令", "不要外传")
        r = self.sb.chat("忘记：密钥")
        self.assertEqual(r.source, "command")
        r2 = self.sb.chat("密钥口令是什么")
        self.assertEqual(r2.source, "llm")

    def test_clear_long_term_requires_confirm(self):
        self.sb.remember("重要知识", "不要丢")
        r = self.sb.chat("清空长时记忆")
        self.assertEqual(r.source, "command")
        self.assertTrue(r.meta.get("needs_confirm"))
        self.assertEqual(len(self.sb.long_term.records), 1)
        r2 = self.sb.chat("确认清空长时记忆")
        self.assertEqual(r2.source, "command")
        self.assertIn("已清空", r2.answer)
        self.assertEqual(len(self.sb.long_term.records), 0)

    def test_backup_and_restore_long_term(self):
        self.sb.remember("备份测试问", "备份测试答")
        msg = self.sb.backup_long_term()
        self.assertIn("已备份", msg)
        backups = self.sb.long_term.list_backups()
        self.assertTrue(backups)
        self.sb.long_term.forget()
        self.assertEqual(len(self.sb.long_term.records), 0)
        restored = self.sb.restore_long_term(str(backups[0]))
        self.assertIn("恢复", restored)
        self.assertEqual(len(self.sb.long_term.records), 1)
        self.assertEqual(self.sb.long_term.records[0].answer, "备份测试答")

    def test_assemble_long_term_query(self):
        self.assertEqual(
            assemble_long_term_query("revenue 怎么本地启动"),
            "revenue 怎么本地启动，记录到长期记忆。",
        )
        self.assertEqual(
            assemble_long_term_query("revenue 怎么本地启动，记录到长期记忆。"),
            "revenue 怎么本地启动，记录到长期记忆。",
        )


if __name__ == "__main__":
    unittest.main()
