#!/usr/bin/env python3
"""BM25 混合检索 / 老化 / 知识包 相关测试。"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import MemorySandbox
from core.bm25 import BM25Index
from core.config import AppConfig, LLMConfig, LongTermConfig, SensoryConfig, WorkingConfig
from core.pack import build_pack, load_pack, write_pack


class P2Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_p2_")
        self.cfg = AppConfig(
            sensory=SensoryConfig(ttl=5.0),
            working=WorkingConfig(chunk_size=7),
            long_term=LongTermConfig(
                persist_dir=self.tmp,
                similarity_threshold=0.55,
                top_k=3,
                bm25_enabled=True,
                vector_weight=0.45,
                keyword_weight=0.20,
                bm25_weight=0.35,
                aging_enabled=True,
                aging_days=30,
                aging_min_hits=0,
                aging_decay=0.2,
            ),
            llm=LLMConfig(enabled=True, provider="mock"),
        )
        self.sb = MemorySandbox(config=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_bm25_index_ranks_relevant_doc(self):
        idx = BM25Index()
        idx.rebuild(["猫喜欢鱼", "狗喜欢骨头", "飞书 oauth 登录脚本"])
        ranked = idx.ranked("飞书 oauth", top_k=2)
        self.assertTrue(ranked)
        self.assertEqual(ranked[0][0], 2)

    def test_hybrid_search_includes_bm25_reason(self):
        self.sb.remember("飞书登录方式", "使用 oauth 脚本 scripts/feishu_login.py", tags=["feishu"])
        self.sb.remember("无关条目", "今天天气不错", tags=["misc"])
        hits = self.sb.long_term.search_hits("oauth 飞书登录", tags=["feishu"])
        self.assertTrue(hits)
        reasons = " ".join(hits[0].reasons)
        self.assertTrue("bm25" in reasons or "alias" in reasons or "vector" in reasons)

    def test_aging_decay_lowers_stale(self):
        self.sb.remember("陈旧知识", "旧答案AAA", scene="dev")
        rec = self.sb.long_term.records[0]
        # 直接改磁盘时间戳
        rec.updated_at = time.time() - 120 * 86400
        rec.hit_count = 0
        self.sb.long_term._save_declarative()
        self.sb.long_term.reload()
        hits = self.sb.long_term.search_hits("陈旧知识", threshold=0.01, top_k=1)
        self.assertTrue(hits)
        self.assertTrue(any(r.startswith("aging:") for r in hits[0].reasons))

    def test_archive_stale(self):
        self.sb.remember("该归档", "旧内容", scene="dev")
        rec = self.sb.long_term.records[0]
        rec.updated_at = time.time() - 200 * 86400
        rec.hit_count = 0
        self.sb.long_term._save_declarative()
        msg = self.sb.archive_stale(older_than_days=30, min_hits=0, confirm=True)
        self.assertIn("已归档", msg)
        self.sb.long_term.reload()
        self.assertEqual(len(self.sb.long_term.records), 0)
        archive = Path(self.tmp) / "declarative_archive.jsonl"
        self.assertTrue(archive.is_file())

    def test_pack_export_import(self):
        self.sb.remember("包内知识", "pnpm build", scene="dev", tags=["frontend"], kind="command")
        pack = build_pack(self.sb.long_term.records, name="frontend-pack", filter_tags=["frontend"])
        self.assertEqual(len(pack.records), 1)
        self.assertNotIn("vector", pack.records[0])
        out = write_pack(pack, self.tmp)
        # 清空再导入
        self.sb.long_term.forget()
        self.assertEqual(len(self.sb.long_term.records), 0)
        msg = self.sb.import_pack(str(out), merge=True, confirm=True)
        self.assertIn("导入", msg)
        self.sb.long_term.reload()
        self.assertEqual(len(self.sb.long_term.records), 1)
        self.assertIn("pnpm build", self.sb.long_term.records[0].answer)

    def test_pack_strips_secrets(self):
        self.sb.remember("密钥", "TOKEN=ghp_abcdefghijklmnopqrstuvwxyz0123456789", tags=["sec"])
        pack = build_pack(self.sb.long_term.records, name="sec", scrub=True)
        blob = json.dumps(pack.as_dict(), ensure_ascii=False)
        self.assertNotIn("ghp_", blob)
        self.assertIn("[REDACTED]", blob)


if __name__ == "__main__":
    unittest.main()
