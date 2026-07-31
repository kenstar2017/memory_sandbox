"""检索设置读写与热更新单测。"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core import MemorySandbox
from core.config import (
    AppConfig,
    LLMConfig,
    LongTermConfig,
    SensoryConfig,
    WorkingConfig,
    persist_long_term_settings,
)


class RetrievalSettingsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mem_rs_")
        self.cfg = AppConfig(
            sensory=SensoryConfig(ttl=5.0),
            working=WorkingConfig(chunk_size=7),
            long_term=LongTermConfig(
                persist_dir=self.tmp,
                similarity_threshold=0.70,
                vector_weight=0.55,
                keyword_weight=0.20,
                bm25_weight=0.25,
                bm25_enabled=True,
            ),
            llm=LLMConfig(enabled=True, provider="mock"),
        )
        self.sb = MemorySandbox(config=self.cfg)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_get_includes_fields_and_help(self):
        payload = self.sb.get_retrieval_settings()
        self.assertIn("values", payload)
        self.assertIn("fields", payload)
        keys = {f["key"] for f in payload["fields"]}
        self.assertIn("vector_weight", keys)
        self.assertIn("bm25_weight", keys)
        self.assertTrue(all(f.get("help") for f in payload["fields"]))
        self.assertAlmostEqual(payload["values"]["vector_weight"], 0.55)

    def test_set_hot_updates_long_term(self):
        msg = self.sb.set_retrieval_settings(
            {
                "vector_weight": 0.4,
                "keyword_weight": 0.3,
                "bm25_weight": 0.3,
                "similarity_threshold": 0.8,
            },
            persist=False,
        )
        self.assertIn("vector_weight=0.4", msg)
        self.assertEqual(self.sb.long_term.vector_weight, 0.4)
        self.assertEqual(self.sb.config.long_term.similarity_threshold, 0.8)
        self.assertEqual(self.sb.long_term.similarity_threshold, 0.8)

    def test_persist_writes_user_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "config.yaml"
            # 伪造 Application Support 路径判断：直接测 persist 函数
            with mock.patch(
                "core.config._user_config_path", return_value=cfg_path
            ):
                path = persist_long_term_settings(
                    None, {"vector_weight": 0.42, "bm25_enabled": False}
                )
            self.assertEqual(path, str(cfg_path))
            text = cfg_path.read_text(encoding="utf-8")
            self.assertIn("vector_weight: 0.42", text)
            self.assertIn("bm25_enabled: false", text)


if __name__ == "__main__":
    unittest.main()
